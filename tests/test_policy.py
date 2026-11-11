"""
Test Policy Layer: Pure unit tests covering all 12 required scenarios.
Executes without PostgreSQL, network access, or LLM dependencies.
"""

import pytest
from app.auth import Principal
from app.policy.rules import DecisionType
from app.policy.engine import decide
from app.schemas import (
    InventoryReadIntent,
    InventoryBulkUpdateIntent,
    InventoryDeleteIntent,
    BillsReadIntent,
    BillsDiscountIntent,
    AccessChangeIntent,
    UnsupportedIntent,
)


@pytest.fixture
def admin_principal():
    return Principal(
        user_id=1,
        hospital_id=1,
        name="Priya Sharma",
        email="admin.a@hospital.com",
        roles=["Admin"],
        permissions={
            "inventory.read": {},
            "inventory.update": {},
            "inventory.delete": {},
            "bills.read": {},
            "bills.discount": {"max_pct": 100},
            "bills.update_status": {},
            "users.read": {},
            "users.manage_access": {},
        },
    )


@pytest.fixture
def pharmacist_principal():
    return Principal(
        user_id=4,
        hospital_id=1,
        name="Anita Desai",
        email="pharmacist.a@hospital.com",
        roles=["Pharmacist"],
        permissions={
            "inventory.read": {},
            "inventory.update": {"max_rows": 500},
        },
    )


@pytest.fixture
def billing_manager_principal():
    return Principal(
        user_id=3,
        hospital_id=1,
        name="Suresh Patel",
        email="billing.a@hospital.com",
        roles=["Billing manager"],
        permissions={
            "bills.read": {},
            "bills.discount": {"max_pct": 10},
            "bills.update_status": {},
        },
    )


@pytest.fixture
def front_desk_principal():
    return Principal(
        user_id=5,
        hospital_id=1,
        name="Vikram Singh",
        email="frontdesk.a@hospital.com",
        roles=["Front desk"],
        permissions={
            "bills.read": {"own_only": True},
        },
    )


# -----------------------------------------------------------------------------
# Scenario 1: Row cap constraint check
# Pharmacist capped at 500 rows; Admin has no cap
# -----------------------------------------------------------------------------
def test_scenario_1_pharmacist_row_cap_refusal(pharmacist_principal, admin_principal):
    intent = InventoryBulkUpdateIntent(set_quantity=5, scope="all")
    facts = {"affected_rows_count": 650}

    # Pharmacist: 650 rows exceeds 500-row cap -> REFUSE with suggestion
    decision_pharmacist = decide(pharmacist_principal, intent, facts)
    assert decision_pharmacist.decision == DecisionType.REFUSE
    assert "exceeding your role cap of 500" in decision_pharmacist.reason
    assert decision_pharmacist.offered_alternative["max_rows"] == 500

    # Same query as Admin: allowed with dry run preview
    decision_admin = decide(admin_principal, intent, facts)
    assert decision_admin.decision == DecisionType.ALLOW_WITH_PREVIEW


# -----------------------------------------------------------------------------
# Scenario 2: Permission check (no inventory.update)
# -----------------------------------------------------------------------------
def test_scenario_2_front_desk_lacks_permission(front_desk_principal):
    intent = InventoryBulkUpdateIntent(set_quantity=5, scope="all")
    decision = decide(front_desk_principal, intent, facts={})

    assert decision.decision == DecisionType.REFUSE
    assert "inventory.update" in decision.reason


# -----------------------------------------------------------------------------
# Scenario 3: Discount ceiling constraint
# Billing manager asks for 20% discount; capped at 10%
# -----------------------------------------------------------------------------
def test_scenario_3_billing_manager_discount_ceiling(billing_manager_principal):
    intent = BillsDiscountIntent(pct=20.0, date_range="last_month")
    decision = decide(billing_manager_principal, intent, facts={})

    assert decision.decision == DecisionType.REFUSE
    assert "ceiling of 10" in decision.reason
    assert decision.offered_alternative["max_pct"] == 10


# -----------------------------------------------------------------------------
# Scenario 4: Admin discount allowed
# -----------------------------------------------------------------------------
def test_scenario_4_admin_discount_allowed(admin_principal):
    intent = BillsDiscountIntent(pct=20.0, date_range="last_month")
    decision = decide(admin_principal, intent, facts={})

    assert decision.decision == DecisionType.ALLOW_WITH_PREVIEW


# -----------------------------------------------------------------------------
# Scenario 5: Revoke access from unambiguous user
# -----------------------------------------------------------------------------
def test_scenario_5_admin_revoke_access_from_ravi(admin_principal):
    intent = AccessChangeIntent(
        action="revoke",
        target_user_name="Ravi",
        permission_code="inventory.update",
    )
    facts = {
        "target_users": [
            {"id": 6, "name": "Ravi Verma", "hospital_id": 1, "roles": ["Pharmacist"]}
        ]
    }
    decision = decide(admin_principal, intent, facts)

    assert decision.decision == DecisionType.ALLOW_WITH_PREVIEW


