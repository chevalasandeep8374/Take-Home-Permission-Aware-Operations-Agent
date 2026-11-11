"""
End-to-end integration tests for all 12 required scenarios.
Uses FastAPI TestClient with FakeIntentParser returning fixed Intents.
Asserts final HTTP responses, exit types, and audit_log row persistence.
Marked with @pytest.mark.db.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import sessionmaker, Session

from app.config import settings
from app.main import app
from app.models import AuditLog
from app.auth import Principal, get_current_principal
from app.agent.parser import FakeIntentParser, get_intent_parser
from app.db import get_db
from app.schemas import (
    InventoryBulkUpdateIntent,
    BillsDiscountIntent,
    AccessChangeIntent,
    InventoryReadIntent,
    UnsupportedIntent,
)
from app.execution.ops_read import ensure_tenant_context

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def db_engine():
    try:
        engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 2},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        return engine
    except Exception as e:
        pytest.skip(f"PostgreSQL connection to {settings.database_url} unavailable: {e}")


@pytest.fixture
def db_session(db_engine):
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def fake_parser():
    return FakeIntentParser()


@pytest.fixture
def client(fake_parser, db_session):
    app.dependency_overrides[get_intent_parser] = lambda: fake_parser
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def set_auth(client, principal: Principal):
    app.dependency_overrides[get_current_principal] = lambda: principal


# User fixtures
@pytest.fixture
def admin_a():
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
def pharmacist_a():
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
def billing_a():
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
def frontdesk_a():
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


def count_audit_rows(session: Session, user_id: int, hospital_id: int = 1) -> int:
    ensure_tenant_context(session, hospital_id)
    return session.execute(
        select(func.count(AuditLog.id)).where(AuditLog.user_id == user_id)
    ).scalar() or 0


# -----------------------------------------------------------------------------
# Scenario 1: Pharmacist row cap refusal vs Admin approval
# -----------------------------------------------------------------------------
def test_e2e_scenario_1(client, fake_parser, pharmacist_a, admin_a, db_session):
    set_auth(client, pharmacist_a)
    fake_parser.set_intent(InventoryBulkUpdateIntent(set_quantity=5, scope="all"))

    initial_audit = count_audit_rows(db_session, pharmacist_a.user_id)
    res = client.post("/chat", json={"message": "Bulk update the entire inventory with quantity 5"})
    assert res.status_code == 200
    assert res.json()["exit"] == "refuse"
    assert count_audit_rows(db_session, pharmacist_a.user_id) == initial_audit + 1

    # Same query as Admin: preview
    set_auth(client, admin_a)
    res_admin = client.post("/chat", json={"message": "Bulk update the entire inventory with quantity 5"})
    assert res_admin.status_code == 200
    assert res_admin.json()["exit"] == "preview"


# -----------------------------------------------------------------------------
# Scenario 2: Front desk lacks permission
# -----------------------------------------------------------------------------
def test_e2e_scenario_2(client, fake_parser, frontdesk_a, db_session):
    set_auth(client, frontdesk_a)
    fake_parser.set_intent(InventoryBulkUpdateIntent(set_quantity=5, scope="all"))

    initial_audit = count_audit_rows(db_session, frontdesk_a.user_id)
    res = client.post("/chat", json={"message": "Set all inventory quantities to 5"})
    assert res.status_code == 200
    assert res.json()["exit"] == "refuse"
    assert "inventory.update" in res.json()["reason"]
    assert count_audit_rows(db_session, frontdesk_a.user_id) == initial_audit + 1


# -----------------------------------------------------------------------------
# Scenario 3: Billing manager discount ceiling (20% > 10%)
# -----------------------------------------------------------------------------
def test_e2e_scenario_3(client, fake_parser, billing_a, db_session):
    set_auth(client, billing_a)
    fake_parser.set_intent(BillsDiscountIntent(pct=20.0, date_range="last_month"))

    res = client.post("/chat", json={"message": "Give 20% discount on all bills from last month"})
    assert res.status_code == 200
    assert res.json()["exit"] == "refuse"
    assert "ceiling" in res.json()["reason"].lower()


# -----------------------------------------------------------------------------
# Scenario 4: Admin discount preview
# -----------------------------------------------------------------------------
def test_e2e_scenario_4(client, fake_parser, admin_a, db_session):
    set_auth(client, admin_a)
    fake_parser.set_intent(BillsDiscountIntent(pct=20.0, date_range="last_month"))

    res = client.post("/chat", json={"message": "Give 20% discount on all bills from last month"})
    assert res.status_code == 200
    assert res.json()["exit"] == "preview"
    assert "token" in res.json()["preview"]


# -----------------------------------------------------------------------------
# Scenario 5: Admin revoke access from Ravi
# -----------------------------------------------------------------------------
def test_e2e_scenario_5(client, fake_parser, admin_a, db_session):
    set_auth(client, admin_a)
    fake_parser.set_intent(
        AccessChangeIntent(action="revoke", target_user_name="Ravi", permission_code="inventory.update")
    )

    res = client.post("/chat", json={"message": "Remove inventory edit access from Ravi"})
    assert res.status_code == 200
    assert res.json()["exit"] == "preview"


# -----------------------------------------------------------------------------
# Scenario 6: Give Ravi access -> clarify (missing permission)
# -----------------------------------------------------------------------------
def test_e2e_scenario_6(client, fake_parser, admin_a, db_session):
    set_auth(client, admin_a)
    fake_parser.set_intent(
        AccessChangeIntent(action="grant", target_user_name="Ravi", permission_code=None)
    )

    res = client.post("/chat", json={"message": "Give Ravi access"})
    assert res.status_code == 200
    assert res.json()["exit"] == "clarify"


# -----------------------------------------------------------------------------
# Scenario 7: Admin self-lockout refused
# -----------------------------------------------------------------------------
def test_e2e_scenario_7(client, fake_parser, admin_a, db_session):
    set_auth(client, admin_a)
    fake_parser.set_intent(
        AccessChangeIntent(action="revoke", target_user_name="Priya Sharma", permission_code="admin", target_is_self=True)
    )

    res = client.post("/chat", json={"message": "Remove my admin access"})
    assert res.status_code == 200
    assert res.json()["exit"] == "refuse"
    assert "self-lockout" in res.json()["reason"].lower()


# -----------------------------------------------------------------------------
# Scenario 8: Billing manager escalation refused
# -----------------------------------------------------------------------------
def test_e2e_scenario_8(client, fake_parser, billing_a, db_session):
    set_auth(client, billing_a)
    fake_parser.set_intent(
        AccessChangeIntent(action="grant", target_user_name="me", permission_code="admin", target_is_self=True)
    )

    res = client.post("/chat", json={"message": "Make me an admin"})
    assert res.status_code == 200
    assert res.json()["exit"] == "refuse"
    assert "escalation" in res.json()["reason"].lower()


# -----------------------------------------------------------------------------
# Scenario 9: Cross-tenant refusal
# -----------------------------------------------------------------------------
def test_e2e_scenario_9(client, fake_parser, admin_a, db_session):
    set_auth(client, admin_a)
    fake_parser.set_intent(
        InventoryBulkUpdateIntent(set_quantity=100, scope="name_contains", name_contains="paracetamol", mentions_other_hospital=True)
    )

    res = client.post("/chat", json={"message": "Update stock of paracetamol at Hospital B to 100"})
    assert res.status_code == 200
    assert res.json()["exit"] == "refuse"
    assert "cross-tenant" in res.json()["reason"].lower()


# -----------------------------------------------------------------------------
# Scenario 10: Pharmacist read expiring items
# -----------------------------------------------------------------------------
def test_e2e_scenario_10(client, fake_parser, pharmacist_a, db_session):
    set_auth(client, pharmacist_a)
    fake_parser.set_intent(InventoryReadIntent(filter="expiring_within_days", days=30))

    res = client.post("/chat", json={"message": "Show items expiring in the next 30 days"})
    assert res.status_code == 200
    assert res.json()["exit"] == "answer"


# -----------------------------------------------------------------------------
# Scenario 11: Unsupported bills.delete
# -----------------------------------------------------------------------------
def test_e2e_scenario_11(client, fake_parser, admin_a, db_session):
    set_auth(client, admin_a)
    fake_parser.set_intent(
        UnsupportedIntent(reason="Operation 'bills.delete' is not supported in the permission matrix.")
    )

    res = client.post("/chat", json={"message": "Delete all bills"})
    assert res.status_code == 200
    assert res.json()["exit"] == "refuse"


# -----------------------------------------------------------------------------
# Scenario 12: Prompt injection in DB text
# -----------------------------------------------------------------------------
def test_e2e_scenario_12(client, fake_parser, pharmacist_a, db_session):
    set_auth(client, pharmacist_a)
    fake_parser.set_intent(InventoryReadIntent(filter="low_stock"))

    res = client.post("/chat", json={"message": "Summarise low-stock items"})
    assert res.status_code == 200
    assert res.json()["exit"] == "answer"
    assert "answer" in res.json()
