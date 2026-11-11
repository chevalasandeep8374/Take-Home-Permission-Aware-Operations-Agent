"""
Typed, parameterized WRITE operations.
Every query enforces tenant isolation twice:
1. Explicit WHERE hospital_id = principal.hospital_id
2. Transaction scoped by SET LOCAL app.hospital_id
"""

from typing import List, Dict, Any, Tuple
from sqlalchemy import select, update, delete, and_, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import InventoryItem, Bill, User, Role, UserRole, Permission, UserPermissionOverride
from app.agent.dates import resolve_date_range
from app.schemas import (
    InventoryBulkUpdateIntent,
    InventoryDeleteIntent,
    BillsDiscountIntent,
    BillsUpdateStatusIntent,
    AccessChangeIntent,
)
from app.execution.ops_read import ensure_tenant_context


def bulk_set_quantity_op(
    principal: Principal,
    intent: InventoryBulkUpdateIntent,
    session: Session,
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """
    Hospital-scoped bulk inventory quantity update.
    Returns (rows_affected, before_snapshot, after_snapshot, total_inr_impact).
    """
    ensure_tenant_context(session, principal.hospital_id)

    query = select(InventoryItem).where(InventoryItem.hospital_id == principal.hospital_id)

    if intent.scope == "category" and intent.category:
        query = query.where(InventoryItem.category.ilike(f"%{intent.category.strip()}%"))
    elif intent.scope == "name_contains" and intent.name_contains:
        query = query.where(InventoryItem.name.ilike(f"%{intent.name_contains.strip()}%"))

    items = session.execute(query).scalars().all()
    rows_affected = len(items)

    before_snapshot = [
        {
            "id": it.id,
            "name": it.name,
            "quantity": it.quantity,
            "unit_price": float(it.unit_price),
        }
        for it in items
    ]

    total_inr_impact = 0.0
    after_snapshot = []

    for it in items:
        qty_diff = abs(intent.set_quantity - it.quantity)
        total_inr_impact += qty_diff * float(it.unit_price)
        it.quantity = intent.set_quantity
        after_snapshot.append({
            "id": it.id,
            "name": it.name,
            "quantity": it.quantity,
            "unit_price": float(it.unit_price),
        })

    session.flush()
    return rows_affected, before_snapshot, after_snapshot, round(total_inr_impact, 2)


def apply_bills_discount_op(
    principal: Principal,
    intent: BillsDiscountIntent,
    session: Session,
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """
    Hospital-scoped bulk discount application on bills.
    Returns (rows_affected, before_snapshot, after_snapshot, total_inr_impact).
    """
    ensure_tenant_context(session, principal.hospital_id)

    query = select(Bill).where(Bill.hospital_id == principal.hospital_id)

    if intent.date_range and intent.date_range != "all":
        start_date, end_date = resolve_date_range(intent.date_range)
        query = query.where(
            and_(
                Bill.bill_date >= start_date,
                Bill.bill_date <= end_date,
            )
        )

    bills = session.execute(query).scalars().all()
    rows_affected = len(bills)

    before_snapshot = [
        {
            "id": b.id,
            "patient_name": b.patient_name,
            "gross_amount": float(b.gross_amount),
            "discount_pct": float(b.discount_pct),
            "net_amount": float(b.net_amount),
        }
        for b in bills
    ]

    total_inr_impact = 0.0
    after_snapshot = []

    for b in bills:
        # Discount impact = gross_amount * (requested_pct / 100)
        inr_diff = float(b.gross_amount) * (intent.pct / 100.0)
        total_inr_impact += inr_diff

        b.discount_pct = intent.pct
        b.net_amount = round(float(b.gross_amount) * (1.0 - intent.pct / 100.0), 2)

        after_snapshot.append({
            "id": b.id,
            "patient_name": b.patient_name,
            "gross_amount": float(b.gross_amount),
            "discount_pct": float(b.discount_pct),
            "net_amount": float(b.net_amount),
        })

    session.flush()
    return rows_affected, before_snapshot, after_snapshot, round(total_inr_impact, 2)


def update_bills_status_op(
    principal: Principal,
    intent: BillsUpdateStatusIntent,
    session: Session,
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """
    Update status for bills within hospital and date range.
    """
    ensure_tenant_context(session, principal.hospital_id)

    query = select(Bill).where(Bill.hospital_id == principal.hospital_id)

    if intent.date_range and intent.date_range != "all":
        start_date, end_date = resolve_date_range(intent.date_range)
        query = query.where(
            and_(
                Bill.bill_date >= start_date,
                Bill.bill_date <= end_date,
            )
        )

    bills = session.execute(query).scalars().all()
    rows_affected = len(bills)

    before_snapshot = [{"id": b.id, "status": b.status} for b in bills]
    after_snapshot = []

    for b in bills:
        b.status = intent.status
        after_snapshot.append({"id": b.id, "status": b.status})

    session.flush()
    return rows_affected, before_snapshot, after_snapshot, 0.0


def change_access_op(
    principal: Principal,
    intent: AccessChangeIntent,
    session: Session,
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """
    Apply permission grant or revoke override for a staff member in the same hospital.
    """
    ensure_tenant_context(session, principal.hospital_id)

    # 1. Look up target user in the same hospital
    user = session.execute(
        select(User).where(
            and_(
                User.hospital_id == principal.hospital_id,
                User.name.ilike(f"%{intent.target_user_name.strip()}%"),
            )
        )
    ).scalars().first()

    if not user:
        raise ValueError(f"Target user '{intent.target_user_name}' not found in your hospital.")

    # 2. Look up permission record
    perm_code = intent.permission_code or "inventory.update"
    permission = session.execute(
        select(Permission).where(Permission.code == perm_code)
    ).scalars().first()

    if not permission:
        raise ValueError(f"Permission code '{perm_code}' not found.")

    # 3. Check existing override and user roles
    user_role_names = [ur.role.name for ur in user.user_roles] if user.user_roles else []
    existing_override = session.execute(
        select(UserPermissionOverride).where(
            and_(
                UserPermissionOverride.user_id == user.id,
                UserPermissionOverride.permission_id == permission.id,
            )
        )
    ).scalars().first()

    before_snapshot = [{
        "user_id": user.id,
        "user_name": user.name,
        "current_roles": user_role_names,
        "permission": perm_code,
        "effect": existing_override.effect if existing_override else "granted_via_role",
    }]

    if existing_override:
        existing_override.effect = intent.action
    else:
        new_override = UserPermissionOverride(
            hospital_id=principal.hospital_id,
            user_id=user.id,
            permission_id=permission.id,
            effect=intent.action,
            constraints={},
        )
        session.add(new_override)

    session.flush()

    after_snapshot = [{
        "user_id": user.id,
        "user_name": user.name,
        "current_roles": user_role_names,
        "permission": perm_code,
        "effect": intent.action,
        "change": f"{intent.action.capitalize()} override applied on '{perm_code}'",
    }]

    return 1, before_snapshot, after_snapshot, 0.0


def delete_inventory_op(
    principal: Principal,
    intent: InventoryDeleteIntent,
    session: Session,
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """Delete inventory items matching scope."""
    ensure_tenant_context(session, principal.hospital_id)

    query = select(InventoryItem).where(InventoryItem.hospital_id == principal.hospital_id)
    items = session.execute(query).scalars().all()
    rows_affected = len(items)

    before_snapshot = [{"id": it.id, "name": it.name} for it in items]

    for it in items:
        session.delete(it)

    session.flush()
    return rows_affected, before_snapshot, [], 0.0
