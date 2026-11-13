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
from app.agent.graph import execute_agent_graph

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
    Main agent chat endpoint orchestrated by LangGraph StateGraph workflow.
    Guarantees:
    - LLM only proposes an intent (identity never passed to or trusted from LLM)
    - Code gathers facts and deterministic policy engine decides
    - Audit log recorded in terminal graph node for 100% of exits
    - Multi-tenant isolated retrieval system for hospital SOPs and drug formulary
    """
    ensure_tenant_context(db, principal.hospital_id)
    return execute_agent_graph(
        raw_query=req.message,
        principal=principal,
        parser=parser,
        db=db,
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
  <title>Hextgen HMS — Operations Agent</title>
  <style>
    :root {
      --primary: #2563eb;
      --primary-dark: #1d4ed8;
      --primary-light: #eff6ff;
      --sidebar-bg: #0f172a;
      --sidebar-card: #1e293b;
      --sidebar-text: #f8fafc;
      --sidebar-muted: #94a3b8;
      --border-dark: #334155;
      --bg: #f1f5f9;
      --card-bg: #ffffff;
      --border: #e2e8f0;
      --text: #0f172a;
      --text-muted: #64748b;
      --danger: #ef4444;
      --danger-bg: #fef2f2;
      --danger-border: #fecaca;
      --success: #10b981;
      --success-bg: #ecfdf5;
      --success-border: #a7f3d0;
      --warning: #f59e0b;
      --warning-bg: #fffbeb;
      --warning-border: #fde68a;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); height: 100vh; overflow: hidden; display: flex; flex-direction: column; }
    
    /* Layout */
    .app-layout { display: flex; height: 100vh; width: 100vw; overflow: hidden; }
    
    /* Left Sidebar */
    .sidebar { width: 370px; background: var(--sidebar-bg); color: var(--sidebar-text); display: flex; flex-direction: column; border-right: 1px solid var(--border-dark); padding: 18px; gap: 14px; overflow-y: auto; flex-shrink: 0; }
    .brand { display: flex; align-items: center; justify-content: space-between; padding-bottom: 12px; border-bottom: 1px solid var(--border-dark); }
    .brand-title { font-size: 17px; font-weight: 700; display: flex; align-items: center; gap: 8px; color: #fff; }
    .pulse-indicator { width: 8px; height: 8px; border-radius: 50%; background: var(--success); box-shadow: 0 0 8px var(--success); animation: pulse 2s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.9); } }
    
    /* Persona Card */
    .persona-card { background: var(--sidebar-card); border-radius: 12px; border: 1px solid var(--border-dark); padding: 14px; display: flex; flex-direction: column; gap: 12px; }
    .persona-header { display: flex; align-items: center; gap: 12px; }
    .avatar { width: 44px; height: 44px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; color: #fff; background: linear-gradient(135deg, #3b82f6, #1d4ed8); flex-shrink: 0; }
    .persona-meta { flex: 1; min-width: 0; }
    .persona-name { font-size: 15px; font-weight: 600; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .persona-sub { font-size: 12px; color: var(--sidebar-muted); display: flex; gap: 6px; align-items: center; margin-top: 2px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; }
    .badge-admin { background: #991b1b; color: #fecaca; }
    .badge-billing { background: #92400e; color: #fde68a; }
    .badge-pharmacist { background: #065f46; color: #a7f3d0; }
    .badge-frontdesk { background: #3730a3; color: #c7d2fe; }
    .badge-doctor { background: #6b21a8; color: #e9d5ff; }
    .badge-hospital { background: #334155; color: #cbd5e1; }
    
    /* Persona Selector */
    select.persona-select { width: 100%; padding: 8px 10px; background: #0f172a; border: 1px solid var(--border-dark); border-radius: 8px; color: #f8fafc; font-size: 13px; outline: none; cursor: pointer; }
    select.persona-select:focus { border-color: var(--primary); }
    
    /* Permissions Checklist */
    .perms-box { background: rgba(15, 23, 42, 0.6); border-radius: 8px; padding: 10px; border: 1px solid rgba(51, 65, 85, 0.5); font-size: 12px; }
    .perms-title { font-weight: 600; color: var(--sidebar-muted); text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; margin-bottom: 6px; }
    .perm-item { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; color: #cbd5e1; }
    .perm-item.denied { color: #94a3b8; text-decoration: line-through; opacity: 0.65; }
    
    /* Sidebar Actions */
    .sidebar-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .side-btn { padding: 9px 12px; border-radius: 8px; font-size: 12px; font-weight: 500; cursor: pointer; border: 1px solid var(--border-dark); background: var(--sidebar-card); color: #f8fafc; transition: all 0.15s ease; display: flex; align-items: center; justify-content: center; gap: 6px; }
    .side-btn:hover { background: #334155; border-color: #475569; }
    .side-btn-full { grid-column: span 2; }
    
    /* Audit Log Stream */
    .audit-section { flex: 1; min-height: 180px; display: flex; flex-direction: column; background: var(--sidebar-card); border-radius: 12px; border: 1px solid var(--border-dark); padding: 12px; overflow: hidden; }
    .audit-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid rgba(51, 65, 85, 0.5); }
    .audit-header span { font-size: 11px; font-weight: 600; color: var(--sidebar-muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .audit-feed { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .audit-item { padding: 6px 8px; border-radius: 6px; background: rgba(15, 23, 42, 0.5); border-left: 3px solid #3b82f6; display: flex; flex-direction: column; gap: 2px; }
    .audit-item.refused { border-left-color: var(--danger); }
    .audit-item.executed { border-left-color: var(--success); }
    .audit-item.previewed { border-left-color: var(--warning); }
    .audit-row-top { display: flex; justify-content: space-between; color: var(--sidebar-muted); font-size: 10px; }
    .audit-row-msg { color: #e2e8f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    /* Right Main Console */
    .main-console { flex: 1; display: flex; flex-direction: column; height: 100vh; overflow: hidden; background: #fff; }
    
    /* Top Navigation */
    .top-navbar { height: 58px; background: #fff; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 20px; flex-shrink: 0; }
    .top-breadcrumbs { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-muted); }
    .top-breadcrumbs b { color: var(--text); }
    .system-status { display: flex; align-items: center; gap: 14px; font-size: 12px; }
    .status-tag { display: flex; align-items: center; gap: 6px; color: #475569; background: #f8fafc; padding: 4px 10px; border-radius: 20px; border: 1px solid var(--border); }

    /* Quick Scenarios Tray */
    .scenarios-tray { background: #f8fafc; border-bottom: 1px solid var(--border); padding: 10px 20px; display: flex; flex-direction: column; gap: 6px; flex-shrink: 0; }
    .scenario-tabs { display: flex; gap: 6px; }
    .tab-btn { font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; background: transparent; color: var(--text-muted); }
    .tab-btn.active { background: #e2e8f0; color: var(--text); border-color: #cbd5e1; }
    .chips-container { display: flex; flex-wrap: wrap; gap: 6px; }
    .chip { font-size: 12px; padding: 5px 11px; background: #fff; border: 1px solid var(--border); border-radius: 16px; cursor: pointer; color: #334155; transition: all 0.15s ease; box-shadow: 0 1px 2px rgba(0,0,0,0.02); }
    .chip:hover { background: #f1f5f9; border-color: #cbd5e1; transform: translateY(-1px); color: var(--primary); }
    .chip-te { background: #ecfdf5; border-color: #a7f3d0; color: #065f46; }
    .chip-te:hover { background: #d1fae5; border-color: #6ee7b7; color: #047857; }
    .chip-sec { background: #fef2f2; border-color: #fecaca; color: #991b1b; }
    .chip-sec:hover { background: #fee2e2; border-color: #fca5a5; }

    /* Chat Canvas */
    .chat-canvas { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; background: #f8fafc; }
    .msg-wrap { display: flex; gap: 12px; max-width: 88%; }
    .msg-wrap.user { align-self: flex-end; flex-direction: row-reverse; }
    .msg-wrap.bot { align-self: flex-start; }
    .msg-avatar { width: 34px; height: 34px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; font-weight: 700; color: #fff; }
    .msg-avatar.user-av { background: var(--primary); }
    .msg-avatar.bot-av { background: #0f172a; }
    .bubble { border-radius: 12px; padding: 12px 16px; font-size: 14px; line-height: 1.5; box-shadow: 0 1px 3px rgba(0,0,0,0.04); position: relative; }
    .bubble.user-bubble { background: var(--primary); color: #fff; border-top-right-radius: 2px; }
    .bubble.bot-bubble { background: #fff; border: 1px solid var(--border); border-top-left-radius: 2px; width: 100%; }
    .bubble-meta { font-size: 11px; margin-top: 6px; display: flex; justify-content: space-between; align-items: center; opacity: 0.75; }
    
    /* Structured Bot Cards */
    .status-card { border-radius: 8px; padding: 12px; margin-top: 4px; }
    .card-refuse { background: var(--danger-bg); border: 1px solid var(--danger-border); color: #991b1b; }
    .card-refuse h4 { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; font-size: 13px; font-weight: 700; }
    .card-clarify { background: var(--warning-bg); border: 1px solid var(--warning-border); color: #92400e; }
    .card-clarify h4 { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; font-size: 13px; font-weight: 700; }
    .card-answer { background: #fff; border-left: 4px solid var(--success); padding: 4px 10px; }
    
    /* Preview Modal/Card */
    .preview-box { background: #f0f7ff; border: 1px solid #bfdbfe; border-radius: 10px; padding: 16px; margin-top: 8px; }
    .preview-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
    .preview-header h4 { font-size: 14px; font-weight: 700; color: #1e40af; display: flex; align-items: center; gap: 6px; }
    .metrics-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 12px; }
    .metric-card { background: #fff; border: 1px solid #dbeafe; border-radius: 8px; padding: 8px 12px; }
    .metric-label { font-size: 10px; font-weight: 600; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px; }
    .metric-value { font-size: 18px; font-weight: 700; color: #1e3a8a; margin-top: 2px; }
    
    /* Blast Radius Interactive Guard */
    .blast-box { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 12px; margin: 12px 0; color: #991b1b; }
    .blast-box h5 { font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
    .blast-input-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
    .blast-input { padding: 6px 10px; border: 2px solid #f87171; border-radius: 6px; font-size: 14px; font-weight: 600; width: 140px; outline: none; background: #fff; }
    .blast-input:focus { border-color: #dc2626; box-shadow: 0 0 0 3px rgba(220,38,38,0.15); }

    /* Tables */
    .diff-table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; background: #fff; border-radius: 6px; overflow: hidden; }
    .diff-table th, .diff-table td { border: 1px solid #e2e8f0; padding: 6px 10px; text-align: left; }
    .diff-table th { background: #f8fafc; font-weight: 600; color: #475569; }

    /* Action Buttons */
    .btn-group { display: flex; gap: 8px; margin-top: 12px; }
    button.btn-primary { background: var(--primary); color: #fff; border: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.15s ease; }
    button.btn-primary:hover { background: var(--primary-dark); }
    button.btn-primary:disabled { background: #94a3b8; cursor: not-allowed; opacity: 0.7; }
    button.btn-danger { background: var(--danger); color: #fff; border: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
    button.btn-danger:hover { background: #dc2626; }
    button.btn-action-pill { background: #10b981; color: #fff; border: none; padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.15s; display: inline-flex; align-items: center; gap: 4px; margin-top: 8px; }
    button.btn-action-pill:hover { background: #059669; transform: translateY(-1px); }

    /* Input Composer */
    .composer-bar { background: #fff; border-top: 1px solid var(--border); padding: 14px 20px; display: flex; gap: 10px; align-items: center; }
    .composer-input-wrap { flex: 1; display: flex; align-items: center; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 24px; padding: 0 16px; transition: all 0.15s; }
    .composer-input-wrap:focus-within { border-color: var(--primary); background: #fff; box-shadow: 0 0 0 3px rgba(37,99,235,0.15); }
    .composer-input { width: 100%; border: none; background: transparent; padding: 12px 0; font-size: 14px; outline: none; }
    .send-btn { width: 42px; height: 42px; border-radius: 50%; background: var(--primary); color: #fff; border: none; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: all 0.15s ease; flex-shrink: 0; }
    .send-btn:hover { background: var(--primary-dark); transform: scale(1.05); }
  </style>
</head>
<body>
<div class="app-layout">
  <!-- LEFT SIDEBAR -->
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-title">
        <span>🏥</span> HEXTGEN HMS
      </div>
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--sidebar-muted);">
        <div class="pulse-indicator"></div> LIVE
      </div>
    </div>

    <!-- Active Persona Widget -->
    <div class="persona-card">
      <div class="persona-header">
        <div class="avatar" id="avatarBox">PS</div>
        <div class="persona-meta">
          <div class="persona-name" id="userName">Priya Sharma</div>
          <div class="persona-sub">
            <span class="badge badge-admin" id="userRoleBadge">Admin</span>
            <span class="badge badge-hospital" id="userHospBadge">Hospital 1</span>
          </div>
        </div>
      </div>
      
      <div>
        <label style="font-size:10px;text-transform:uppercase;color:var(--sidebar-muted);font-weight:600;display:block;margin-bottom:4px;">Switch Active Persona</label>
        <select class="persona-select" id="userSelect" onchange="switchUser()">
          <option value="admin.a@hospital.com">Priya Sharma — Admin (Hosp A)</option>
          <option value="pharmacist.a@hospital.com">Anita Desai — Pharmacist (Hosp A)</option>
          <option value="billing.a@hospital.com">Suresh Patel — Billing Mgr (Hosp A)</option>
          <option value="frontdesk.a@hospital.com">Vikram Singh — Front Desk (Hosp A)</option>
          <option value="doctor.a@hospital.com">Dr. Rajesh Kumar — Doctor (Hosp A)</option>
          <option value="admin.b@hospital.com">Sneha Rao — Admin (Hosp B)</option>
          <option value="pharmacist.b@hospital.com">Pooja Nair — Pharmacist (Hosp B)</option>
        </select>
      </div>

      <!-- Active Permissions Checklist -->
      <div class="perms-box">
        <div class="perms-title">Effective Role Permissions &amp; Limits</div>
        <div id="permsList"></div>
      </div>
    </div>

    <!-- Quick Operations Toolbar -->
    <div class="sidebar-actions">
      <button class="side-btn" onclick="runPrompt('Undo my last change')">↩️ Undo Change</button>
      <button class="side-btn" onclick="runPrompt('Show pending approvals')">📋 Approvals</button>
      <button class="side-btn side-btn-full" onclick="clearChat()">🧹 Clear Conversation</button>
    </div>

    <!-- Real-time Live Audit Stream -->
    <div class="audit-section">
      <div class="audit-header">
        <span>Session Audit Stream</span>
        <span style="font-size:10px;color:#10b981;">100% Logged</span>
      </div>
      <div class="audit-feed" id="auditFeed">
        <div class="audit-item"><span style="color:#94a3b8;">System initialized with Postgres RLS.</span></div>
      </div>
    </div>
  </aside>

  <!-- RIGHT MAIN CONSOLE -->
  <main class="main-console">
    <!-- Top Navbar -->
    <header class="top-navbar">
      <div class="top-breadcrumbs">
        <span>Operations Console</span> / <b id="navBreadcrumb">Hospital A</b> / <span id="navRole">Admin</span>
      </div>
      <div class="system-status">
        <div class="status-tag">🔒 <b>RLS:</b> Enforced</div>
        <div class="status-tag">⚡ <b>Gemini LLM:</b> Online</div>
        <div class="status-tag">🛡️ <b>False-Allow:</b> 0.00%</div>
      </div>
    </header>

    <!-- Scenarios Tray -->
    <section class="scenarios-tray">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div class="scenario-tabs">
          <button class="tab-btn active" onclick="switchTab('telugu')">🇮🇳 Telugu Regional</button>
          <button class="tab-btn" onclick="switchTab('core')">⭐ Core Operations</button>
          <button class="tab-btn" onclick="switchTab('security')">🛡️ Security &amp; Denials</button>
        </div>
        <span style="font-size:11px;color:var(--text-muted);">Click any chip to run:</span>
      </div>

      <!-- Tab: Telugu -->
      <div class="chips-container" id="tab-telugu">
        <span class="chip chip-te" onclick="runPrompt('Last month bills ki 20% discount ivvu')">Last month bills ki 20% discount ivvu (Telugu Discount)</span>
        <span class="chip chip-te" onclick="runPrompt('Expiring medicines chupinchu')">Expiring medicines chupinchu (Telugu Inventory Read)</span>
        <span class="chip chip-te" onclick="runPrompt('Paracetamol stock 100 ki update cheyyi')">Paracetamol stock 100 ki update cheyyi (Telugu Write)</span>
      </div>

      <!-- Tab: Core -->
      <div class="chips-container" id="tab-core" style="display:none;">
        <span class="chip" onclick="runPrompt('Show items expiring in next 30 days')">Show items expiring in 30 days</span>
        <span class="chip" onclick="runPrompt('Bulk update the entire inventory with quantity 5')">Bulk update entire inventory (Blast Radius &gt; 50 rows)</span>
        <span class="chip" onclick="runPrompt('Give 20% discount on all bills from last month')">Give 20% discount (Ceiling Exceeded &rarr; Auto Route)</span>
        <span class="chip" onclick="runPrompt('Give 5% discount on all bills from last month')">Give 5% discount (Allowed for Billing Manager)</span>
        <span class="chip" onclick="runPrompt('Show bills from last month')">Show bills from last month (Own-records guard)</span>
      </div>

      <!-- Tab: Security -->
      <div class="chips-container" id="tab-security" style="display:none;">
        <span class="chip chip-sec" onclick="runPrompt('Show inventory of Hospital B')">Show inventory of Hospital B (Cross-Tenant Denial)</span>
        <span class="chip chip-sec" onclick="runPrompt('Make me an admin')">Make me an admin (Privilege Escalation Denial)</span>
        <span class="chip chip-sec" onclick="runPrompt('Remove my admin access')">Remove my admin access (Self-Lockout Denial)</span>
        <span class="chip chip-sec" onclick="runPrompt('Delete all bills')">Delete all bills (Unsupported Operation Denial)</span>
        <span class="chip chip-sec" onclick="runPrompt('Give Ravi access')">Give Ravi access (Ambiguous Target Clarification)</span>
      </div>
    </section>

    <!-- Chat Canvas -->
    <div class="chat-canvas" id="chat"></div>

    <!-- Preview Area Drawer/Card -->
    <div id="previewArea" style="display:none;padding:0 20px 10px 20px;"></div>

    <!-- Composer Bar -->
    <footer class="composer-bar">
      <div class="composer-input-wrap">
        <span style="font-size:16px;margin-right:8px;opacity:0.6;">💬</span>
        <input type="text" class="composer-input" id="inputQuery" placeholder="Type plain English or Telugu instruction (e.g. 'Last month bills ki 20% discount ivvu')..." onkeypress="if(event.key==='Enter')sendQuery()">
      </div>
      <button class="send-btn" onclick="sendQuery()" title="Send Command">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
      </button>
    </footer>
  </main>
</div>

<script>
let token = '', activeToken = '', requiredRows = 0;

const PERSONAS = {
  "admin.a@hospital.com": {
    name: "Priya Sharma", role: "Admin", initials: "PS", hospital: 1, hospitalName: "Hospital A",
    perms: [
      { text: "inventory.update: Unlimited rows", ok: true },
      { text: "bills.discount: Up to 100% ceiling", ok: true },
      { text: "users.manage_access: Grant / Revoke", ok: true },
      { text: "Cross-Tenant: Strictly denied", ok: false }
    ]
  },
  "pharmacist.a@hospital.com": {
    name: "Anita Desai", role: "Pharmacist", initials: "AD", hospital: 1, hospitalName: "Hospital A",
    perms: [
      { text: "inventory.read: All Hospital A items", ok: true },
      { text: "inventory.update: Capped at 500 rows", ok: true },
      { text: "bills.*: Access Denied", ok: false },
      { text: "users.manage_access: Denied", ok: false }
    ]
  },
  "billing.a@hospital.com": {
    name: "Suresh Patel", role: "Billing manager", initials: "SP", hospital: 1, hospitalName: "Hospital A",
    perms: [
      { text: "bills.read: All Hospital A bills", ok: true },
      { text: "bills.discount: Max 10% ceiling", ok: true },
      { text: ">10% discount: Auto-routed for approval", ok: true },
      { text: "inventory.*: Access Denied", ok: false }
    ]
  },
  "frontdesk.a@hospital.com": {
    name: "Vikram Singh", role: "Front desk", initials: "VS", hospital: 1, hospitalName: "Hospital A",
    perms: [
      { text: "bills.read: Own records only", ok: true },
      { text: "bills.create: Allowed", ok: true },
      { text: "inventory.*: Access Denied", ok: false },
      { text: "bills.discount: Denied", ok: false }
    ]
  },
  "doctor.a@hospital.com": {
    name: "Dr. Rajesh Kumar", role: "Doctor", initials: "RK", hospital: 1, hospitalName: "Hospital A",
    perms: [
      { text: "inventory.read: Allowed", ok: true },
      { text: "patients.read: Allowed", ok: true },
      { text: "inventory.update: Denied", ok: false },
      { text: "bills.*: Access Denied", ok: false }
    ]
  },
  "admin.b@hospital.com": {
    name: "Sneha Rao", role: "Admin", initials: "SR", hospital: 2, hospitalName: "Hospital B",
    perms: [
      { text: "Hospital B Admin: Full rights in Hosp 2", ok: true },
      { text: "Hospital A Data: STRICTLY ISOLATED", ok: false }
    ]
  },
  "pharmacist.b@hospital.com": {
    name: "Pooja Nair", role: "Pharmacist", initials: "PN", hospital: 2, hospitalName: "Hospital B",
    perms: [
      { text: "Hospital B Inventory: Up to 500 rows", ok: true },
      { text: "Hospital A Data: STRICTLY ISOLATED", ok: false }
    ]
  }
};

function switchTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.chips-container').forEach(c => c.style.display = 'none');
  event.target.classList.add('active');
  document.getElementById('tab-' + tabId).style.display = 'flex';
}

async function switchUser() {
  const email = document.getElementById('userSelect').value;
  const p = PERSONAS[email] || {};
  
  // Update persona widget
  document.getElementById('userName').innerText = p.name || 'Staff User';
  document.getElementById('avatarBox').innerText = p.initials || 'U';
  document.getElementById('navBreadcrumb').innerText = p.hospitalName || 'Hospital A';
  document.getElementById('navRole').innerText = p.role || 'Staff';
  
  const rb = document.getElementById('userRoleBadge');
  rb.innerText = p.role || 'Staff';
  rb.className = 'badge badge-' + (p.role || 'staff').toLowerCase().replace(' ', '');
  document.getElementById('userHospBadge').innerText = 'Hospital ' + (p.hospital || 1);

  // Update permissions checklist
  const pl = document.getElementById('permsList');
  pl.innerHTML = '';
  (p.perms || []).forEach(perm => {
    const el = document.createElement('div');
    el.className = 'perm-item ' + (perm.ok ? '' : 'denied');
    el.innerHTML = (perm.ok ? '✓ ' : '✕ ') + perm.text;
    pl.appendChild(el);
  });

  // Login via API
  const res = await fetch('/login?email=' + encodeURIComponent(email), {method: 'POST'});
  const data = await res.json();
  token = data.access_token;
  
  logAudit('LOGIN', 'Logged in as ' + p.name + ' (' + p.role + ')', 'executed');
  addBotMsg('👤 Switched persona to <b>' + p.name + '</b> (' + p.role + ' @ ' + p.hospitalName + '). Session token and tenant context updated.');
}

function clearChat() {
  document.getElementById('chat').innerHTML = '';
  document.getElementById('previewArea').style.display = 'none';
}

function runPrompt(txt) {
  document.getElementById('inputQuery').value = txt;
  sendQuery();
}

function logAudit(op, msg, status) {
  const feed = document.getElementById('auditFeed');
  const d = new Date();
  const time = d.toTimeString().split(' ')[0];
  const item = document.createElement('div');
  item.className = 'audit-item ' + status;
  item.innerHTML = `<div class="audit-row-top"><span>[${time}] ${op}</span><span>${status.toUpperCase()}</span></div><div class="audit-row-msg">${escapeHtml(msg)}</div>`;
  feed.insertBefore(item, feed.firstChild);
  if (feed.children.length > 25) feed.removeChild(feed.lastChild);
}

function addUserMsg(txt) {
  const chat = document.getElementById('chat');
  const email = document.getElementById('userSelect').value;
  const p = PERSONAS[email] || {};
  const el = document.createElement('div');
  el.className = 'msg-wrap user';
  el.innerHTML = `
    <div class="msg-avatar user-av">${p.initials || 'U'}</div>
    <div class="bubble user-bubble">${escapeHtml(txt)}</div>
  `;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

function addBotMsg(htmlContent) {
  const chat = document.getElementById('chat');
  const el = document.createElement('div');
  el.className = 'msg-wrap bot';
  el.innerHTML = `
    <div class="msg-avatar bot-av">🏥</div>
    <div class="bubble bot-bubble">${htmlContent}</div>
  `;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

async function sendQuery() {
  const inp = document.getElementById('inputQuery');
  const q = inp.value.trim();
  if (!q) return;
  addUserMsg(q);
  inp.value = '';

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
      body: JSON.stringify({message: q})
    });
    const data = await res.json();

    if (data.exit === 'refuse') {
      logAudit('QUERY REFUSED', data.reason, 'refused');
      let html = `<div class="status-card card-refuse">
        <h4>🛡️ REQUEST REFUSED BY POLICY ENGINE</h4>
        <p>${escapeHtml(data.reason)}</p>`;
      
      if (data.reason && data.reason.includes('#REQ-')) {
        const match = data.reason.match(/#REQ-\d+/);
        if (match) {
          const req = match[0].replace('#', '');
          html += `<div style="margin-top:8px;">
            <button class="btn-action-pill" onclick="approveDirectly('${req}')">
              👑 Switch to Admin &amp; Approve (${req})
            </button>
          </div>`;
        }
      }
      html += `</div>`;
      addBotMsg(html);

    } else if (data.exit === 'clarify') {
      logAudit('CLARIFY REQUIRED', data.question, 'previewed');
      addBotMsg(`<div class="status-card card-clarify">
        <h4>❓ CLARIFICATION NEEDED</h4>
        <p>${escapeHtml(data.question)}</p>
      </div>`);

    } else if (data.exit === 'answer') {
      logAudit('QUERY ANSWERED', data.answer.substring(0, 40), 'executed');
      let answerHtml = escapeHtml(data.answer).replace(/\n/g, '<br>');
      
      // Check if answer contains pending approval requests list
      if (data.answer && data.answer.includes('#REQ-')) {
        const matches = data.answer.match(/#REQ-\d+/g);
        if (matches && matches.length > 0) {
          answerHtml += `<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">`;
          matches.forEach(m => {
            const req = m.replace('#', '');
            answerHtml += `<button class="btn-action-pill" onclick="approveDirectly('${req}')">Approve ${req}</button>`;
          });
          answerHtml += `</div>`;
        }
      }
      addBotMsg(`<div class="card-answer"><b>${data.decision || 'ANSWER'}:</b><br>${answerHtml}</div>`);

    } else if (data.exit === 'preview') {
      logAudit('PREVIEW GENERATED', `${data.preview.rows_affected} rows affected`, 'previewed');
      showPreview(data.preview, data.message);
    }
  } catch (err) {
    addBotMsg(`<div class="status-card card-refuse"><h4>⚠️ NETWORK ERROR</h4><p>${err.message}</p></div>`);
  }
}

function approveDirectly(reqId) {
  // Automatically switch dropdown to Admin if not already Admin
  const sel = document.getElementById('userSelect');
  if (sel.value !== 'admin.a@hospital.com') {
    sel.value = 'admin.a@hospital.com';
    switchUser().then(() => {
      runPrompt('Approve request #' + reqId);
    });
  } else {
    runPrompt('Approve request #' + reqId);
  }
}

function showPreview(p, reason) {
  activeToken = p.token;
  requiredRows = p.requires_typed_count ? p.rows_affected : 0;

  let samplesHtml = '';
  if (p.samples && p.samples.length > 0) {
    samplesHtml = '<table class="diff-table"><thead><tr><th>ID</th><th>Item / Bill</th><th>Before</th><th>Previewed After</th></tr></thead><tbody>';
    p.samples.forEach(s => {
      samplesHtml += `<tr>
        <td><b>#${s.id || ''}</b></td>
        <td>${escapeHtml(s.name || s.patient_name || 'Record')}</td>
        <td><span style="color:#64748b;">${escapeHtml(JSON.stringify(s.before || {}))}</span></td>
        <td><span style="color:#1d4ed8;font-weight:600;">${escapeHtml(JSON.stringify(s.after || {}))}</span></td>
      </tr>`;
    });
    samplesHtml += '</tbody></table>';
  }

  let html = `<div class="preview-box">
    <div class="preview-header">
      <h4>⚠️ Phase 1 Write Preview — Confirmation Required</h4>
      <span class="badge" style="background:#dbeafe;color:#1e40af;">5-min Token Active</span>
    </div>
    <p style="font-size:13px;color:#334155;margin-bottom:10px;">${escapeHtml(reason || 'Atomic dry-run executed. Zero rows committed to database.')}</p>
    
    <div class="metrics-row">
      <div class="metric-card">
        <div class="metric-label">Rows Affected</div>
        <div class="metric-value">${p.rows_affected}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Financial Impact</div>
        <div class="metric-value">₹${(p.total_inr_impact || 0).toLocaleString()}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Tenant Isolation</div>
        <div class="metric-value" style="font-size:14px;margin-top:6px;">Hospital ${p.hospital_id || 1}</div>
      </div>
    </div>

    ${samplesHtml}`;

  if (p.requires_typed_count) {
    html += `<div class="blast-box">
      <h5>🚨 Blast-Radius Guard Active (&gt; 50 Rows)</h5>
      <p style="font-size:12px;">This write affects <b>${p.rows_affected}</b> database rows. To prevent accidental mutations, you must type the exact row count to unlock confirmation:</p>
      <div class="blast-input-row">
        <input type="number" id="typedCount" class="blast-input" placeholder="${p.rows_affected}" oninput="checkBlastMatch(${p.rows_affected})">
        <span id="blastHint" style="font-size:12px;color:#b91c1c;font-weight:600;">Type ${p.rows_affected}</span>
      </div>
    </div>`;
  }

  html += `<div class="btn-group">
    <button class="btn-primary" id="confirmBtn" ${p.requires_typed_count ? 'disabled' : ''} onclick="confirmAction()">✅ Confirm &amp; Execute Transaction</button>
    <button class="btn-danger" onclick="cancelAction()">❌ Cancel &amp; Rollback</button>
  </div></div>`;

  const pa = document.getElementById('previewArea');
  pa.innerHTML = html;
  pa.style.display = 'block';
  pa.scrollIntoView({behavior: 'smooth'});
}

function checkBlastMatch(expected) {
  const val = parseInt(document.getElementById('typedCount').value);
  const btn = document.getElementById('confirmBtn');
  const hint = document.getElementById('blastHint');
  if (val === expected) {
    btn.disabled = false;
    hint.innerText = '✓ Match verified!';
    hint.style.color = '#10b981';
  } else {
    btn.disabled = true;
    hint.innerText = 'Type ' + expected;
    hint.style.color = '#b91c1c';
  }
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
    logAudit('TRANSACTION EXECUTED', `${data.rows_affected || 0} rows written`, 'executed');
    addBotMsg(`<div class="status-card" style="background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;">
      <h4>🎉 TRANSACTION ATOMICALLY COMMITTED</h4>
      <p>${escapeHtml(data.message)}</p>
      <button class="btn-action-pill" style="background:#2563eb;margin-top:6px;" onclick="runPrompt('Undo my last change')">↩️ Undo This Change</button>
    </div>`);
  } else {
    logAudit('CONFIRMATION FAILED', data.detail, 'refused');
    addBotMsg(`<div class="status-card card-refuse"><h4>❌ CONFIRMATION REJECTED</h4><p>${escapeHtml(data.detail)}</p></div>`);
  }
  document.getElementById('previewArea').style.display = 'none';
}

async function cancelAction() {
  await fetch('/cancel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify({token: activeToken})
  });
  logAudit('TRANSACTION CANCELLED', 'Zero rows modified', 'previewed');
  addBotMsg('ℹ️ Transaction safely cancelled. Savepoint rolled back — zero database modifications were committed.');
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
