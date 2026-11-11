"""
FastAPI Main Application.
Routes:
  GET  /        - Minimal Chat UI with Role Switcher
  GET  /health  - Health Check
  POST /login   - Issue JWT for Demo Users
  POST /chat    - Natural Language Agent with Deterministic Policy Enforcement
  POST /confirm - Phase 2 Confirmation of Previewed Operations
  POST /cancel  - Cancellation of Previewed Operations
"""

from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, func

from app.config import settings
from app.db import get_db
from app.auth import Principal, get_current_principal, auth_router
from app.models import InventoryItem, User, Bill
from app.schemas import (
    Intent,
    InventoryReadIntent,
    BillsReadIntent,
    UsersReadIntent,
    InventoryBulkUpdateIntent,
    AccessChangeIntent,
    intent_adapter,
)
from app.agent.parser import BaseIntentParser, get_intent_parser
from app.agent.dates import resolve_date_range
from app.policy.engine import decide
from app.policy.rules import DecisionType
from app.execution.ops_read import (
    inventory_read_op,
    bills_read_op,
    users_read_op,
    format_inventory_summary,
    format_bills_summary,
    ensure_tenant_context,
)
from app.execution.dryrun import generate_dryrun_preview
from app.execution.confirm import confirm_write_action, cancel_write_action
from app.execution.ops_undo import undo_last_change_op
from app.execution.ops_approval import (
    create_approval_request,
    list_pending_approvals,
    execute_admin_approval,
)
from app.schemas import UndoIntent, ApprovalIntent
from app.audit import record_audit

app = FastAPI(
    title="HMS Permission-Aware Operations Agent API",
    description="Hospital Management System Agent with deterministic code-enforced policy",
    version="1.0.0",
)

# Authentication router
app.include_router(auth_router)


# Request & Response Models
class ChatRequest(BaseModel):
    message: str


class ConfirmRequest(BaseModel):
    token: str
    typed_count: Optional[int] = None


