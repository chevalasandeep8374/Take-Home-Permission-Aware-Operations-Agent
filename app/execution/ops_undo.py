"""
Undo operations: restores before-snapshot if rows have not been modified since.
"""

from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, text

from app.auth import Principal
from app.models import AuditLog, InventoryItem, Bill, Permission, UserPermissionOverride
from app.execution.ops_read import ensure_tenant_context


def undo_last_change_op(principal: Principal, session: Session) -> Dict[str, Any]:
    """
    Looks up the last successful mutation executed by the logged-in user in their hospital.
    Verifies that no rows have been modified since (concurrency guard).
    Restores before_snapshot atomically in one transaction.
    """
    ensure_tenant_context(session, principal.hospital_id)

    # 1. Look up the last executed mutation by this user in this hospital
    last_log = session.execute(
        select(AuditLog)
        .where(
            and_(
                AuditLog.hospital_id == principal.hospital_id,
                AuditLog.user_id == principal.user_id,
                AuditLog.status == "executed",
                AuditLog.before_snapshot.isnot(None),
            )
        )
        .order_by(AuditLog.id.desc())
    ).scalars().first()

    if not last_log or not last_log.before_snapshot:
        return {
            "success": False,
            "message": "No previous executed changes found to undo in your session.",
            "rows_affected": 0,
        }

    intent_data = last_log.parsed_intent or {}
    intent_type = intent_data.get("type", "")
    before_snap = last_log.before_snapshot
    after_snap = last_log.after_snapshot or []

    # Ensure list
    if isinstance(before_snap, dict):
        before_snap = [before_snap]
    if isinstance(after_snap, dict):
        after_snap = [after_snap]

    # Infer intent_type from snapshot structure if missing from log
    if not intent_type and before_snap:
        sample = before_snap[0] if isinstance(before_snap, list) and len(before_snap) > 0 else {}
        if "quantity" in sample:
            intent_type = "inventory_bulk_update"
        elif "discount_pct" in sample:
            intent_type = "bills_discount"
        elif "permission_code" in sample or "action" in sample or "user_id" in sample:
            intent_type = "access_change"

    # 2. Concurrency check & Atomic Restoration
    if intent_type == "inventory_bulk_update":
        # Check concurrency: each item must still have the quantity that after_snap set
        for item_data in after_snap:
            iid = item_data.get("id")
            expected_qty = item_data.get("quantity")
            current = session.execute(
                select(InventoryItem).where(
                    and_(
                        InventoryItem.id == iid,
                        InventoryItem.hospital_id == principal.hospital_id,
                    )
                )
            ).scalars().first()
            if not current:
                return {
                    "success": False,
                    "message": f"Cannot undo: Inventory item #{iid} was deleted since your change.",
                    "rows_affected": 0,
                }
            if current.quantity != expected_qty:
                return {
                    "success": False,
                    "message": (
                        f"Cannot undo: Inventory item '{current.name}' (ID: {iid}) was modified "
                        f"by another transaction (current qty: {current.quantity}, expected: {expected_qty})."
                    ),
                    "rows_affected": 0,
                }

        # Restore original quantities
        restored_count = 0
        for item_data in before_snap:
            iid = item_data.get("id")
            orig_qty = item_data.get("quantity")
            if orig_qty is not None and iid:
                session.execute(
                    InventoryItem.__table__.update()
                    .where(
                        and_(
                            InventoryItem.id == iid,
                            InventoryItem.hospital_id == principal.hospital_id,
                        )
                    )
                    .values(quantity=orig_qty)
                )
                restored_count += 1

        session.flush()
        return {
            "success": True,
            "message": f"Successfully undone your last change! Restored {restored_count} inventory items to their previous quantities.",
            "rows_affected": restored_count,
            "undone_log_id": last_log.id,
            "before_snapshot": after_snap,
            "after_snapshot": before_snap,
        }

    elif intent_type == "bills_discount":
        # Check concurrency on bills
        for b_data in after_snap:
            bid = b_data.get("id")
            expected_pct = b_data.get("discount_pct")
            current = session.execute(
                select(Bill).where(
                    and_(
                        Bill.id == bid,
                        Bill.hospital_id == principal.hospital_id,
                    )
                )
            ).scalars().first()
            if not current or float(current.discount_pct) != float(expected_pct):
                return {
                    "success": False,
                    "message": f"Cannot undo: Bill #{bid} has been modified since your discount was applied.",
                    "rows_affected": 0,
                }

        restored_count = 0
        for b_data in before_snap:
            bid = b_data.get("id")
            if bid:
                session.execute(
                    Bill.__table__.update()
                    .where(
                        and_(
                            Bill.id == bid,
                            Bill.hospital_id == principal.hospital_id,
                        )
                    )
                    .values(
                        discount_pct=b_data.get("discount_pct", 0.0),
                        net_amount=b_data.get("net_amount", 0.0),
                    )
                )
                restored_count += 1

        session.flush()
        return {
            "success": True,
            "message": f"Successfully undone your last discount operation! Restored {restored_count} bills to their previous rates.",
            "rows_affected": restored_count,
            "undone_log_id": last_log.id,
            "before_snapshot": after_snap,
            "after_snapshot": before_snap,
        }

    elif intent_type == "access_change":
        # Restore permission override state
        for snap in before_snap:
            uid = snap.get("user_id")
            perm_code = snap.get("permission")
            orig_effect = snap.get("effect")
            perm = session.execute(
                select(Permission).where(Permission.code == perm_code)
            ).scalars().first()
            if perm and uid:
                override = session.execute(
                    select(UserPermissionOverride).where(
                        and_(
                            UserPermissionOverride.user_id == uid,
                            UserPermissionOverride.permission_id == perm.id,
                            UserPermissionOverride.hospital_id == principal.hospital_id,
                        )
                    )
                ).scalars().first()
                if orig_effect in ("none", None, "granted_via_role"):
                    if override:
                        session.delete(override)
                else:
                    if override:
                        override.effect = orig_effect

        session.flush()
        return {
            "success": True,
            "message": "Successfully undone access change! Restored user's previous permission override status.",
            "rows_affected": 1,
            "undone_log_id": last_log.id,
            "before_snapshot": after_snap,
            "after_snapshot": before_snap,
        }

    return {
        "success": False,
        "message": f"Undo is not currently supported for operation type '{intent_type}'.",
        "rows_affected": 0,
    }
