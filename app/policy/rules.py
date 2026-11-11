"""
Policy Rules: Pure functions implementing deterministic security and permission checks.
Strictly zero LLM, database, or network imports.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
from app.auth import Principal
from app.policy.matrix import SYSTEM_PERMISSIONS


class DecisionType(str, Enum):
    ALLOW = "allow"
    ALLOW_WITH_PREVIEW = "allow_with_preview"
    REFUSE = "refuse"
    CLARIFY = "clarify"


@dataclass
class Decision:
    """Policy decision result."""
    decision: DecisionType
    reason: str = ""
    preview: Optional[Dict[str, Any]] = None
    applied_filters: Optional[Dict[str, Any]] = None
    offered_alternative: Optional[Any] = None

    @property
    def is_allowed(self) -> bool:
        return self.decision in (DecisionType.ALLOW, DecisionType.ALLOW_WITH_PREVIEW)

    @property
    def is_refusal(self) -> bool:
        return self.decision == DecisionType.REFUSE

    @property
    def is_clarify(self) -> bool:
        return self.decision == DecisionType.CLARIFY


INTENT_PERMISSION_MAP: Dict[str, str] = {
    "inventory_read": "inventory.read",
    "inventory_bulk_update": "inventory.update",
    "inventory_delete": "inventory.delete",
    "bills_read": "bills.read",
    "bills_discount": "bills.discount",
    "bills_update_status": "bills.update_status",
    "users_read": "users.read",
    "access_change": "users.manage_access",
}

WRITE_INTENTS = {
    "inventory_bulk_update",
    "inventory_delete",
    "bills_discount",
    "bills_update_status",
    "access_change",
}


def rule_check_active_user(principal: Principal) -> Optional[Decision]:
    """Rule 1: Inactive user -> refuse."""
    if not principal.is_active:
        return Decision(
            decision=DecisionType.REFUSE,
            reason="User account is inactive. Operations are denied.",
        )
    return None


def rule_check_supported_intent(intent: Any, principal: Optional[Principal] = None) -> Optional[Decision]:
    """Rule 2: Intent unsupported / no matching permission code -> refuse."""
    intent_type = getattr(intent, "type", None)

    if intent_type == "unsupported":
        reason = getattr(intent, "reason", "Unsupported operation.")
        return Decision(decision=DecisionType.REFUSE, reason=reason)

    if intent_type == "clarify":
        question = getattr(intent, "question", "Clarification requested.")
        return Decision(decision=DecisionType.CLARIFY, reason=question)

    # Undo is permitted for any authenticated user to revert their own operations
    if intent_type == "undo":
        return None

    # Approval management
    if intent_type == "approval":
        action = getattr(intent, "action", "list")
        if action in ("approve", "reject") and principal and not principal.is_admin:
            return Decision(
                decision=DecisionType.REFUSE,
                reason="Permission denied: Only administrators can approve or reject pending requests.",
            )
        return None

    perm_code = INTENT_PERMISSION_MAP.get(intent_type)
    if not perm_code:
        return Decision(
            decision=DecisionType.REFUSE,
            reason=f"Unsupported operation: no system capability for intent '{intent_type}'.",
        )

    if perm_code not in SYSTEM_PERMISSIONS:
        return Decision(
            decision=DecisionType.REFUSE,
            reason=f"Unsupported operation: system permission '{perm_code}' does not exist.",
        )

    return None


def rule_check_cross_tenant(principal: Principal, intent: Any, facts: Dict[str, Any]) -> Optional[Decision]:
    """
    Rule 3: Cross-tenant: if the query names another hospital -> refuse with a generic message.
    Never look the item up, never say whether it exists (scenario 9).
    """
    if getattr(intent, "mentions_other_hospital", False) or facts.get("mentions_other_hospital"):
        return Decision(
            decision=DecisionType.REFUSE,
            reason="Cross-tenant access denied: you cannot view or modify data belonging to other hospitals.",
        )

    target_hid = facts.get("target_hospital_id")
    if target_hid is not None and target_hid != principal.hospital_id:
        return Decision(
            decision=DecisionType.REFUSE,
            reason="Cross-tenant access denied: you cannot view or modify data belonging to other hospitals.",
        )

    return None


def rule_check_has_permission(principal: Principal, intent: Any) -> Optional[Decision]:
    """Rule 4: Principal lacks permission -> refuse, no DB query on that table (scenario 2)."""
    intent_type = getattr(intent, "type", "")
    perm_code = INTENT_PERMISSION_MAP.get(intent_type)
    if not perm_code:
        return None

    # Access change escalation check (scenario 8: 'make me admin' by non-admin)
    if intent_type == "access_change":
        action = getattr(intent, "action", "")
        target_perm = (getattr(intent, "permission_code", "") or "").strip().lower()
        if action == "grant" and ("admin" in target_perm or target_perm in ("admin", "all", "superuser")):
            if not principal.is_admin:
                return Decision(
                    decision=DecisionType.REFUSE,
                    reason="Privilege escalation denied: user lacks permissions to grant administrative privileges.",
                )

    if not principal.has_permission(perm_code):
        return Decision(
            decision=DecisionType.REFUSE,
            reason=f"Permission denied: your role lacks '{perm_code}' permission.",
        )

    return None


def rule_check_constraints(
    principal: Principal, intent: Any, facts: Dict[str, Any]
) -> Tuple[Optional[Decision], Dict[str, Any]]:
    """
    Rule 5: Constraints evaluation:
      - max_rows: count affected rows in the principal's hospital first; if count > cap -> refuse (scenario 1).
      - max_pct: pct > ceiling -> refuse and offer the maximum allowed (scenario 3).
      - own_only: force a created_by_user_id = principal.user_id filter.
    """
    applied_filters: Dict[str, Any] = {}
    intent_type = getattr(intent, "type", "")
    perm_code = INTENT_PERMISSION_MAP.get(intent_type, "")

    # 1. max_rows constraint (e.g. inventory.update max 500 rows)
    max_rows = principal.get_constraint(perm_code, "max_rows")
    if max_rows is not None:
        affected_rows = facts.get("affected_rows_count", 0)
        if affected_rows > max_rows:
            return (
                Decision(
                    decision=DecisionType.REFUSE,
                    reason=(
                        f"Operation touches {affected_rows} rows, exceeding your role cap of {max_rows} rows. "
                        "Please narrow your query (e.g., by category or specific medicine name)."
                    ),
                    offered_alternative={"max_rows": max_rows, "suggested_action": "narrow_by_category"},
                ),
                applied_filters,
            )

    # 2. max_pct constraint (e.g. bills.discount ceiling)
    max_pct = principal.get_constraint(perm_code, "max_pct")
    if max_pct is not None and intent_type == "bills_discount":
        requested_pct = getattr(intent, "pct", 0.0)
        if requested_pct > max_pct:
            return (
                Decision(
                    decision=DecisionType.REFUSE,
                    reason=(
                        f"Requested discount of {requested_pct}% exceeds your role ceiling of {max_pct}%. "
                        f"Maximum allowed discount is {max_pct}%."
                    ),
                    offered_alternative={"max_pct": max_pct},
                ),
                applied_filters,
            )

    # 3. own_only constraint (e.g. front desk bills.read)
    own_only = principal.get_constraint(perm_code, "own_only")
    if own_only and intent_type == "bills_read":
        applied_filters["created_by_user_id"] = principal.user_id

    return None, applied_filters


def rule_check_access_changes(
    principal: Principal, intent: Any, facts: Dict[str, Any]
) -> Optional[Decision]:
    """
    Rule 6: Access changes:
      - Target must be in the same hospital; ambiguous name -> clarify.
      - Missing permission_code -> clarify (scenario 6).
      - Escalation: grantor may only grant permissions they hold; "make me admin" by non-admin -> refuse (scenario 8).
      - Self-lockout: cannot remove own admin role / own last admin permission -> refuse (scenario 7).
    """
    if getattr(intent, "type", "") != "access_change":
        return None

    target_name = getattr(intent, "target_user_name", "")
    action = getattr(intent, "action", "")
    perm_code = getattr(intent, "permission_code", None)
    target_is_self = getattr(intent, "target_is_self", False)

    # Ambiguity and target resolution in same hospital
    target_users = facts.get("target_users", [])
    if facts.get("ambiguous_target") or len(target_users) > 1:
        return Decision(
            decision=DecisionType.CLARIFY,
            reason=f"Ambiguous target: multiple users named '{target_name}' exist in your hospital. Please clarify by providing full name or email.",
        )

    if facts.get("user_not_found"):
        return Decision(
            decision=DecisionType.REFUSE,
            reason=f"User '{target_name}' was not found in your hospital.",
        )

    # Missing permission code -> clarify (scenario 6)
    if not perm_code or perm_code.strip() == "":
        return Decision(
            decision=DecisionType.CLARIFY,
            reason=f"Which access or permission would you like to {action} for {target_name}?",
        )

    norm_perm = perm_code.strip().lower()

    # Self-lockout check (scenario 7): cannot remove own admin role
    is_self = (
        target_is_self
        or facts.get("target_user_id") == principal.user_id
        or (principal.name and target_name.lower() in principal.name.lower())
        or target_name.lower() in ("me", "myself", "my")
    )

    if action == "revoke" and is_self:
        if norm_perm in ("admin", "users.manage_access", "all") or "admin" in norm_perm:
            return Decision(
                decision=DecisionType.REFUSE,
                reason="Self-lockout denied: you cannot remove your own admin access.",
            )

    # Escalation check (scenario 8): grantor may only grant permissions they hold
    if action == "grant":
        if norm_perm in ("admin", "all", "superuser") or "admin" in norm_perm:
            if not principal.is_admin:
                return Decision(
                    decision=DecisionType.REFUSE,
                    reason="Privilege escalation denied: user lacks permissions to grant administrative privileges.",
                )
        if norm_perm in SYSTEM_PERMISSIONS and not principal.has_permission(norm_perm):
            return Decision(
                decision=DecisionType.REFUSE,
                reason=f"Privilege escalation denied: you do not hold permission '{norm_perm}' to grant it.",
            )

    return None
