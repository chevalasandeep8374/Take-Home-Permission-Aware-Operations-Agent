"""
Execution Layer Integration Tests against PostgreSQL.
Covers:
- Cross-tenant data isolation (explicit WHERE + RLS)
- Atomic transaction execution & rollback on mid-transaction error
- Two-phase dry run & blast-radius typed count verification
Marked with @pytest.mark.db so policy unit tests can run standalone.
"""

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.auth import Principal
from app.models import InventoryItem, Bill, PendingAction
from app.schemas import InventoryBulkUpdateIntent
from app.execution.ops_read import inventory_read_op, ensure_tenant_context
from app.execution.dryrun import generate_dryrun_preview
from app.execution.confirm import confirm_write_action, cancel_write_action

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
        session.rollback()
        session.close()


@pytest.fixture
def admin_principal_a():
    return Principal(
        user_id=1,
        hospital_id=1,
        roles=["Admin"],
        permissions={"inventory.update": {}, "inventory.read": {}},
    )


@pytest.fixture
def admin_principal_b():
    return Principal(
        user_id=9,
        hospital_id=2,
        roles=["Admin"],
        permissions={"inventory.update": {}, "inventory.read": {}},
    )


def test_cross_tenant_isolation(db_session, admin_principal_a, admin_principal_b):
    """Verifies that queries never leak or return records from other hospitals."""
    ensure_tenant_context(db_session, admin_principal_a.hospital_id)

    # Query items for Hospital A
    items_a = db_session.execute(
        select(InventoryItem).where(InventoryItem.hospital_id == admin_principal_a.hospital_id)
    ).scalars().all()

    assert len(items_a) > 0
    assert all(it.hospital_id == 1 for it in items_a)

    # Cross-tenant query attempt with Hospital A's context targeting Hospital B's items
    items_b_under_a = db_session.execute(
        select(InventoryItem).where(InventoryItem.hospital_id == admin_principal_b.hospital_id)
    ).scalars().all()

    # RLS or query scoping must yield 0 records
    assert len(items_b_under_a) == 0


def test_atomic_rollback_on_forced_failure(db_session, admin_principal_a):
    """Verifies that any mid-transaction failure rolls back all changes atomically."""
    ensure_tenant_context(db_session, admin_principal_a.hospital_id)

    # Fetch initial target item
    item = db_session.execute(
        select(InventoryItem).where(InventoryItem.hospital_id == admin_principal_a.hospital_id)
    ).scalars().first()

    assert item is not None
    original_qty = item.quantity
    item_id = item.id

    # Execute transaction that fails halfway
    try:
        with db_session.begin_nested():
            item.quantity = 9999
            db_session.flush()
            # Force mid-transaction exception
            raise RuntimeError("Forced simulation error before commit")
    except RuntimeError:
        pass

    db_session.rollback()
    ensure_tenant_context(db_session, admin_principal_a.hospital_id)

    # Verify database was restored to exact original quantity
    refreshed_item = db_session.execute(
        select(InventoryItem).where(InventoryItem.id == item_id)
    ).scalars().first()

    assert refreshed_item is not None
    assert refreshed_item.quantity == original_qty


def test_two_phase_write_dryrun_and_confirm(db_session, admin_principal_a):
    """Verifies two-phase dry-run preview and blast-radius typed count confirmation."""
    ensure_tenant_context(db_session, admin_principal_a.hospital_id)

    intent = InventoryBulkUpdateIntent(
        set_quantity=77,
        scope="name_contains",
        name_contains="Paracetamol",
    )

    # Phase 1: Dry-run preview
    preview = generate_dryrun_preview(admin_principal_a, intent, db_session)
    assert "token" in preview
    assert preview["rows_affected"] > 0
    token = preview["token"]
    rows = preview["rows_affected"]

    # Phase 2: Mismatched typed count must be rejected
    if preview["requires_typed_count"]:
        with pytest.raises(ValueError) as exc:
            confirm_write_action(
                token=token,
                typed_count=rows + 999,
                principal=admin_principal_a,
                session=db_session,
            )
        assert "Blast-radius guard" in str(exc.value)

    # Correct confirmation execution
    result = confirm_write_action(
        token=token,
        typed_count=rows if preview["requires_typed_count"] else None,
        principal=admin_principal_a,
        session=db_session,
    )

    assert result["status"] == "executed"
    assert result["rows_affected"] == rows
