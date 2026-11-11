"""
Role-Permission Matrix (mirrors database seed)
Pure Python definitions with no external DB or network dependencies.
"""

from typing import Dict, Any, List

SYSTEM_PERMISSIONS: List[str] = [
    "inventory.read",
    "inventory.update",
    "inventory.delete",
    "inventory.create",
    "bills.read",
    "bills.discount",
    "bills.update_status",
    "bills.create",
    "users.read",
    "users.manage_access",
    "reports.read",
    "patients.read",
]

# Canonical role permissions and their constraints
ROLE_PERMISSIONS_MATRIX: Dict[str, Dict[str, Dict[str, Any]]] = {
    "Admin": {
        "inventory.read": {},
        "inventory.update": {},
        "inventory.delete": {},
        "inventory.create": {},
        "bills.read": {},
        "bills.discount": {"max_pct": 100},
        "bills.update_status": {},
        "bills.create": {},
        "users.read": {},
        "users.manage_access": {},
        "reports.read": {},
        "patients.read": {},
    },
    "Billing manager": {
        "bills.read": {},
        "bills.discount": {"max_pct": 10},
        "bills.update_status": {},
        "bills.create": {},
    },
    "Pharmacist": {
        "inventory.read": {},
        "inventory.update": {"max_rows": 500},
        "inventory.create": {},
    },
    "Front desk": {
        "bills.read": {"own_only": True},
        "bills.create": {},
    },
    "Doctor": {
        "inventory.read": {},
        "patients.read": {},
    },
}