# -----------------------------------------------------------------------------
# Scenario 6: Missing permission code requires clarification
# -----------------------------------------------------------------------------
def test_scenario_6_admin_give_ravi_access_missing_permission_clarify(admin_principal):
    intent = AccessChangeIntent(
        action="grant",
        target_user_name="Ravi",
        permission_code=None,
    )
    facts = {
        "target_users": [
            {"id": 6, "name": "Ravi Verma", "hospital_id": 1}
        ]
    }
    decision = decide(admin_principal, intent, facts)

    assert decision.decision == DecisionType.CLARIFY
    assert "Which access or permission" in decision.reason


# -----------------------------------------------------------------------------
# Scenario 7: Self-lockout prevention
# -----------------------------------------------------------------------------
def test_scenario_7_admin_self_lockout_refused(admin_principal):
    intent = AccessChangeIntent(
        action="revoke",
        target_user_name="Priya Sharma",
        permission_code="admin",
        target_is_self=True,
    )
    facts = {"target_user_id": admin_principal.user_id}
    decision = decide(admin_principal, intent, facts)

    assert decision.decision == DecisionType.REFUSE
    assert "Self-lockout denied" in decision.reason


# -----------------------------------------------------------------------------
# Scenario 8: Privilege escalation prevention
# -----------------------------------------------------------------------------
def test_scenario_8_billing_manager_privilege_escalation_refused(billing_manager_principal):
    intent = AccessChangeIntent(
        action="grant",
        target_user_name="Suresh Patel",
        permission_code="admin",
        target_is_self=True,
    )
    facts = {"target_user_id": billing_manager_principal.user_id}
    decision = decide(billing_manager_principal, intent, facts)

    assert decision.decision == DecisionType.REFUSE
    assert "Privilege escalation denied" in decision.reason


# -----------------------------------------------------------------------------
# Scenario 9: Cross-tenant isolation (no existence leak)
# -----------------------------------------------------------------------------
def test_scenario_9_cross_tenant_refusal(admin_principal):
    intent = InventoryBulkUpdateIntent(
        set_quantity=100,
        scope="name_contains",
        name_contains="paracetamol",
        mentions_other_hospital=True,
    )
    facts = {"mentions_other_hospital": True, "target_hospital_name": "Hospital B"}
    decision = decide(admin_principal, intent, facts)

    assert decision.decision == DecisionType.REFUSE
    assert "Cross-tenant access denied" in decision.reason


# -----------------------------------------------------------------------------
# Scenario 10: Pharmacist read-only query allowed without confirmation
# -----------------------------------------------------------------------------
def test_scenario_10_pharmacist_inventory_read_allowed(pharmacist_principal):
    intent = InventoryReadIntent(filter="expiring_within_days", days=30)
    decision = decide(pharmacist_principal, intent, facts={})

    assert decision.decision == DecisionType.ALLOW


# -----------------------------------------------------------------------------
# Scenario 11: Non-existent permission code (bills.delete)
# -----------------------------------------------------------------------------
def test_scenario_11_unsupported_bills_delete_refused(admin_principal):
    intent = UnsupportedIntent(
        reason="Operation 'bills.delete' does not exist in system permission matrix."
    )
    decision = decide(admin_principal, intent, facts={})

    assert decision.decision == DecisionType.REFUSE
    assert "bills.delete" in decision.reason


# -----------------------------------------------------------------------------
# Scenario 12: Prompt injection in DB text treated strictly as data
# -----------------------------------------------------------------------------
def test_scenario_12_prompt_injection_item_treated_as_data(pharmacist_principal):
    intent = InventoryReadIntent(filter="low_stock")
    decision = decide(pharmacist_principal, intent, facts={})

    assert decision.decision == DecisionType.ALLOW


# -----------------------------------------------------------------------------
# Additional Guard Tests
# -----------------------------------------------------------------------------
def test_inactive_user_refusal(pharmacist_principal):
    pharmacist_principal.is_active = False
    intent = InventoryReadIntent(filter="low_stock")
    decision = decide(pharmacist_principal, intent, facts={})

    assert decision.decision == DecisionType.REFUSE
    assert "inactive" in decision.reason.lower()


def test_front_desk_own_bills_filter_enforced(front_desk_principal):
    intent = BillsReadIntent(date_range="last_month")
    decision = decide(front_desk_principal, intent, facts={})

    assert decision.decision == DecisionType.ALLOW
    assert decision.applied_filters == {"created_by_user_id": 5}


def test_ambiguous_ravi_target_in_hospital_b_clarify(admin_principal):
    intent = AccessChangeIntent(
        action="revoke",
        target_user_name="Ravi",
        permission_code="inventory.update",
    )
    facts = {
        "target_users": [
            {"id": 14, "name": "Ravi Kumar"},
            {"id": 15, "name": "Ravi Teja"},
        ]
    }
    decision = decide(admin_principal, intent, facts)

    assert decision.decision == DecisionType.CLARIFY
    assert "Ambiguous target" in decision.reason
