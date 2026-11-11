"""
Phase 2 Write Execution: Confirmation & Cancellation.
Guarantees:
1. Re-check policy before execution (state may have changed)
2. Blast-radius verification: typed row count match when rows > 50
3. Atomic transaction execution: BEGIN; SET LOCAL app.hospital_id; UPDATE; COMMIT
4. Atomic rollback on any failure
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional
from sqlalchemy import select, and_, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import PendingAction
from app.schemas import intent_adapter
from app.policy.engine import decide
from app.execution.ops_read import ensure_tenant_context
from app.execution.dryrun import execute_dryrun_op


def confirm_write_action(
    token: str,
    typed_count: Optional[int],
    principal: Principal,
    session: Session,
) -> Dict[str, Any]:
    """
    Executes a previously previewed action upon explicit user confirmation.
    """
    ensure_tenant_context(session, principal.hospital_id)

    # 1. Load pending action for the exact user and hospital
    pending = session.execute(
        select(PendingAction).where(
            and_(
                PendingAction.token == token,
                PendingAction.hospital_id == principal.hospital_id,
                PendingAction.user_id == principal.user_id,
            )
        )
    ).scalars().first()

    if not pending:
        raise ValueError("Invalid confirmation token or action does not belong to the authenticated user.")

    if pending.status != "pending":
        raise ValueError(f"Action cannot be confirmed because its status is already '{pending.status}'.")

    now_utc = datetime.now(timezone.utc)
    if pending.expires_at < now_utc:
        pending.status = "expired"
        session.commit()
        raise ValueError("Confirmation token has expired (5-minute window). Please request the operation again.")

    preview = pending.preview or {}
    expected_rows = preview.get("rows_affected", 0)
    requires_typed_count = preview.get("requires_typed_count", False)

    # 2. Blast-radius guard: typed count verification if rows > 50
    if requires_typed_count:
        if typed_count is None:
            raise ValueError(
                f"Blast-radius guard: operation affects {expected_rows} rows (> 50). "
                f"You must type the exact row count ({expected_rows}) to confirm."
            )
        if typed_count != expected_rows:
            raise ValueError(
                f"Blast-radius guard confirmation failed: typed count ({typed_count}) "
                f"does not match affected rows ({expected_rows}). Operation cancelled."
            )

    # 3. Re-run policy check against current database state
    intent = intent_adapter.validate_python(pending.intent)
    intent_dict = pending.intent
    policy_decision = decide(
        principal=principal,
        intent=intent,
        facts={"affected_rows_count": expected_rows},
    )

    if policy_decision.is_refusal:
        pending.status = "refused_at_confirmation"
        session.commit()
        raise PermissionError(f"Policy denied operation upon re-evaluation: {policy_decision.reason}")

    # 4. Atomic execution in one transaction
    try:
        rows_affected, before_snaps, after_snaps, total_inr = execute_dryrun_op(
            principal, intent, session
        )
        pending.status = "executed"
        session.commit()

        return {
            "status": "executed",
            "rows_affected": rows_affected,
            "before_snapshot": before_snaps,
            "after_snapshot": after_snaps,
            "total_inr_impact": total_inr,
            "message": f"Successfully executed operation affecting {rows_affected} rows.",
            "intent": intent_dict,
        }
    except Exception as e:
        session.rollback()
        # Mark pending action as failed in a new transaction
        with session.begin():
            p = session.execute(
                select(PendingAction).where(PendingAction.id == pending.id)
            ).scalars().first()
            if p:
                p.status = "error"
        raise RuntimeError(f"Database transaction failed and rolled back cleanly: {str(e)}")


def cancel_write_action(
    token: str,
    principal: Principal,
    session: Session,
) -> Dict[str, Any]:
    """Cancels a pending write action."""
    ensure_tenant_context(session, principal.hospital_id)

    pending = session.execute(
        select(PendingAction).where(
            and_(
                PendingAction.token == token,
                PendingAction.hospital_id == principal.hospital_id,
                PendingAction.user_id == principal.user_id,
            )
        )
    ).scalars().first()

    if not pending:
        raise ValueError("Invalid confirmation token.")

    pending.status = "cancelled"
    session.commit()
    return {"status": "cancelled", "message": "Operation successfully cancelled. No database changes were made."}