class CancelRequest(BaseModel):
    token: str


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/chat")
def chat_endpoint(
    req: ChatRequest,
    principal: Principal = Depends(get_current_principal),
    parser: BaseIntentParser = Depends(get_intent_parser),
    db: Session = Depends(get_db),
):
    """
    Main agent chat endpoint.
    Guarantees:
    - LLM only proposes an intent (identity never passed to or trusted from LLM)
    - Code gathers facts and policy engine decides
    - Audit log recorded in finally block for all exits
    """
    raw_query = req.message
    parsed_intent = None
    decision_type = "REFUSE"
    decision_reason = "Processing error"
    status_code_audit = "error"
    rows_affected = 0
    preview_data = None

    try:
        ensure_tenant_context(db, principal.hospital_id)

        # 1. LLM Intent parsing (LLM NEVER receives user id, role, or hospital id)
        parsed_intent = parser.parse_intent(raw_query)

        # 2. Fact Resolution in code
        facts: Dict[str, Any] = {}
        intent_type = getattr(parsed_intent, "type", "")

        # Check for cross-tenant mention in query
        q_lower = raw_query.lower()
        if principal.hospital_id == 1 and ("hospital b" in q_lower or "hosp b" in q_lower):
            facts["mentions_other_hospital"] = True
        elif principal.hospital_id == 2 and ("hospital a" in q_lower or "hosp a" in q_lower):
            facts["mentions_other_hospital"] = True

        # Pre-count affected rows for inventory update (needed for pharmacist row cap check)
        if intent_type == "inventory_bulk_update":
            scope = getattr(parsed_intent, "scope", "all")
            cnt_query = select(func.count(InventoryItem.id)).where(
                InventoryItem.hospital_id == principal.hospital_id
            )
            if scope == "category" and getattr(parsed_intent, "category", None):
                cnt_query = cnt_query.where(
                    InventoryItem.category.ilike(f"%{parsed_intent.category.strip()}%")
                )
            elif scope == "name_contains" and getattr(parsed_intent, "name_contains", None):
                cnt_query = cnt_query.where(
                    InventoryItem.name.ilike(f"%{parsed_intent.name_contains.strip()}%")
                )
            count_val = db.execute(cnt_query).scalar() or 0
            facts["affected_rows_count"] = count_val

        # Resolve target users in principal's hospital for access change
        if intent_type == "access_change":
            target_name = getattr(parsed_intent, "target_user_name", "")
            if target_name.lower() in ("me", "myself", "my"):
                facts["target_user_id"] = principal.user_id
            else:
                matching_users = db.execute(
                    select(User).where(
                        and_(
                            User.hospital_id == principal.hospital_id,
                            User.name.ilike(f"%{target_name.strip()}%"),
                        )
                    )
                ).scalars().all()
                facts["target_users"] = [{"id": u.id, "name": u.name} for u in matching_users]
                if len(matching_users) == 0:
                    facts["user_not_found"] = True
                elif len(matching_users) > 1:
                    facts["ambiguous_target"] = True
                else:
                    facts["target_user_id"] = matching_users[0].id

        # 3. Pure Policy Decision
        decision = decide(principal=principal, intent=parsed_intent, facts=facts)
        decision_type = str(decision.decision.value)
        decision_reason = decision.reason

        # 4. Route based on Policy Decision
        if decision.decision == DecisionType.REFUSE:
            if (facts.get("route_approval") or (intent_type == "bills_discount" and "Billing manager" in principal.roles)):
                appr_res = create_approval_request(
                    principal=principal,
                    requested_intent_data=parsed_intent.model_dump(),
                    reason=f"Discount of {getattr(parsed_intent, 'pct', 0)}% exceeds role ceiling.",
                    session=db,
                )
                status_code_audit = "refused"
                msg_txt = f"Request exceeds role ceiling. It has been routed to Admin for review as #{appr_res['request_id']}."
                return {
                    "exit": "refuse",
                    "decision": "REFUSE",
                    "reason": msg_txt,
                    "answer": msg_txt,
                    "approval": appr_res,
                }

            status_code_audit = "refused"
            return {
                "exit": "refuse",
                "decision": "REFUSE",
                "reason": decision.reason,
                "offered_alternative": decision.offered_alternative,
            }

        elif decision.decision == DecisionType.CLARIFY:
            status_code_audit = "clarify"
            return {
                "exit": "clarify",
                "decision": "CLARIFY",
                "question": decision.reason,
            }

        elif decision.decision == DecisionType.ALLOW_WITH_PREVIEW:
            # Phase 1: Dry run preview with confirmation token
            preview_data = generate_dryrun_preview(
                principal=principal,
                intent=parsed_intent,
                session=db,
            )
            rows_affected = preview_data.get("rows_affected", 0)
            status_code_audit = "previewed"
            return {
                "exit": "preview",
                "decision": "ALLOW_WITH_PREVIEW",
                "message": decision.reason,
                "preview": preview_data,
            }

        else:
            # DecisionType.ALLOW (Read operations)
            status_code_audit = "answered"
            if intent_type == "inventory_read":
                items = inventory_read_op(principal, parsed_intent, db)
                summary_text = format_inventory_summary(items)
                return {
                    "exit": "answer",
                    "decision": "ALLOW",
                    "answer": summary_text,
                    "count": len(items),
                    "items": items[:25],
                }

            elif intent_type == "bills_read":
                bills = bills_read_op(principal, parsed_intent, db, decision.applied_filters)
                summary_text = format_bills_summary(bills)
                return {
                    "exit": "answer",
                    "decision": "ALLOW",
                    "answer": summary_text,
                    "count": len(bills),
                    "bills": bills[:25],
                }

            elif intent_type == "users_read":
                users = users_read_op(principal, parsed_intent, db)
                return {
                    "exit": "answer",
                    "decision": "ALLOW",
                    "users": users,
                }

            elif intent_type == "undo":
                undo_res = undo_last_change_op(principal, db)
                status_code_audit = "executed" if undo_res.get("success") else "refused"
                rows_affected = undo_res.get("rows_affected", 0)
                before_snap = undo_res.get("before_snapshot")
                after_snap = undo_res.get("after_snapshot")
                return {
                    "exit": "answer" if undo_res.get("success") else "refuse",
                    "decision": "ALLOW" if undo_res.get("success") else "REFUSE",
                    "answer": undo_res.get("message"),
                    "reason": undo_res.get("message"),
                    "details": undo_res,
                }

            elif intent_type == "approval":
                action = getattr(parsed_intent, "action", "list")
                if action == "list":
                    appr_res = list_pending_approvals(principal, db)
                    status_code_audit = "answered"
                    return {
                        "exit": "answer",
                        "decision": "ALLOW",
                        "answer": appr_res.get("message"),
                        "requests": appr_res.get("requests", []),
                        "count": appr_res.get("count", 0),
                    }
                elif action in ("approve", "reject"):
                    req_id = getattr(parsed_intent, "request_id", "") or "1"
                    exec_res = execute_admin_approval(principal, req_id, action, db)
                    status_code_audit = "executed" if exec_res.get("success") else "refused"
                    rows_affected = exec_res.get("rows_affected", 0)
                    before_snap = exec_res.get("before_snapshot")
                    after_snap = exec_res.get("after_snapshot")
                    return {
                        "exit": "answer" if exec_res.get("success") else "refuse",
                        "decision": "ALLOW" if exec_res.get("success") else "REFUSE",
                        "answer": exec_res.get("message"),
                        "reason": exec_res.get("message"),
                        "details": exec_res,
                    }

            return {
                "exit": "answer",
                "decision": "ALLOW",
                "answer": "Operation executed successfully.",
            }

    finally:
        # Every request path writes exactly one audit_log row
        record_audit(
            hospital_id=principal.hospital_id,
            user_id=principal.user_id,
            raw_query=raw_query,
            parsed_intent=parsed_intent,
            decision=decision_type,
            reason=decision_reason,
            status=status_code_audit,
            rows_affected=rows_affected,
            session=db,
        )


