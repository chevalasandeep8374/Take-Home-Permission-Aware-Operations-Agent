"""
Approval Routing: Allows staff exceeding their role ceiling to submit requests
that Administrators can review and approve in chat.
"""

import secrets
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import select, and_

from app.auth import Principal
from app.models import PendingAction, User, Bill
from app.execution.ops_read import ensure_tenant_context
from app.execution.ops_write import apply_bills_discount_op
from app.schemas import BillsDiscountIntent


def create_approval_request(
    principal: Principal,
    requested_intent_data: Dict[str, Any],
    reason: str,
    session: Session,
) -> Dict[str, Any]:
    """
    Creates a pending action with status='pending_approval' routed for Admin review.
    """
    ensure_tenant_context(session, principal.hospital_id)

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    preview_payload = {
        "requested_by_user_id": principal.user_id,
        "requested_by_name": principal.name or f"User #{principal.user_id}",
        "requested_by_role": principal.roles[0] if principal.roles else "Staff",
        "reason": reason,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    action = PendingAction(
        hospital_id=principal.hospital_id,
        user_id=principal.user_id,
        intent=requested_intent_data,
        preview=preview_payload,
        token=token,
        expires_at=expires_at,
        status="pending_approval",
    )
    session.add(action)
    session.flush()
    action_id = action.id
    session.commit()

    req_id = f"REQ-{action_id}"
    return {
        "request_id": req_id,
        "id": action_id,
        "token": token,
        "status": "pending_approval",
        "message": f"Approval request #{req_id} has been submitted and routed to Admin for review.",
    }


def list_pending_approvals(principal: Principal, session: Session) -> Dict[str, Any]:
    """
    Lists all pending approval requests in the user's hospital.
    """
    ensure_tenant_context(session, principal.hospital_id)

    actions = session.execute(
        select(PendingAction).where(
            and_(
                PendingAction.hospital_id == principal.hospital_id,
                PendingAction.status == "pending_approval",
            )
        ).order_by(PendingAction.id.desc())
    ).scalars().all()

    requests_list = []
    for a in actions:
        prev = a.preview or {}
        intent = a.intent or {}
        req_id = f"REQ-{a.id}"
        requests_list.append({
            "request_id": req_id,
            "id": a.id,
            "requested_by": prev.get("requested_by_name"),
            "role": prev.get("requested_by_role"),
            "operation": intent.get("type"),
            "details": f"{intent.get('pct')}% discount on {intent.get('date_range')} bills" if intent.get("pct") else str(intent),
            "submitted_at": prev.get("submitted_at"),
            "token": a.token,
        })

    if not requests_list:
        return {
            "count": 0,
            "requests": [],
            "message": "No pending approval requests found for your hospital.",
        }

    formatted = "\n".join([
        f"• #{r['request_id']}: {r['requested_by']} ({r['role']}) requested {r['details']}"
        for r in requests_list
    ])
    return {
        "count": len(requests_list),
        "requests": requests_list,
        "message": f"Pending Approvals ({len(requests_list)}):\n{formatted}\n\nType 'Approve request #{requests_list[0]['request_id']}' to approve.",
    }


def execute_admin_approval(
    principal: Principal,
    request_id_or_number: str,
    action_type: str,  # 'approve' or 'reject'
    session: Session,
) -> Dict[str, Any]:
    """
    Approves or rejects a pending request. Requires Admin privileges.
    """
    if not principal.is_admin:
        return {
            "success": False,
            "message": "Permission denied: Only administrators can approve or reject pending requests.",
        }

    ensure_tenant_context(session, principal.hospital_id)

    # Parse numeric ID
    clean_id_str = request_id_or_number.upper().replace("REQ-", "").replace("#", "").strip()
    try:
        req_id = int(clean_id_str)
    except ValueError:
        return {"success": False, "message": f"Invalid request ID '{request_id_or_number}'."}

    action = session.execute(
        select(PendingAction).where(
            and_(
                PendingAction.id == req_id,
                PendingAction.hospital_id == principal.hospital_id,
            )
        )
    ).scalars().first()

    if not action:
        return {"success": False, "message": f"Approval request #REQ-{req_id} not found."}

    if action.status != "pending_approval":
        return {"success": False, "message": f"Request #REQ-{req_id} is already '{action.status}'."}

    if action_type == "reject":
        action.status = "rejected"
        session.commit()
        return {
            "success": True,
            "status": "rejected",
            "message": f"Approval request #REQ-{req_id} was successfully rejected.",
        }

    # Execute approved operation under Admin's authority
    intent_data = action.intent or {}
    intent_type = intent_data.get("type")

    if intent_type == "bills_discount":
        discount_intent = BillsDiscountIntent(**intent_data)
        rows_affected, before_snaps, after_snaps, total_inr = apply_bills_discount_op(
            principal=principal,
            intent=discount_intent,
            session=session,
        )
        action.status = "approved"
        session.commit()
        return {
            "success": True,
            "status": "executed",
            "rows_affected": rows_affected,
            "total_inr_impact": total_inr,
            "before_snapshot": before_snaps,
            "after_snapshot": after_snaps,
            "message": (
                f"Approved and executed request #REQ-{req_id}! Applied {discount_intent.pct}% discount "
                f"to {rows_affected} bills (Total INR Impact: ₹{total_inr:,.2f})."
            ),
        }

    return {"success": False, "message": f"Execution for operation '{intent_type}' is not configured."}
