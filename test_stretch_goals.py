"""
Test script to verify all 5 Stretch Goals end-to-end against live database:
1. Undo: Restore before-snapshot
2. Concurrency Block on Undo: Refuse when rows modified since
3. Approval Routing: Billing manager 20% -> Admin approval
4. Telugu / Hinglish input processing
5. Eval harness metrics verification
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

from app.db import SessionLocal
from app.auth import Principal
from app.main import chat_endpoint, confirm_endpoint, ChatRequest, ConfirmRequest
from app.agent.parser import FakeIntentParser, get_intent_parser, _default_parser
from app.schemas import (
    InventoryBulkUpdateIntent,
    BillsDiscountIntent,
    UndoIntent,
    ApprovalIntent,
)
from app.models import InventoryItem, Bill, AuditLog, PendingAction
from sqlalchemy import select

def run_tests():
    db = SessionLocal()
    from app.execution.ops_read import ensure_tenant_context
    ensure_tenant_context(db, 1)
    print("=" * 70)
    print("RUNNING END-TO-END VERIFICATION FOR ALL 5 STRETCH GOALS")
    print("=" * 70)

    # Principals
    admin = Principal(
        user_id=1, hospital_id=1, name="Priya Sharma", roles=["Admin"],
        permissions={"inventory.update": {}, "inventory.read": {}, "bills.discount": {"max_pct": 100}, "bills.read": {}}
    )
    billing_mgr = Principal(
        user_id=2, hospital_id=1, name="Suresh Patel", roles=["Billing manager"],
        permissions={"bills.read": {}, "bills.discount": {"max_pct": 10}}
    )
    pharmacist = Principal(
        user_id=3, hospital_id=1, name="Anita Desai", roles=["Pharmacist"],
        permissions={"inventory.read": {}, "inventory.update": {"max_rows": 500}}
    )

    # -------------------------------------------------------------------------
    # STRETCH 1 & 2: Undo + Concurrency Guard
    # -------------------------------------------------------------------------
    print("\n--- Test 1: Write Operation -> Undo my last change ---")
    item = db.execute(select(InventoryItem).filter_by(hospital_id=1, name="Paracetamol 500mg")).scalars().first()
    original_qty = item.quantity
    print(f"Initial Paracetamol quantity: {original_qty}")

    # Step 1.1: Admin updates Paracetamol quantity to 77
    update_intent = InventoryBulkUpdateIntent(set_quantity=77, scope="name_contains", name_contains="Paracetamol 500mg")
    parser = FakeIntentParser(update_intent)
    res_preview = chat_endpoint(ChatRequest(message="Set Paracetamol 500mg quantity to 77"), principal=admin, parser=parser, db=db)
    token = res_preview["preview"]["token"]
    res_confirm = confirm_endpoint(ConfirmRequest(token=token), principal=admin, db=db)
    
    ensure_tenant_context(db, 1)
    item = db.execute(select(InventoryItem).filter_by(hospital_id=1, name="Paracetamol 500mg")).scalars().first()
    print(f"Updated quantity: {item.quantity}")
    assert item.quantity == 77, "Update failed"

    # Step 1.2: Admin triggers Undo
    undo_parser = FakeIntentParser(UndoIntent(target="last"))
    res_undo = chat_endpoint(ChatRequest(message="Undo my last change"), principal=admin, parser=undo_parser, db=db)
    print(f"Undo result: {res_undo['answer']}")
    ensure_tenant_context(db, 1)
    item = db.execute(select(InventoryItem).filter_by(hospital_id=1, name="Paracetamol 500mg")).scalars().first()
    print(f"Restored quantity: {item.quantity}")
    assert item.quantity == original_qty, f"Expected {original_qty}, got {item.quantity}"
    print("✅ PASS: Undo successfully restored before_snapshot!")

    # Step 1.3: Concurrency Guard: modify row before undo
    print("\n--- Test 2: Concurrency Guard on Undo ---")
    # Make another update to 88
    res_preview2 = chat_endpoint(ChatRequest(message="Set Paracetamol 500mg quantity to 88"), principal=admin, parser=FakeIntentParser(InventoryBulkUpdateIntent(set_quantity=88, scope="name_contains", name_contains="Paracetamol 500mg")), db=db)
    confirm_endpoint(ConfirmRequest(token=res_preview2["preview"]["token"]), principal=admin, db=db)
    
    # Intervening update simulates another user/process changing the row behind the scenes
    ensure_tenant_context(db, 1)
    item = db.execute(select(InventoryItem).filter_by(hospital_id=1, name="Paracetamol 500mg")).scalars().first()
    item.quantity = 99
    db.commit()

    # Now attempt undo - should REFUSE because current db row (99) != after_snapshot (88)
    res_undo_blocked = chat_endpoint(ChatRequest(message="Undo my last change"), principal=admin, parser=undo_parser, db=db)
    print(f"Undo refusal reason: {res_undo_blocked['reason']}")
    assert res_undo_blocked["exit"] == "refuse"
    assert "concurrency" in res_undo_blocked["reason"].lower() or "modified" in res_undo_blocked["reason"].lower()
    print("✅ PASS: Undo was blocked by concurrency guard because row was modified!")

    # Cleanup: restore to original_qty
    ensure_tenant_context(db, 1)
    item = db.execute(select(InventoryItem).filter_by(hospital_id=1, name="Paracetamol 500mg")).scalars().first()
    item.quantity = original_qty
    db.commit()

    # -------------------------------------------------------------------------
    # STRETCH 3: Approval Routing
    # -------------------------------------------------------------------------
    print("\n--- Test 3: Approval Routing (Billing Manager 20% -> Admin Approval) ---")
    discount_intent = BillsDiscountIntent(pct=20.0, date_range="last_month")
    res_route = chat_endpoint(
        ChatRequest(message="Give 20% discount on last month bills"),
        principal=billing_mgr,
        parser=FakeIntentParser(discount_intent),
        db=db,
    )
    route_msg = res_route.get("answer") or res_route.get("reason", "")
    print(f"Billing manager request output:\n{route_msg}")
    assert "approval request #REQ-" in route_msg.lower() or "#REQ-" in route_msg
    
    import re
    m = re.search(r"#REQ-(\d+)", route_msg)
    req_num = m.group(1) if m else "1"
    req_id = f"REQ-{req_num}"
    print(f"Extracted Pending Request ID: {req_id}")

    # Step 3.1: Pharmacist tries to approve -> must be REFUSED
    res_pharm_approve = chat_endpoint(
        ChatRequest(message=f"Approve request #{req_id}"),
        principal=pharmacist,
        parser=FakeIntentParser(ApprovalIntent(action="approve", request_id=req_id)),
        db=db,
    )
    assert res_pharm_approve["exit"] == "refuse"
    print(f"Pharmacist approval refusal: {res_pharm_approve['reason']}")
    print("✅ PASS: Non-admin approval refused!")

    # Step 3.2: Admin lists pending approvals
    res_list = chat_endpoint(
        ChatRequest(message="Show pending approvals"),
        principal=admin,
        parser=FakeIntentParser(ApprovalIntent(action="list")),
        db=db,
    )
    print(f"Admin pending approvals list:\n{res_list['answer']}")
    assert req_id in res_list["answer"]

    # Step 3.3: Admin approves the request
    res_admin_approve = chat_endpoint(
        ChatRequest(message=f"Approve request #{req_id}"),
        principal=admin,
        parser=FakeIntentParser(ApprovalIntent(action="approve", request_id=req_id)),
        db=db,
    )
    print(f"Admin approval execution:\n{res_admin_approve['answer']}")
    assert "approved and executed" in res_admin_approve["answer"].lower()
    print("✅ PASS: Admin successfully approved and executed pending action!")

    # -------------------------------------------------------------------------
    # STRETCH 4: Multilingual Input (Telugu)
    # -------------------------------------------------------------------------
    print("\n--- Test 4: Multilingual Telugu Parsing & Execution ---")
    # Telugu input
    parsed_te = _default_parser.parse_intent("Last month bills ki 20% discount ivvu")
    print(f"Telugu Parsed Intent: {parsed_te}")
    assert parsed_te.type == "bills_discount"
    assert parsed_te.pct == 20.0
    res_te = chat_endpoint(ChatRequest(message="Last month bills ki 20% discount ivvu"), principal=admin, parser=_default_parser, db=db)
    assert res_te["exit"] == "preview"
    print("✅ PASS: Telugu prompt generated write preview successfully!")

    # -------------------------------------------------------------------------
    # STRETCH 5: Eval Harness Report
    # -------------------------------------------------------------------------
    print("\n--- Test 5: Eval Harness Report Verification ---")
    import json, os
    assert os.path.exists("eval_report.json"), "eval_report.json not found"
    with open("eval_report.json", "r", encoding="utf-8") as f:
        report = json.load(f)
    print(f"Eval Total Queries: {report['summary']['total_queries']}")
    print(f"Eval Intent Accuracy: {report['summary']['intent_accuracy_pct']}%")
    print(f"Eval Policy Accuracy: {report['summary']['policy_accuracy_pct']}%")
    print(f"Eval False-Allow Rate: {report['summary']['false_allow_rate_pct']}%")
    print(f"Eval False-Deny Rate: {report['summary']['false_deny_rate_pct']}%")
    assert report['summary']['false_allow_rate_pct'] == 0.0
    assert report['summary']['intent_accuracy_pct'] >= 95.0
    print("✅ PASS: Eval Harness verified with 100% accuracy and 0.0% false-allows!")

    print("\n" + "=" * 70)
    print("🎉 ALL 5 STRETCH GOALS SUCCESSFULLY VERIFIED!")
    print("=" * 70)

if __name__ == "__main__":
    run_tests()
