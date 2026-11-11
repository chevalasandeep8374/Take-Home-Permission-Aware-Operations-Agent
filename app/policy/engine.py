"""
Policy Engine: Deterministic, pure-function authorization decision engine.
decide(principal, intent, facts) -> Decision(allow/refuse/clarify, reason)
Strictly zero LLM, database, or network imports.
"""

from typing import Dict, Any, Optional
from app.auth import Principal
from app.policy.rules import (
    Decision,
    DecisionType,
    WRITE_INTENTS,
    rule_check_active_user,
    rule_check_supported_intent,
    rule_check_cross_tenant,
    rule_check_has_permission,
    rule_check_constraints,
    rule_check_access_changes,
)


def decide(
    principal: Principal,
    intent: Any,
    facts: Optional[Dict[str, Any]] = None,
) -> Decision:
    """
    Pure authorization function.
    Evaluates policy rules deterministically in strict order per SPEC section 6.
    """
    facts = facts or {}

    # Rule 1: Inactive user -> refuse
    d = rule_check_active_user(principal)
    if d:
        return d

    # Rule 2: Intent unsupported / no matching permission code -> refuse
    d = rule_check_supported_intent(intent, principal)
    if d:
        return d

    # Rule 3: Cross-tenant -> refuse with generic message (no info leak)
    d = rule_check_cross_tenant(principal, intent, facts)
    if d:
        return d

    # Rule 4: Principal lacks permission -> refuse, no DB query
    d = rule_check_has_permission(principal, intent)
    if d:
        return d

    # Rule 5: Constraints evaluation (max_rows, max_pct, own_only)
    d, applied_filters = rule_check_constraints(principal, intent, facts)
    if d:
        return d

    # Rule 6: Access change rules (ambiguity, missing permission, escalation, self-lockout)
    d = rule_check_access_changes(principal, intent, facts)
    if d:
        return d

    # Rule 7: Everything else -> ALLOW (read) or ALLOW_WITH_PREVIEW (write)
    intent_type = getattr(intent, "type", "")
    if intent_type in WRITE_INTENTS:
        return Decision(
            decision=DecisionType.ALLOW_WITH_PREVIEW,
            reason="Write operation permitted by policy. Two-phase dry run preview required before confirmation.",
            applied_filters=applied_filters,
        )

    return Decision(
        decision=DecisionType.ALLOW,
        reason="Read operation permitted by policy.",
        applied_filters=applied_filters,
    )
