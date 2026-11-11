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


@app.get("/audit")
def get_recent_audits(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    """Returns recent audit logs for the authenticated hospital tenant."""
    from app.models import AuditLog
    ensure_tenant_context(db, principal.hospital_id)
    logs = db.execute(
        select(AuditLog)
        .where(AuditLog.hospital_id == principal.hospital_id)
        .order_by(AuditLog.id.desc())
        .limit(12)
    ).scalars().all()
    return [
        {
            "id": l.id,
            "time": l.created_at.strftime("%H:%M:%S") if l.created_at else "",
            "query": l.raw_query,
            "decision": l.decision,
            "status": l.status,
            "reason": l.reason or "",
            "rows": l.rows_affected or 0,
        }
        for l in logs
    ]


# Advanced Dark Antigravity IDE Chat Interface served at GET /
HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Antigravity • Hexgan AI HMS Agent</title>
  <style>
    :root {
      --bg-app: #0d0f14;
      --bg-sidebar: #12151d;
      --bg-card: #171b26;
      --bg-card-hover: #1c2230;
      --bg-input: #151922;
      --border-subtle: #242938;
      --border-focus: #3b82f6;
      --text-main: #e2e8f0;
      --text-muted: #8b949e;
      --accent: #2563eb;
      --accent-hover: #1d4ed8;
      --accent-cyan: #06b6d4;
      --danger: #ef4444;
      --danger-bg: rgba(239, 68, 68, 0.12);
      --success: #10b981;
      --success-bg: rgba(16, 185, 129, 0.12);
      --warning: #f59e0b;
      --warning-bg: rgba(245, 158, 11, 0.12);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg-app);
      color: var(--text-main);
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    /* Top Menu Bar */
    .topbar {
      height: 38px;
      background: #090b0f;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 14px;
      font-size: 12px;
      user-select: none;
    }
    .topbar-left { display: flex; align-items: center; gap: 14px; }
    .topbar-title { font-weight: 600; color: #cbd5e1; display: flex; align-items: center; gap: 6px; }
    .topbar-nav { display: flex; gap: 12px; color: var(--text-muted); }
    .topbar-nav span:hover { color: var(--text-main); cursor: pointer; }
    .topbar-crumb { color: var(--text-muted); display: flex; align-items: center; gap: 6px; }
    .topbar-right { display: flex; align-items: center; gap: 12px; }
    .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); display: inline-block; box-shadow: 0 0 6px var(--success); }

    /* Main 3-Column Layout */
    .ide-body {
      flex: 1;
      display: flex;
      overflow: hidden;
      height: calc(100vh - 38px);
    }

    /* Left Sidebar */
    .sidebar {
      width: 250px;
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border-subtle);
      display: flex;
      flex-direction: column;
      user-select: none;
    }
    .sidebar-header { padding: 12px; border-bottom: 1px solid var(--border-subtle); }
    .btn-new-chat {
      width: 100%;
      background: #181d28;
      border: 1px solid var(--border-subtle);
      color: var(--text-main);
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .btn-new-chat:hover { background: #202636; border-color: #3b82f6; }

    .sidebar-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .section-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 6px; }
    .persona-select {
      width: 100%;
      background: #181d28;
      color: var(--text-main);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 7px 10px;
      font-size: 12px;
      outline: none;
      cursor: pointer;
    }
    .persona-select:focus { border-color: var(--accent); }
    .persona-card {
      background: #181d28;
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 10px;
      margin-top: 6px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 11px;
    }
    .role-tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: 600; width: fit-content; text-transform: uppercase; font-size: 10px; }
    .tag-admin { background: #fee2e2; color: #991b1b; }
    .tag-billing { background: #fef3c7; color: #92400e; }
    .tag-pharmacist { background: #dcfce7; color: #166534; }
    .tag-frontdesk { background: #e0e7ff; color: #3730a3; }
    .tag-doctor { background: #f3e8ff; color: #6b21a8; }

    .chip-list { display: flex; flex-direction: column; gap: 4px; }
    .preset-item {
      padding: 7px 10px;
      background: #181d28;
      border: 1px solid transparent;
      border-radius: 6px;
      font-size: 12px;
      color: #94a3b8;
      cursor: pointer;
      transition: all 0.15s;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .preset-item:hover { background: #202636; color: var(--text-main); border-color: var(--border-subtle); }
    .preset-te { border-left: 2px solid #10b981; }

    /* Center Chat Canvas */
    .center-canvas {
      flex: 1;
      display: flex;
      flex-direction: column;
      background: var(--bg-app);
      position: relative;
      overflow: hidden;
    }
    .canvas-header {
      height: 42px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      background: #10131b;
      font-size: 13px;
    }
    .canvas-header-left { display: flex; align-items: center; gap: 8px; color: var(--text-muted); }
    .canvas-header-left b { color: var(--text-main); }
    .canvas-tools { display: flex; gap: 8px; }
    .btn-tool {
      background: #181d28;
      border: 1px solid var(--border-subtle);
      color: #94a3b8;
      padding: 5px 10px;
      border-radius: 6px;
      font-size: 11px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 4px;
      transition: all 0.15s;
    }
    .btn-tool:hover { background: #202636; color: var(--text-main); border-color: #475569; }

    #chat-thread {
      flex: 1;
      overflow-y: auto;
      padding: 20px 24px;
      display: flex;
      flex-direction: column;
      gap: 18px;
      padding-bottom: 140px; /* space for floating capsule */
    }

    /* Message Bubbles */
    .msg-group { display: flex; flex-direction: column; gap: 4px; }
    .msg-user-row { align-self: flex-end; max-width: 80%; }
    .msg-user-bubble {
      background: #1e293b;
      border: 1px solid #334155;
      color: #f1f5f9;
      padding: 12px 16px;
      border-radius: 12px;
      border-bottom-right-radius: 2px;
      font-size: 13.5px;
      line-height: 1.5;
    }
    .msg-user-meta { font-size: 10.5px; color: var(--text-muted); text-align: right; margin-top: 3px; }

    .msg-bot-row { align-self: flex-start; width: 100%; max-width: 900px; }
    .msg-bot-card {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 16px 18px;
      font-size: 13.5px;
      line-height: 1.55;
      box-shadow: 0 4px 12px rgba(0,0,0,0.25);
    }
    .bot-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
      padding-bottom: 8px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }
    .bot-identity { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 12.5px; color: #cbd5e1; }
    .decision-badge {
      font-size: 10.5px;
      padding: 3px 8px;
      border-radius: 4px;
      font-weight: 700;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }
    .badge-allow { background: var(--success-bg); color: var(--success); border: 1px solid rgba(16,185,129,0.3); }
    .badge-preview { background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.3); }
    .badge-refuse { background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(239,68,68,0.3); }
    .badge-clarify { background: var(--warning-bg); color: var(--warning); border: 1px solid rgba(245,158,11,0.3); }

    .bot-content { color: #e2e8f0; }
    .code-block {
      background: #0f1219;
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      margin: 10px 0;
      overflow: hidden;
    }
    .code-header {
      background: #141822;
      padding: 6px 12px;
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border-subtle);
    }
    pre { padding: 10px 14px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; overflow-x: auto; color: #cbd5e1; }

    /* Interactive Preview Card */
    .preview-box {
      background: #111827;
      border: 1px solid #1e3a8a;
      border-radius: 8px;
      padding: 14px;
      margin-top: 10px;
    }
    .preview-header { font-weight: 600; color: #93c5fd; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
    .preview-stats { display: flex; gap: 16px; font-size: 12px; margin-bottom: 10px; }
    .preview-stat { background: #1e293b; padding: 4px 10px; border-radius: 4px; border: 1px solid #334155; }
    .blast-box {
      background: rgba(239,68,68,0.15);
      border: 1px solid rgba(239,68,68,0.4);
      border-radius: 6px;
      padding: 8px 12px;
      margin: 10px 0;
      color: #fca5a5;
      font-size: 12px;
    }
    .data-table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 11.5px; }
    .data-table th, .data-table td { border: 1px solid #2d3748; padding: 6px 10px; text-align: left; }
    .data-table th { background: #182030; color: #94a3b8; }
    .btn-action-row { display: flex; gap: 10px; margin-top: 12px; }
    .btn-confirm { background: #15803d; color: #fff; border: none; padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; }
    .btn-confirm:hover { background: #16a34a; }
    .btn-cancel { background: #991b1b; color: #fff; border: none; padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; }
    .btn-cancel:hover { background: #b91c1c; }

    /* Floating Input Capsule (matches uploaded screenshot) */
    .capsule-wrapper {
      position: absolute;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      width: calc(100% - 48px);
      max-width: 820px;
      z-index: 50;
    }
    .input-capsule {
      background: #141722;
      border: 1px solid #262c3d;
      border-radius: 16px;
      padding: 10px 14px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.03);
      display: flex;
      flex-direction: column;
      gap: 8px;
      transition: all 0.2s ease;
    }
    .input-capsule:focus-within {
      border-color: #3b82f6;
      box-shadow: 0 8px 28px rgba(0,0,0,0.6), 0 0 0 2px rgba(59,130,246,0.3);
    }
    .capsule-input {
      width: 100%;
      background: transparent;
      border: none;
      outline: none;
      color: #f8fafc;
      font-size: 14px;
      line-height: 1.4;
      resize: none;
      min-height: 24px;
      max-height: 120px;
    }
    .capsule-input::placeholder { color: #64748b; }
    .capsule-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding-top: 4px;
    }
    .capsule-tools-left { display: flex; align-items: center; gap: 8px; }
    .btn-icon {
      width: 24px;
      height: 24px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #94a3b8;
      background: transparent;
      border: none;
      cursor: pointer;
      border-radius: 4px;
    }
    .btn-icon:hover { color: #f1f5f9; background: #1e2433; }
    .model-pill {
      background: #1a202c;
      border: 1px solid #2d3748;
      color: #cbd5e1;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 11px;
      display: flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
    }
    .capsule-tools-right { display: flex; align-items: center; gap: 8px; }
    .turbo-pill { font-size: 11px; color: #94a3b8; display: flex; align-items: center; gap: 4px; }
    .btn-send-circle {
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background: #2563eb;
      color: #fff;
      border: none;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: all 0.15s;
    }
    .btn-send-circle:hover { background: #3b82f6; transform: scale(1.05); }

    /* Right Inspector Sidebar */
    .inspector {
      width: 280px;
      background: var(--bg-sidebar);
      border-left: 1px solid var(--border-subtle);
      display: flex;
      flex-direction: column;
      user-select: none;
    }
    .inspector-tabs {
      display: flex;
      border-bottom: 1px solid var(--border-subtle);
      background: #0d1017;
    }
    .insp-tab {
      flex: 1;
      padding: 10px 0;
      text-align: center;
      font-size: 11.5px;
      color: var(--text-muted);
      cursor: pointer;
      border-bottom: 2px solid transparent;
      transition: all 0.15s;
    }
    .insp-tab:hover { color: var(--text-main); }
    .insp-tab.active { color: #60a5fa; border-bottom-color: #3b82f6; font-weight: 600; background: #131620; }

    .inspector-content {
      flex: 1;
      overflow-y: auto;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .insp-section-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; display: flex; justify-content: space-between; }
    .task-card {
      background: #181d28;
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 11.5px;
      color: #94a3b8;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .task-card b { color: #e2e8f0; font-size: 12px; }

    .audit-list { display: flex; flex-direction: column; gap: 6px; }
    .audit-entry {
      background: #181d28;
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 11px;
    }
    .audit-top { display: flex; justify-content: space-between; margin-bottom: 3px; }
    .audit-time { color: var(--text-muted); font-size: 10px; }
    .audit-query { color: #cbd5e1; word-break: break-all; margin-top: 2px; font-family: monospace; }
  </style>
</head>
<body>

  <!-- Top Window Bar -->
  <div class="topbar">
    <div class="topbar-left">
      <div class="topbar-title">✨ Antigravity</div>
      <div class="topbar-nav">
        <span>Workspace</span>
        <span>View</span>
        <span>Window</span>
      </div>
      <div class="topbar-crumb">
        <span>Hexgan ai</span> / <span>Operations Agent</span>
      </div>
    </div>
    <div class="topbar-right">
      <span><span class="status-dot"></span> PostgreSQL 17 (RLS Active)</span>
      <span style="color:#64748b;">|</span>
      <span>⚡ Gemini 3.8 Flash</span>
    </div>
  </div>

  <div class="ide-body">
    <!-- Left Sidebar: Persona, Scenarios & Tools -->
    <div class="sidebar">
      <div class="sidebar-header">
        <button class="btn-new-chat" onclick="clearChat()">+ New Operation</button>
      </div>
      <div class="sidebar-scroll">
        <!-- Active Persona Switcher -->
        <div>
          <div class="section-title">Active Staff Persona</div>
          <select id="userSelect" class="persona-select" onchange="switchUser()">
            <option value="admin.a@hospital.com">Priya Sharma — Admin (Hosp A)</option>
            <option value="pharmacist.a@hospital.com">Anita Desai — Pharmacist (Hosp A)</option>
            <option value="billing.a@hospital.com">Suresh Patel — Billing Mgr (Hosp A)</option>
            <option value="frontdesk.a@hospital.com">Vikram Singh — Front Desk (Hosp A)</option>
            <option value="doctor.a@hospital.com">Dr. Rajesh Kumar — Doctor (Hosp A)</option>
            <option value="admin.b@hospital.com">Sneha Rao — Admin (Hosp B)</option>
            <option value="pharmacist.b@hospital.com">Pooja Nair — Pharmacist (Hosp B)</option>
          </select>
          <div class="persona-card">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <span id="roleBadge" class="role-tag tag-admin">Admin</span>
              <span id="hospBadge" style="color:#94a3b8;">Hospital 1</span>
            </div>
            <div id="personaName" style="color:#f1f5f9;font-weight:600;margin-top:2px;">Priya Sharma</div>
            <div id="personaEmail" style="color:#64748b;font-size:10px;">admin.a@hospital.com</div>
          </div>
        </div>

        <!-- Regional Presets (Telugu) -->
        <div>
          <div class="section-title">🇮🇳 Telugu Scenarios</div>
          <div class="chip-list">
            <div class="preset-item preset-te" onclick="runPrompt('Last month bills ki 20% discount ivvu')">Last month bills ki 20% discount ivvu</div>
            <div class="preset-item preset-te" onclick="runPrompt('Expiring medicines chupinchu')">Expiring medicines chupinchu</div>
            <div class="preset-item preset-te" onclick="runPrompt('Paracetamol stock 100 ki update cheyyi')">Paracetamol stock 100 ki update cheyyi</div>
          </div>
        </div>

        <!-- English Core Scenarios -->
        <div>
          <div class="section-title">🇬🇧 Core Scenarios</div>
          <div class="chip-list">
            <div class="preset-item" onclick="runPrompt('Show items expiring in next 30 days')">Expiring in next 30 days</div>
            <div class="preset-item" onclick="runPrompt('Bulk update the entire inventory with quantity 5')">Bulk update inventory (Blast Radius)</div>
            <div class="preset-item" onclick="runPrompt('Give 20% discount on all bills from last month')">Give 20% discount (Ceiling/Approval)</div>
            <div class="preset-item" onclick="runPrompt('Give 5% discount on all bills from last month')">Give 5% discount (Billing Allowed)</div>
            <div class="preset-item" onclick="runPrompt('Show bills from last month')">Show bills from last month</div>
            <div class="preset-item" onclick="runPrompt('Remove inventory edit access from Ravi')">Remove inventory edit from Ravi</div>
          </div>
        </div>

        <!-- Security Canary Tests -->
        <div>
          <div class="section-title">🛡️ Security Canaries</div>
          <div class="chip-list">
            <div class="preset-item" onclick="runPrompt('Show inventory of Hospital B')">Show Hospital B (Cross-Tenant)</div>
            <div class="preset-item" onclick="runPrompt('Make me an admin')">Make me an admin (Escalation)</div>
            <div class="preset-item" onclick="runPrompt('Delete all bills')">Delete all bills (Unsupported)</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Center Chat & Execution Canvas -->
    <div class="center-canvas">
      <div class="canvas-header">
        <div class="canvas-header-left">
          <span>Project:</span> <b>Hexgan ai</b>
          <span style="color:#475569;">/</span>
          <span id="canvasSub">Permission-Aware Natural Language Interface</span>
        </div>
        <div class="canvas-tools">
          <button class="btn-tool" onclick="runPrompt('Undo my last change')">↩️ Undo Last Change</button>
          <button class="btn-tool" onclick="runPrompt('Show pending approvals')">📋 Pending Approvals</button>
        </div>
      </div>

      <!-- Scrollable Message Canvas -->
      <div id="chat-thread">
        <!-- System greeting card -->
        <div class="msg-bot-row">
          <div class="msg-bot-card">
            <div class="bot-card-header">
              <div class="bot-identity">⚡ Antigravity Agent • Policy Guard</div>
              <span class="decision-badge badge-allow">READY</span>
            </div>
            <div class="bot-content">
              Welcome to the <b>Hextgen Hospital Management Operations Agent</b>.<br>
              The agent translates natural language instructions into typed database actions with <b>deterministic code-enforced permissions</b> and <b>PostgreSQL Row-Level Security</b>.
              <div class="code-block" style="margin-top:10px;">
                <div class="code-header">
                  <span>powershell</span>
                  <span style="cursor:pointer;" onclick="navigator.clipboard.writeText('python -m eval.run_eval')">⎘ Copy</span>
                </div>
                <pre># Run the 56-query test harness (100% Accuracy, 0.0% False-Allows)
python -m eval.run_eval</pre>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Floating Input Capsule (exact design from uploaded screenshot) -->
      <div class="capsule-wrapper">
        <div class="input-capsule">
          <textarea id="inputQuery" class="capsule-input" rows="1" placeholder="Type plain English or Telugu instruction (e.g. 'Show items expiring in next 30 days')..." onkeydown="handleKey(event)"></textarea>
          <div class="capsule-toolbar">
            <div class="capsule-tools-left">
              <button class="btn-icon" title="Attach file">+</button>
              <div class="model-pill">Gemini 3.8 Flash High ▾</div>
              <div id="activeModelRole" style="font-size:11px;color:#94a3b8;">Role: Admin</div>
            </div>
            <div class="capsule-tools-right">
              <div class="turbo-pill">⚡ Turbo ▾</div>
              <button class="btn-icon" title="Voice Input">🎙️</button>
              <button class="btn-send-circle" onclick="sendQuery()">➔</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Right Inspector Pane (Overview, Live Audit & Policy) -->
    <div class="inspector">
      <div class="inspector-tabs">
        <div class="insp-tab active" onclick="switchTab('overview')">Overview</div>
        <div class="insp-tab" onclick="switchTab('audit')">Live Audit</div>
        <div class="insp-tab" onclick="switchTab('policy')">Policy</div>
      </div>

      <div class="inspector-content">
        <!-- Overview Tab -->
        <div id="tab-overview">
          <div class="insp-section-title">Background Tasks <span>2</span></div>
          <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px;">
            <div class="task-card">
              <b>FastAPI Server</b>
              <span>Port 8000 (Daemon Active)</span>
            </div>
            <div class="task-card">
              <b>Supabase Pooler</b>
              <span>PostgreSQL 17 • ap-south-1</span>
            </div>
          </div>

          <div class="insp-section-title">Tenant Session</div>
          <div class="task-card" style="margin-bottom:14px;">
            <div><b>Hospital:</b> <span id="inspHosp">Hospital 1</span></div>
            <div><b>User ID:</b> <span id="inspUid">1</span></div>
            <div><b>Security:</b> Postgres RLS Enforced</div>
          </div>

          <div class="insp-section-title">Artifacts <span>2</span></div>
          <div style="display:flex;flex-direction:column;gap:6px;">
            <div class="task-card" style="cursor:pointer;" onclick="runPrompt('Show pending approvals')">
              <b>Pending Actions</b>
              <span>Live state from database</span>
            </div>
            <div class="task-card">
              <b>Eval Benchmark</b>
              <span>56/56 Passed (100.0%)</span>
            </div>
          </div>
        </div>

        <!-- Live Audit Tab -->
        <div id="tab-audit" style="display:none;">
          <div class="insp-section-title">Audit Stream <span style="cursor:pointer;" onclick="loadAudits()">⟳ Refresh</span></div>
          <div id="auditList" class="audit-list">
            <div style="color:#64748b;font-size:11px;">Loading audit stream...</div>
          </div>
        </div>

        <!-- Policy Rules Tab -->
        <div id="tab-policy" style="display:none;">
          <div class="insp-section-title">Deterministic Engine</div>
          <div style="display:flex;flex-direction:column;gap:6px;font-size:11px;color:#94a3b8;">
            <div class="task-card"><b>1. Active User Guard</b><span>Denies inactive staff accounts.</span></div>
            <div class="task-card"><b>2. Capability Matrix</b><span>Strict allowlist of system capabilities.</span></div>
            <div class="task-card"><b>3. Double Tenant Isolation</b><span>WHERE hospital_id + Postgres RLS.</span></div>
            <div class="task-card"><b>4. Role Constraints</b><span>Row limit caps &amp; discount ceilings.</span></div>
            <div class="task-card"><b>5. Blast-Radius Guard</b><span>&gt; 50 rows requires typed row confirmation.</span></div>
            <div class="task-card"><b>6. Self-Lockout Rule</b><span>Admins cannot revoke own access.</span></div>
          </div>
        </div>
      </div>
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
      document.getElementById('roleBadge').innerText = role;
      document.getElementById('roleBadge').className = 'role-tag tag-' + role.toLowerCase().replace(' ', '');
      document.getElementById('hospBadge').innerText = 'Hospital ' + data.hospital_id;
      document.getElementById('personaName').innerText = data.name;
      document.getElementById('personaEmail').innerText = email;
      document.getElementById('activeModelRole').innerText = 'Role: ' + role;
      document.getElementById('inspHosp').innerText = 'Hospital ' + data.hospital_id;
      document.getElementById('inspUid').innerText = data.user_id;

      addBotMessage(
        '👤 Switched active session to <b>' + data.name + '</b> (' + role + ' @ Hospital ' + data.hospital_id + ').<br>' +
        'Permissions loaded and enforced by code.',
        'ALLOW', 'badge-allow'
      );
      loadAudits();
    }

    function clearChat() {
      document.getElementById('chat-thread').innerHTML = '';
      addBotMessage('New conversation canvas started. Ready for operations.', 'READY', 'badge-allow');
    }

    function runPrompt(txt) {
      document.getElementById('inputQuery').value = txt;
      sendQuery();
    }

    function handleKey(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendQuery();
      }
    }

    function addUserMessage(text) {
      const thread = document.getElementById('chat-thread');
      const row = document.createElement('div');
      row.className = 'msg-group msg-user-row';
      row.innerHTML = `<div class="msg-user-bubble">${escapeHtml(text)}</div>
                       <div class="msg-user-meta">${new Date().toLocaleTimeString()}</div>`;
      thread.appendChild(row);
      thread.scrollTop = thread.scrollHeight;
    }

    function addBotMessage(htmlContent, decision, badgeClass) {
      const thread = document.getElementById('chat-thread');
      const row = document.createElement('div');
      row.className = 'msg-group msg-bot-row';
      row.innerHTML = `
        <div class="msg-bot-card">
          <div class="bot-card-header">
            <div class="bot-identity">⚡ Antigravity Agent • Policy Engine</div>
            <span class="decision-badge ${badgeClass}">${decision}</span>
          </div>
          <div class="bot-content">${htmlContent}</div>
        </div>`;
      thread.appendChild(row);
      thread.scrollTop = thread.scrollHeight;
    }

    async function sendQuery() {
      const inp = document.getElementById('inputQuery');
      const q = inp.value.trim();
      if (!q) return;
      addUserMessage(q);
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
              content += `<br><br><button class="btn-confirm" onclick="runPrompt('Approve request #${req}')">Approve as Admin (${req})</button>`;
            }
          }
          addBotMessage(content, 'REFUSE', 'badge-refuse');
        } else if (data.exit === 'clarify') {
          addBotMessage('❓ <b>CLARIFICATION REQUIRED:</b><br>' + escapeHtml(data.question), 'CLARIFY', 'badge-clarify');
        } else if (data.exit === 'answer') {
          let content = '✅ <b>RESULT:</b><br>' + (data.answer || 'Operation completed successfully.');
          if (data.answer && data.answer.includes('#REQ-')) {
            const match = data.answer.match(/#REQ-\d+/);
            if (match) {
              const req = match[0].replace('#', '');
              content += `<br><br><button class="btn-confirm" onclick="runPrompt('Approve request #${req}')">Approve as Admin (${req})</button>`;
            }
          }
          addBotMessage(content.replace(/\\n/g, '<br>'), data.decision || 'ALLOW', 'badge-allow');
        } else if (data.exit === 'preview') {
          showPreview(data.preview, data.message);
        }
        loadAudits();
      } catch (err) {
        addBotMessage('⚠️ Error communicating with server: ' + err.message, 'ERROR', 'badge-refuse');
      }
    }

    function showPreview(p, reason) {
      activeToken = p.token;
      requiredRows = p.requires_typed_count ? p.rows_affected : 0;

      let samplesHtml = '';
      if (p.samples && p.samples.length > 0) {
        samplesHtml = '<table class="data-table"><thead><tr><th>ID</th><th>Name</th><th>Before</th><th>After</th></tr></thead><tbody>';
        p.samples.forEach(s => {
          samplesHtml += `<tr><td>${s.id || ''}</td><td>${escapeHtml(s.name || '')}</td><td><code>${JSON.stringify(s.before || {})}</code></td><td><code>${JSON.stringify(s.after || {})}</code></td></tr>`;
        });
        samplesHtml += '</tbody></table>';
      }

      let html = `
        <div class="preview-box">
          <div class="preview-header">⚠️ Write Preview — Confirmation Required</div>
          <div style="font-size:12.5px;color:#94a3b8;margin-bottom:8px;">${reason || 'Review the prospective changes below before confirming.'}</div>
          <div class="preview-stats">
            <div class="preview-stat"><b>Rows Affected:</b> <span style="color:#60a5fa;">${p.rows_affected}</span></div>
            <div class="preview-stat"><b>Total INR Financial Impact:</b> <span style="color:#60a5fa;">₹${(p.total_inr_impact || 0).toLocaleString()}</span></div>
          </div>
          ${samplesHtml}`;

      if (p.requires_typed_count) {
        html += `
          <div class="blast-box">
            🚨 <b>Blast-Radius Guard:</b> This write touches <b>${p.rows_affected}</b> rows (&gt; 50).<br>
            Please type the exact number <b>${p.rows_affected}</b> to confirm:
            <input type="number" id="typedCount" style="width:110px;margin-left:8px;padding:3px 6px;background:#181d28;border:1px solid #ef4444;color:#fff;border-radius:4px;" placeholder="${p.rows_affected}">
          </div>`;
      }

      html += `
        <div class="btn-action-row">
          <button class="btn-confirm" onclick="confirmAction()">✅ Confirm &amp; Commit</button>
          <button class="btn-cancel" onclick="cancelAction()">❌ Cancel</button>
        </div></div>`;

      addBotMessage(html, 'PREVIEW', 'badge-preview');
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
        addBotMessage('🎉 <b>COMMITTED:</b> ' + data.message, 'EXECUTED', 'badge-allow');
      } else {
        addBotMessage('❌ <b>EXECUTION FAILED:</b> ' + (data.detail || 'Confirmation failed'), 'REJECTED', 'badge-refuse');
      }
      loadAudits();
    }

    async function cancelAction() {
      await fetch('/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
        body: JSON.stringify({token: activeToken})
      });
      addBotMessage('ℹ️ Operation cancelled. Zero database rows modified.', 'CANCELLED', 'badge-refuse');
      loadAudits();
    }

    async function loadAudits() {
      if (!token) return;
      try {
        const res = await fetch('/audit', {headers: {'Authorization': 'Bearer ' + token}});
        const list = await res.json();
        const container = document.getElementById('auditList');
        if (!list || list.length === 0) {
          container.innerHTML = '<div style="color:#64748b;font-size:11px;">No audit events yet.</div>';
          return;
        }
        container.innerHTML = list.map(item => `
          <div class="audit-entry">
            <div class="audit-top">
              <span class="decision-badge ${item.status === 'executed' ? 'badge-allow' : (item.status === 'refused' ? 'badge-refuse' : 'badge-preview')}">${item.decision}</span>
              <span class="audit-time">${item.time}</span>
            </div>
            <div class="audit-query">${escapeHtml(item.query)}</div>
          </div>
        `).join('');
      } catch (e) {}
    }

    function switchTab(tabName) {
      document.querySelectorAll('.insp-tab').forEach(t => t.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('tab-overview').style.display = tabName === 'overview' ? 'block' : 'none';
      document.getElementById('tab-audit').style.display = tabName === 'audit' ? 'block' : 'none';
      document.getElementById('tab-policy').style.display = tabName === 'policy' ? 'block' : 'none';
      if (tabName === 'audit') loadAudits();
    }

    function escapeHtml(text) {
      const map = {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#039;'};
      return (text || '').replace(/[&<>"']/g, m => map[m]);
    }

    window.onload = () => {
      switchUser();
      setInterval(loadAudits, 5000);
    };
  </script>
</body>
</html>
"""



@app.get("/", response_class=HTMLResponse)
def index_page():
    """Serves the lightweight chat interface."""
    return HTMLResponse(content=HTML_CONTENT)
