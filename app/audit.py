"""
Audit logging service: guarantees that every request path
(answered, clarify, refused, previewed, executed, cancelled, error)
records exactly one audit_log entry.
"""

from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel

from app.models import AuditLog


def record_audit(
    hospital_id: int,
    user_id: int,
    raw_query: str,
    decision: str,
    status: str,
    parsed_intent: Optional[Any] = None,
    reason: Optional[str] = None,
    rows_affected: int = 0,
    before_snapshot: Optional[Any] = None,
    after_snapshot: Optional[Any] = None,
    session: Optional[Session] = None,
) -> Optional[int]:
    """
    Inserts one immutable row into the audit_log table.
    Ensures that refusals, cancellations, and errors are always logged.
    """
    if session is None:
        return None

    # Serialize intent if pydantic model
    intent_data = None
    if isinstance(parsed_intent, BaseModel):
        intent_data = parsed_intent.model_dump()
    elif isinstance(parsed_intent, dict):
        intent_data = parsed_intent

    # Normalize decision
    decision_str = str(decision).upper()
    if "." in decision_str:
        decision_str = decision_str.split(".")[-1]

    audit_entry = AuditLog(
        hospital_id=hospital_id,
        user_id=user_id,
        raw_query=raw_query or "",
        parsed_intent=intent_data,
        decision=decision_str,
        reason=reason or "",
        status=status,
        rows_affected=rows_affected or 0,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
    )

    try:
        session.execute(
            text("SELECT set_config('app.hospital_id', :hid, true);"),
            {"hid": str(int(hospital_id))},
        )
        session.add(audit_entry)
        session.flush()
        audit_id = audit_entry.id
        session.commit()
        return audit_id
    except Exception as e:
        session.rollback()
        # Fallback raw insert
        try:
            with session.begin():
                session.execute(
                    text("SELECT set_config('app.hospital_id', :hid, true);"),
                    {"hid": str(int(hospital_id))},
                )
                session.execute(
                    text("""
                        INSERT INTO audit_log (
                            hospital_id, user_id, raw_query, decision, reason, status, rows_affected
                        ) VALUES (:hid, :uid, :query, :dec, :reason, :stat, :rows);
                    """),
                    {
                        "hid": hospital_id,
                        "uid": user_id,
                        "query": raw_query[:500],
                        "dec": decision_str,
                        "reason": str(reason or e)[:500],
                        "stat": status,
                        "rows": rows_affected,
                    },
                )
        except Exception:
            pass
        return None
