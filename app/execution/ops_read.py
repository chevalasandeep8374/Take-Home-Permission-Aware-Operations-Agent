"""
Typed, parameterized READ operations.
Every query enforces tenant isolation twice:
1. Explicit WHERE hospital_id = principal.hospital_id
2. Transaction scoped by SET LOCAL app.hospital_id
"""

from typing import List, Dict, Any, Optional
from datetime import timedelta
from sqlalchemy import select, and_, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import InventoryItem, Bill, User, Role, UserRole
from app.agent.dates import get_current_ist_date, resolve_date_range
from app.schemas import InventoryReadIntent, BillsReadIntent, UsersReadIntent


def ensure_tenant_context(session: Session, hospital_id: int):
    """Executes SET LOCAL app.hospital_id / set_config for PostgreSQL RLS."""
    session.execute(
        text("SELECT set_config('app.hospital_id', :hid, true);"),
        {"hid": str(int(hospital_id))},
    )


def inventory_read_op(
    principal: Principal,
    intent: InventoryReadIntent,
    session: Session,
) -> List[Dict[str, Any]]:
    """
    Hospital-scoped read of inventory items.
    """
    ensure_tenant_context(session, principal.hospital_id)
    today = get_current_ist_date()

    query = select(InventoryItem).where(
        InventoryItem.hospital_id == principal.hospital_id
    )

    if intent.filter == "expiring_within_days":
        days = intent.days or 30
        end_date = today + timedelta(days=days)
        query = query.where(
            and_(
                InventoryItem.expiry_date >= today,
                InventoryItem.expiry_date <= end_date,
            )
        ).order_by(InventoryItem.expiry_date.asc())

    elif intent.filter == "low_stock":
        # Items with quantity <= 10
        query = query.where(InventoryItem.quantity <= 10).order_by(InventoryItem.quantity.asc())

    elif intent.filter == "by_name" and intent.name:
        query = query.where(InventoryItem.name.ilike(f"%{intent.name.strip()}%"))

    elif intent.filter == "by_category" and intent.category:
        query = query.where(InventoryItem.category.ilike(f"%{intent.category.strip()}%"))

    rows = session.execute(query).scalars().all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "category": item.category,
            "batch_no": item.batch_no,
            "expiry_date": item.expiry_date.isoformat(),
            "quantity": item.quantity,
            "unit_price": float(item.unit_price),
        }
        for item in rows
    ]


def bills_read_op(
    principal: Principal,
    intent: BillsReadIntent,
    session: Session,
    applied_filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Hospital-scoped read of bills.
    Supports own_only filter constraint from policy layer.
    """
    ensure_tenant_context(session, principal.hospital_id)

    query = select(Bill).where(Bill.hospital_id == principal.hospital_id)

    # Apply policy constraint filters (e.g. Front desk own_only)
    if applied_filters and "created_by_user_id" in applied_filters:
        query = query.where(Bill.created_by_user_id == applied_filters["created_by_user_id"])

    # Date range filter
    if intent.date_range and intent.date_range != "all":
        start_date, end_date = resolve_date_range(intent.date_range)
        query = query.where(
            and_(
                Bill.bill_date >= start_date,
                Bill.bill_date <= end_date,
            )
        )

    # Patient name filter
    if intent.patient:
        query = query.where(Bill.patient_name.ilike(f"%{intent.patient.strip()}%"))

    query = query.order_by(Bill.bill_date.desc()).limit(100)
    rows = session.execute(query).scalars().all()

    return [
        {
            "id": b.id,
            "patient_name": b.patient_name,
            "bill_date": b.bill_date.isoformat(),
            "gross_amount": float(b.gross_amount),
            "discount_pct": float(b.discount_pct),
            "net_amount": float(b.net_amount),
            "status": b.status,
            "created_by_user_id": b.created_by_user_id,
        }
        for b in rows
    ]


def users_read_op(
    principal: Principal,
    intent: UsersReadIntent,
    session: Session,
) -> List[Dict[str, Any]]:
    """
    Hospital-scoped read of users and their assigned roles.
    """
    ensure_tenant_context(session, principal.hospital_id)

    query = (
        select(User, Role.name)
        .outerjoin(UserRole, User.id == UserRole.user_id)
        .outerjoin(Role, UserRole.role_id == Role.id)
        .where(User.hospital_id == principal.hospital_id)
    )

    if intent.name:
        query = query.where(User.name.ilike(f"%{intent.name.strip()}%"))

    results = session.execute(query).all()
    users_dict: Dict[int, Dict[str, Any]] = {}
    for user, role_name in results:
        if user.id not in users_dict:
            users_dict[user.id] = {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "is_active": user.is_active,
                "roles": [],
            }
        if role_name and role_name not in users_dict[user.id]["roles"]:
            users_dict[user.id]["roles"].append(role_name)

    return list(users_dict.values())


def format_inventory_summary(items: List[Dict[str, Any]], title: str = "Inventory Summary") -> str:
    """
    Builds structured prose summaries in pure code.
    Prompt injection defense: DB values (e.g. item names) are treated as pure data literals.
    """
    if not items:
        return f"{title}: No items found matching the requested criteria."

    lines = [f"{title} ({len(items)} items found):"]
    for idx, item in enumerate(items[:20], 1):
        lines.append(
            f"  {idx}. {item['name']} | Category: {item['category']} | "
            f"Qty: {item['quantity']} | Expiry: {item['expiry_date']} | Unit: ₹{item['unit_price']:.2f}"
        )
    if len(items) > 20:
        lines.append(f"  ... and {len(items) - 20} more items.")
    return "\n".join(lines)


def format_bills_summary(bills: List[Dict[str, Any]], title: str = "Bills Summary") -> str:
    """Deterministic summary of bills."""
    if not bills:
        return f"{title}: No bills found."

    total_gross = sum(b["gross_amount"] for b in bills)
    total_net = sum(b["net_amount"] for b in bills)
    lines = [
        f"{title} ({len(bills)} bills):",
        f"  Total Gross: ₹{total_gross:,.2f} | Total Net: ₹{total_net:,.2f}",
        "  Recent Bills:",
    ]
    for idx, b in enumerate(bills[:10], 1):
        lines.append(
            f"    {idx}. #{b['id']} - Patient: {b['patient_name']} | Date: {b['bill_date']} | "
            f"Gross: ₹{b['gross_amount']:.2f} | Disc: {b['discount_pct']}% | Net: ₹{b['net_amount']:.2f} | Status: {b['status']}"
        )
    if len(bills) > 10:
        lines.append(f"    ... and {len(bills) - 10} more bills.")
    return "\n".join(lines)
