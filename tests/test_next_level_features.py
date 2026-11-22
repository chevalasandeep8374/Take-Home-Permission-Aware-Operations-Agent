"""
Tests for Next-Level Features:
1. Pre-LLM PHI/PII De-Identification & HIPAA Masking Engine (Safe Harbor compliance)
2. Cryptographic Tamper-Proof Audit Ledger (Hash-Chained Audit Trail & Verification)
"""

import pytest
from app.agent.redactor import PHIRedactor, RedactionResult
from app.audit import record_audit, verify_audit_ledger, compute_entry_hash, GENESIS_HASH
from app.models import AuditLog


def test_phi_redactor_phone_and_aadhaar():
    """Verify phone numbers and Aadhaar IDs are masked with pseudonyms."""
    raw = "Patient contact is +91 9876543210 with Aadhaar 2345 6789 1234"
    result = PHIRedactor.redact(raw)

    assert result.is_redacted is True
    assert "+91 9876543210" not in result.redacted_text
    assert "9876543210" not in result.redacted_text
    assert "2345 6789 1234" not in result.redacted_text
    assert "[PHONE_TOKEN_1]" in result.redacted_text
    assert "[NATIONAL_ID_TOKEN_1]" in result.redacted_text
    assert "PHONE_NUMBER" in result.detected_types
    assert "NATIONAL_ID" in result.detected_types

    # Test restoration
    restored = PHIRedactor.restore_text(result.redacted_text, result.token_map)
    assert "9876543210" in restored
    assert "2345 6789 1234" in restored


def test_phi_redactor_patient_name():
    """Verify patient names in clinical context are de-identified."""
    raw = "Show all unpaid bills for patient Ramesh Patel"
    result = PHIRedactor.redact(raw)

    assert result.is_redacted is True
    assert "Ramesh Patel" not in result.redacted_text
    assert "[PATIENT_PSEUDONYM_1]" in result.redacted_text
    assert "PATIENT_NAME" in result.detected_types

    # Test object restoration
    data = {"query": result.redacted_text, "patient_name": "[PATIENT_PSEUDONYM_1]"}
    restored_obj = PHIRedactor.restore_object(data, result.token_map)
    assert restored_obj["patient_name"] == "Ramesh Patel"


def test_phi_redactor_no_sensitive_info():
    """Verify non-sensitive inventory queries remain un-redacted."""
    raw = "Summarise low-stock items in inventory"
    result = PHIRedactor.redact(raw)

    assert result.is_redacted is False
    assert result.redacted_text == raw
    assert len(result.token_map) == 0
    assert len(result.detected_types) == 0


def test_audit_hash_chain_computation():
    """Verify SHA-256 hash chaining formula is deterministic and sensitive to changes."""
    h1 = compute_entry_hash(
        hospital_id=1,
        user_id=2,
        raw_query="Show low stock",
        decision="EXECUTE",
        status="answered",
        rows_affected=5,
        prev_hash=GENESIS_HASH,
    )
    assert isinstance(h1, str)
    assert len(h1) == 64

    # Different query yields different hash
    h2 = compute_entry_hash(
        hospital_id=1,
        user_id=2,
        raw_query="Show expired items",
        decision="EXECUTE",
        status="answered",
        rows_affected=5,
        prev_hash=GENESIS_HASH,
    )
    assert h1 != h2

    # Chaining h1 into next entry
    h3 = compute_entry_hash(
        hospital_id=1,
        user_id=2,
        raw_query="Give 10% discount",
        decision="PREVIEW",
        status="previewed",
        rows_affected=10,
        prev_hash=h1,
    )
    assert len(h3) == 64
    assert h3 != h1


def test_audit_ledger_verification_and_tamper_detection(db_session):
    """
    End-to-end test of cryptographic hash chain verification and tamper detection.
    1. Insert two chained audit logs.
    2. Verify ledger is valid.
    3. Tamper with one entry's decision in DB.
    4. Verify ledger immediately detects CONTENT_TAMPERED at that exact entry ID.
    """
    # Clear or use isolated hospital_id 999
    test_hid = 1
    test_uid = 1

    id1 = record_audit(
        hospital_id=test_hid,
        user_id=test_uid,
        raw_query="Test query 1",
        decision="EXECUTE",
        status="answered",
        rows_affected=1,
        session=db_session,
    )
    assert id1 is not None

    id2 = record_audit(
        hospital_id=test_hid,
        user_id=test_uid,
        raw_query="Test query 2",
        decision="PREVIEW",
        status="previewed",
        rows_affected=3,
        session=db_session,
    )
    assert id2 is not None

    # Step 2: Verification should be 100% valid
    verification = verify_audit_ledger(db_session, hospital_id=test_hid)
    assert verification["valid"] is True
    assert verification["cryptographically_verified_entries"] >= 2

    # Step 3: Simulate rogue database tamper attack on id1
    entry1 = db_session.get(AuditLog, id1)
    original_decision = entry1.decision
    entry1.decision = "REFUSE" # Tampering with recorded decision!
    db_session.commit()

    # Step 4: Verification must catch tampering!
    tamper_result = verify_audit_ledger(db_session, hospital_id=test_hid)
    assert tamper_result["valid"] is False
    assert tamper_result["error"] == "CONTENT_TAMPERED"
    assert tamper_result["tampered_entry_id"] == id1

    # Cleanup: restore original value
    entry1.decision = original_decision
    db_session.commit()