@app.post("/confirm")
def confirm_endpoint(
    req: ConfirmRequest,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    """Confirm a previously previewed write action."""
    status_audit = "error"
    rows_affected = 0
    before_snap = None
    after_snap = None
    reason_audit = ""
    parsed_intent_audit = None

    try:
        ensure_tenant_context(db, principal.hospital_id)
        result = confirm_write_action(
            token=req.token,
            typed_count=req.typed_count,
            principal=principal,
            session=db,
        )
        status_audit = "executed"
        rows_affected = result.get("rows_affected", 0)
        before_snap = result.get("before_snapshot")
        after_snap = result.get("after_snapshot")
        parsed_intent_audit = result.get("intent")
        reason_audit = result.get("message", "Confirmed and executed")
        return result
    except Exception as e:
        reason_audit = str(e)
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        record_audit(
            hospital_id=principal.hospital_id,
            user_id=principal.user_id,
            raw_query=f"CONFIRM token={req.token[:10]}...",
            parsed_intent=parsed_intent_audit,
            decision="CONFIRM",
            status=status_audit,
            reason=reason_audit,
            rows_affected=rows_affected,
            before_snapshot=before_snap,
            after_snapshot=after_snap,
            session=db,
        )


@app.post("/cancel")
def cancel_endpoint(
    req: CancelRequest,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    """Cancel a previously previewed write action."""
    status_audit = "cancelled"
    reason_audit = "User cancelled previewed operation"

    try:
        ensure_tenant_context(db, principal.hospital_id)
        result = cancel_write_action(token=req.token, principal=principal, session=db)
        return result
    except Exception as e:
        reason_audit = str(e)
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        record_audit(
            hospital_id=principal.hospital_id,
            user_id=principal.user_id,
            raw_query=f"CANCEL token={req.token[:10]}...",
            decision="CANCEL",
            status=status_audit,
            reason=reason_audit,
            session=db,
        )


# Minimal Chat HTML Interface served at GET /
HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hextgen HMS Operations Agent</title>
  <style>
    :root {
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --border: #e2e8f0;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --danger: #dc2626;
      --success: #16a34a;
      --warning: #d97706;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text-main); line-height: 1.5; padding: 20px 10px; }
    .container { max-width: 920px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
    .header { background: var(--card-bg); padding: 18px 22px; border-radius: 12px; border: 1px solid var(--border); box-shadow: 0 1px 3px rgba(0,0,0,0.05); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
    .header h1 { font-size: 20px; font-weight: 700; color: #1e293b; display: flex; align-items: center; gap: 8px; }
    .header .subtitle { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
    .badge { display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .badge-admin { background: #fee2e2; color: #991b1b; }
    .badge-billing { background: #fef3c7; color: #92400e; }
    .badge-pharmacist { background: #dcfce7; color: #166534; }
    .badge-frontdesk { background: #e0e7ff; color: #3730a3; }
    .badge-doctor { background: #f3e8ff; color: #6b21a8; }
    .card { background: var(--card-bg); border-radius: 12px; border: 1px solid var(--border); box-shadow: 0 1px 3px rgba(0,0,0,0.05); padding: 16px; }
    .role-bar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
    .role-selector { display: flex; align-items: center; gap: 10px; }
    select, input, button { font-size: 14px; border-radius: 8px; border: 1px solid #cbd5e1; outline: none; transition: all 0.15s ease; }
    select { padding: 8px 12px; background: #fff; cursor: pointer; }
    select:focus, input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(37,99,235,0.15); }
    button { padding: 8px 14px; font-weight: 500; cursor: pointer; background: var(--primary); color: #fff; border: none; }
    button:hover { background: var(--primary-hover); }
    .btn-secondary { background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; }
    .btn-secondary:hover { background: #e2e8f0; }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-danger:hover { background: #b91c1c; }
    .btn-success { background: var(--success); color: #fff; }
    .btn-success:hover { background: #15803d; }
    .chips-section { display: flex; flex-direction: column; gap: 8px; }
    .chips-label { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; }
    .chips-group { display: flex; flex-wrap: wrap; gap: 6px; }
    .chip { font-size: 12px; padding: 6px 12px; background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 20px; cursor: pointer; color: #334155; transition: all 0.15s; }
    .chip:hover { background: #e2e8f0; border-color: #cbd5e1; color: #0f172a; transform: translateY(-1px); }
    .chip-te { background: #ecfdf5; border-color: #a7f3d0; color: #065f46; }
    .chip-te:hover { background: #d1fae5; border-color: #6ee7b7; }
    #chat { height: 420px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 4px; }
    .msg { padding: 12px 16px; border-radius: 12px; max-width: 85%; font-size: 14px; line-height: 1.5; word-break: break-word; }
    .msg-user { align-self: flex-end; background: var(--primary); color: #fff; border-bottom-right-radius: 2px; }
    .msg-bot { align-self: flex-start; background: #fff; border: 1px solid var(--border); border-bottom-left-radius: 2px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
    .msg-refuse { border-left: 4px solid var(--danger); background: #fef2f2; }
    .msg-clarify { border-left: 4px solid var(--warning); background: #fffbeb; }
    .msg-answer { border-left: 4px solid var(--success); }
    .preview-card { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px; padding: 14px; margin-top: 10px; }
    .blast-guard { background: #fee2e2; border: 1px solid #fecaca; border-radius: 8px; padding: 10px; margin: 10px 0; color: #991b1b; font-weight: 500; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
    th, td { border: 1px solid #cbd5e1; padding: 6px 10px; text-align: left; }
    th { background: #f8fafc; font-weight: 600; }
    .footer-bar { font-size: 11px; color: var(--text-muted); display: flex; justify-content: space-between; align-items: center; padding: 0 4px; }
  </style>
</head>
<body>
<div class="container">
  <!-- Header -->
  <div class="header">
    <div>
      <h1>🏥 Hextgen HMS Operations Agent</h1>
      <div class="subtitle">Deterministic Code-Enforced Permissions • Double Tenant Isolation • Gemini LLM • 0.0% False Allows</div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <button class="btn-secondary" onclick="runPrompt('Undo my last change')">↩️ Undo Last Change</button>
      <button class="btn-secondary" onclick="runPrompt('Show pending approvals')">📋 Pending Approvals</button>
    </div>
  </div>

  <!-- Role Switcher -->
  <div class="card role-bar">
    <div class="role-selector">
      <b>Active Persona:</b>
      <select id="userSelect" onchange="switchUser()">
        <option value="admin.a@hospital.com">Priya Sharma — Admin (Hospital A)</option>
        <option value="pharmacist.a@hospital.com">Anita Desai — Pharmacist (Hospital A)</option>
        <option value="billing.a@hospital.com">Suresh Patel — Billing Manager (Hospital A)</option>
        <option value="frontdesk.a@hospital.com">Vikram Singh — Front Desk (Hospital A)</option>
        <option value="doctor.a@hospital.com">Dr. Rajesh Kumar — Doctor (Hospital A)</option>
        <option value="admin.b@hospital.com">Sneha Rao — Admin (Hospital B)</option>
        <option value="pharmacist.b@hospital.com">Pooja Nair — Pharmacist (Hospital B)</option>
      </select>
      <span id="roleBadge" class="badge badge-admin">Admin</span>
      <span id="hospBadge" class="badge" style="background:#f1f5f9;color:#334155;">Hospital 1</span>
    </div>
    <button class="btn-secondary" onclick="clearChat()">🧹 Clear Chat</button>
  </div>

  <!-- Multilingual & Quick Prompt Chips -->
  <div class="card chips-section">
    <div>
      <div class="chips-label">🇮🇳 Telugu Input (Stretch Goal)</div>
      <div class="chips-group" style="margin-top:4px;">
        <span class="chip chip-te" onclick="runPrompt('Last month bills ki 20% discount ivvu')">Last month bills ki 20% discount ivvu</span>
        <span class="chip chip-te" onclick="runPrompt('Expiring medicines chupinchu')">Expiring medicines chupinchu</span>
        <span class="chip chip-te" onclick="runPrompt('Paracetamol stock 100 ki update cheyyi')">Paracetamol stock 100 ki update cheyyi</span>
      </div>
    </div>
    <div>
      <div class="chips-label">🇬🇧 English Scenarios</div>
      <div class="chips-group" style="margin-top:4px;">
        <span class="chip" onclick="runPrompt('Show items expiring in next 30 days')">Show items expiring in next 30 days</span>
        <span class="chip" onclick="runPrompt('Bulk update the entire inventory with quantity 5')">Bulk update entire inventory with qty 5 (Blast Radius)</span>
        <span class="chip" onclick="runPrompt('Give 20% discount on all bills from last month')">Give 20% discount (Ceiling / Approval)</span>
        <span class="chip" onclick="runPrompt('Give 5% discount on all bills from last month')">Give 5% discount (Allowed for Billing)</span>
        <span class="chip" onclick="runPrompt('Show bills from last month')">Show bills from last month</span>
        <span class="chip" onclick="runPrompt('Show inventory of Hospital B')">Show inventory of Hospital B (Cross-Tenant Denial)</span>
        <span class="chip" onclick="runPrompt('Make me an admin')">Make me an admin (Privilege Escalation Denial)</span>
      </div>
    </div>
  </div>

  <!-- Chat Log -->
  <div class="card"><div id="chat"></div></div>

  <!-- Preview Area -->
  <div class="card" id="previewArea" style="display:none;"></div>

  <!-- Input Area -->
  <div class="card" style="display:flex;gap:10px;">
    <input type="text" id="inputQuery" style="flex:1;padding:10px 14px;" placeholder="Type plain English or Telugu instruction..." onkeypress="if(event.key==='Enter')sendQuery()">
    <button onclick="sendQuery()" style="padding:10px 20px;">Send</button>
  </div>

  <!-- Footer Info -->
  <div class="footer-bar">
    <span>🔒 PostgreSQL 17 Row-Level Security Enabled</span>
    <span>⚡ Connected: Google Gemini LLM (Intent Proposal Layer)</span>
    <span>📊 Deterministic Code Engine (Execution &amp; Audit Layer)</span>
  </div>
</div>

<script>
let token = '', activeToken = '', requiredRows = 0;

async function switchUser() {
  const email = document.getElementById('userSelect').value;
  const res = await fetch('/login?email=' + encodeURIComponent(email), {method: 'POST'});
  const data = await res.json();
  token = data.access_token;
  
  const role = data.roles[0] || 'Staff';
  const rb = document.getElementById('roleBadge');
  rb.innerText = role;
  rb.className = 'badge badge-' + role.toLowerCase().replace(' ', '');
  
  const hb = document.getElementById('hospBadge');
  hb.innerText = 'Hospital ' + data.hospital_id;

  addMsg('👤 Switched persona to <b>' + data.name + '</b> (' + role + ' @ Hospital ' + data.hospital_id + ')', 'msg-bot');
}

function clearChat() {
  document.getElementById('chat').innerHTML = '';
  document.getElementById('previewArea').style.display = 'none';
}

function runPrompt(txt) {
  document.getElementById('inputQuery').value = txt;
  sendQuery();
}

function addMsg(htmlContent, cls) {
  const chat = document.getElementById('chat');
  const el = document.createElement('div');
  el.className = 'msg ' + cls;
  el.innerHTML = htmlContent;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

async function sendQuery() {
  const inp = document.getElementById('inputQuery');
  const q = inp.value.trim();
  if (!q) return;
  addMsg(escapeHtml(q), 'msg-user');
  inp.value = '';

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
      body: JSON.stringify({message: q})
    });
    const data = await res.json();

    if (data.exit === 'refuse') {
      let content = '⛔ <b>REFUSED BY POLICY:</b><br>' + escapeHtml(data.reason);
      if (data.reason && data.reason.includes('#REQ-')) {
        const match = data.reason.match(/#REQ-\d+/);
        if (match) {
          const req = match[0].replace('#', '');
          content += `<br><br><button class="btn-success" style="font-size:12px;padding:4px 10px;" onclick="runPrompt('Approve request #${req}')">Approve as Admin (${req})</button>`;
        }
      }
      addMsg(content, 'msg-bot msg-refuse');
    } else if (data.exit === 'clarify') {
      addMsg('❓ <b>CLARIFICATION REQUIRED:</b><br>' + escapeHtml(data.question), 'msg-bot msg-clarify');
    } else if (data.exit === 'answer') {
      let content = '✅ <b>' + (data.decision || 'ANSWER') + ':</b><br>' + (data.answer || 'Operation completed.');
      if (data.answer && data.answer.includes('#REQ-')) {
        const match = data.answer.match(/#REQ-\d+/);
        if (match) {
          const req = match[0].replace('#', '');
          content += `<br><br><button class="btn-success" style="font-size:12px;padding:4px 10px;" onclick="runPrompt('Approve request #${req}')">Approve as Admin (${req})</button>`;
        }
      }
      addMsg(content.replace(/\\n/g, '<br>'), 'msg-bot msg-answer');
    } else if (data.exit === 'preview') {
      showPreview(data.preview, data.message);
    }
  } catch (err) {
    addMsg('⚠️ Error communicating with server: ' + err.message, 'msg-bot msg-refuse');
  }
}

function showPreview(p, reason) {
  activeToken = p.token;
  requiredRows = p.requires_typed_count ? p.rows_affected : 0;

  let samplesHtml = '';
  if (p.samples && p.samples.length > 0) {
    samplesHtml = '<table><thead><tr><th>ID</th><th>Name</th><th>Before</th><th>After</th></tr></thead><tbody>';
    p.samples.forEach(s => {
      samplesHtml += `<tr><td>${s.id || ''}</td><td>${s.name || ''}</td><td>${JSON.stringify(s.before || {})}</td><td>${JSON.stringify(s.after || {})}</td></tr>`;
    });
    samplesHtml += '</tbody></table>';
  }

  let html = `<div class="preview-card">
    <h3 style="color:#1e40af;margin-bottom:6px;">⚠️ Write Preview — Confirmation Required</h3>
    <p style="font-size:13px;color:#334155;margin-bottom:8px;">${reason || 'Operation evaluated successfully. Review details below before applying mutations.'}</p>
    <div style="display:flex;gap:12px;margin:8px 0;font-size:13px;">
      <div><b>Rows Affected:</b> <span style="background:#dbeafe;padding:2px 8px;border-radius:4px;">${p.rows_affected}</span></div>
      <div><b>Total Financial Impact:</b> <span style="background:#dbeafe;padding:2px 8px;border-radius:4px;">₹${(p.total_inr_impact || 0).toLocaleString()}</span></div>
    </div>
    ${samplesHtml}`;

  if (p.requires_typed_count) {
    html += `<div class="blast-guard">
      🚨 <b>Blast-Radius Guard Active:</b> This write touches <b>${p.rows_affected}</b> rows (&gt; 50 threshold).<br>
      To prevent accidental data mutations, please type the exact number <b>${p.rows_affected}</b> to confirm:
      <input type="number" id="typedCount" style="width:110px;margin-left:8px;padding:4px 8px;" placeholder="${p.rows_affected}">
    </div>`;
  }

  html += `<div style="display:flex;gap:10px;margin-top:12px;">
    <button class="btn-success" onclick="confirmAction()">✅ Confirm &amp; Execute</button>
    <button class="btn-danger" onclick="cancelAction()">❌ Cancel</button>
  </div></div>`;

  const pa = document.getElementById('previewArea');
  pa.innerHTML = html;
  pa.style.display = 'block';
  pa.scrollIntoView({behavior: 'smooth'});
}

async function confirmAction() {
  let count = requiredRows ? parseInt(document.getElementById('typedCount').value) : null;
  const res = await fetch('/confirm', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify({token: activeToken, typed_count: count})
  });
  const data = await res.json();
  if (res.ok) {
    addMsg('🎉 <b>WRITE EXECUTED:</b> ' + data.message, 'msg-bot msg-answer');
  } else {
    addMsg('❌ <b>CONFIRMATION REJECTED:</b> ' + (data.detail || 'Confirmation failed'), 'msg-bot msg-refuse');
  }
  document.getElementById('previewArea').style.display = 'none';
}

async function cancelAction() {
  await fetch('/cancel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify({token: activeToken})
  });
  addMsg('ℹ️ Operation cancelled. Zero database modifications were made.', 'msg-bot');
  document.getElementById('previewArea').style.display = 'none';
}

function escapeHtml(text) {
  const map = {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#039;'};
  return (text || '').replace(/[&<>"']/g, m => map[m]);
}

window.onload = switchUser;
</script>
</body>
</html>
"""



@app.get("/", response_class=HTMLResponse)
def index_page():
    """Serves the lightweight chat interface."""
    return HTMLResponse(content=HTML_CONTENT)
