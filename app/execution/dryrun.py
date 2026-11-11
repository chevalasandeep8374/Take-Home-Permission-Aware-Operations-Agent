"""
Phase 1 Write Safety: Dry Run Engine.
Executes write operations in an isolated rolled-back transaction to compute:
1. Rows affected
2. 5-row before/after diff sample
3. Total ₹ INR impact
Stores a pending_actions record with confirmation token and 5-minute expiry.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.auth import Principal
from app.models import PendingAction
from app.execution.ops_read import ensure_tenant_context
from app.execution.ops_write import (
    bulk_set_quantity_op,
    apply_bills_discount_op,
    update_bills_status_op,
    change_access_op,
    delete_inventory_op,
)
from app.schemas import (
    InventoryBulkUpdateIntent,
    InventoryDeleteIntent,
    BillsDiscountIntent,
    BillsUpdateStatusIntent,
    AccessChangeIntent,
)


def execute_dryrun_op(
    principal: Principal,
    intent: Any,
    session: Session,
) -> tuple[int, List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """Dispatches intent to appropriate hand-written write operation."""
    intent_type = getattr(intent, "type", "")

    if intent_type == "inventory_bulk_update":
        return bulk_set_quantity_op(principal, intent, session)
    elif intent_type == "bills_discount":
        return apply_bills_discount_op(principal, intent, session)
    elif intent_type == "bills_update_status":
        return update_bills_status_op(principal, intent, session)
    elif intent_type == "access_change":
        return change_access_op(principal, intent, session)
    elif intent_type == "inventory_delete":
        return delete_inventory_op(principal, intent, session)
    else:
        raise ValueError(f"Unsupported write intent for dry run: '{intent_type}'")


def generate_dryrun_preview(
    principal: Principal,
    intent: Any,
    session: Session,
) -> Dict[str, Any]:
    """
    Executes a write operation inside a nested savepoint that is rolled back,
    computes impact diffs, and saves a pending confirmation token.
    """
    ensure_tenant_context(session, principal.hospital_id)

    # Begin nested transaction savepoint to isolate mutations
    nested = session.begin_nested()
    try:
        rows_affected, before_snaps, after_snaps, total_inr = execute_dryrun_op(
            principal, intent, session
        )
    finally:
        # Always roll back the test writes so database remains unchanged
        nested.rollback()

    # Build 5-row before/after comparison sample
    samples = []
    sample_limit = min(5, len(before_snaps))
    for i in range(sample_limit):
        samples.append({
            "before": before_snaps[i],
            "after": after_snaps[i] if i < len(after_snaps) else None,
        })

    requires_typed_count = rows_affected > 50
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    preview_payload = {
        "rows_affected": rows_affected,
        "samples": samples,
        "total_inr_impact": total_inr,
        "requires_typed_count": requires_typed_count,
        "expires_at": expires_at.isoformat(),
    }

    # Store pending action server-side
    pending = PendingAction(
        hospital_id=principal.hospital_id,
        user_id=principal.user_id,
        intent=intent.model_dump(),
        preview=preview_payload,
        token=token,
        expires_at=expires_at,
        status="pending",
    )
    session.add(pending)
    session.commit()

    return {
        "token": token,
        "rows_affected": rows_affected,
        "samples": samples,
        "total_inr_impact": total_inr,
        "requires_typed_count": requires_typed_count,
        "expires_at": expires_at.isoformat(),
    }
