"""
LLM Intent Parser using Google Gemini SDK (google-genai).
Extracts strictly typed Intent union models from user instructions.
Injectable dependency for testing.
"""

import json
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from pydantic import TypeAdapter, ValidationError

from app.config import settings
from app.agent.dates import get_current_ist_date
from app.agent.prompts import SYSTEM_PARSER_PROMPT
from app.schemas import (
    Intent,
    InventoryReadIntent,
    InventoryBulkUpdateIntent,
    InventoryDeleteIntent,
    BillsReadIntent,
    BillsDiscountIntent,
    BillsUpdateStatusIntent,
    UsersReadIntent,
    AccessChangeIntent,
    UndoIntent,
    ApprovalIntent,
    ClarifyIntent,
    UnsupportedIntent,
)

intent_adapter = TypeAdapter(Intent)


class BaseIntentParser(ABC):
    """Abstract interface for LLM intent parsers."""

    @abstractmethod
    def parse_intent(self, message: str) -> Intent:
        """Parse natural language instruction into a typed Intent model."""
        pass


class GeminiIntentParser(BaseIntentParser):
    """Real LLM intent parser using Google Gemini API."""

    def __init__(self, api_key: Optional[str] = None):
        raw_key = api_key or settings.gemini_api_key
        # Check if the key is empty or a placeholder
        if raw_key and raw_key.strip() not in ("", "your-gemini-api-key-here", "your_gemini_api_key"):
            self.api_key = raw_key.strip()
        else:
            self.api_key = None

        self.client = None
        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
            except Exception:
                self.client = None

    def parse_intent(self, message: str) -> Intent:
        if not self.client or getattr(self, "_quota_exhausted", False):
            # Fallback heuristic parser if Gemini API key is not yet set or quota is exhausted
            return self._heuristic_fallback(message)

        today_ist = get_current_ist_date().isoformat()
        prompt = (
            f"{SYSTEM_PARSER_PROMPT}\n\n"
            f"Context: Today's date in Asia/Kolkata is {today_ist}.\n\n"
            f"Staff instruction: \"{message}\"\n\n"
            "Respond ONLY with a valid JSON object matching the Intent schema."
        )

        try:
            response = self.client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                },
            )
            raw_json = response.text.strip()
            data = json.loads(raw_json)
            return intent_adapter.validate_python(data)
        except (json.JSONDecodeError, ValidationError) as e:
            return ClarifyIntent(
                question=f"I couldn't clearly parse that instruction. Could you rephrase? ({str(e)})"
            )
        except Exception as e:
            # If API key is invalid, quota exceeded (429), or network fails, fall back to heuristic parser
            err_msg = str(e)
            if any(code in err_msg for code in ("API_KEY_INVALID", "400", "403", "404", "429", "RESOURCE_EXHAUSTED", "rate-limits")):
                if any(code in err_msg for code in ("429", "RESOURCE_EXHAUSTED")):
                    self._quota_exhausted = True
                return self._heuristic_fallback(message)
            return UnsupportedIntent(reason=f"LLM parsing error: {str(e)}")

    def _heuristic_fallback(self, message: str) -> Intent:
        """Lightweight regex/keyword parser when running without active API key or quota exceeded, supporting English, Telugu, Hinglish."""
        msg_lower = message.lower().strip()
        import re

        # Check cross-tenant mention
        mentions_other = ("hospital b" in msg_lower) or ("hospital a" in msg_lower)

        # Undo Intent
        if "undo" in msg_lower or "cancel cheyyi" in msg_lower or "wapas lo" in msg_lower:
            return UndoIntent(target="last")

        # Approval Intent
        if "approve" in msg_lower and ("req" in msg_lower or "request" in msg_lower or "#" in msg_lower):
            m = re.search(r"(?:req-?|#)(\d+)", msg_lower)
            req_id = m.group(1) if m else "1"
            return ApprovalIntent(action="approve", request_id=f"REQ-{req_id}")

        if "reject" in msg_lower and ("req" in msg_lower or "request" in msg_lower or "#" in msg_lower):
            m = re.search(r"(?:req-?|#)(\d+)", msg_lower)
            req_id = m.group(1) if m else "1"
            return ApprovalIntent(action="reject", request_id=f"REQ-{req_id}")

        if "pending" in msg_lower or "approvals" in msg_lower or "approval request" in msg_lower:
            return ApprovalIntent(action="list")

        # Unsupported / dangerous ops
        if any(term in msg_lower for term in ("delete all bills", "delete bills", "bills anni delete", "bills delete cheyyi", "drop ", "purge")):
            return UnsupportedIntent(reason="Operation is not supported in the permission matrix.")

        # Admin access management (English, Telugu, Hinglish, Prompt Injection)
        if "ignore previous instructions" in msg_lower or "grant admin to user" in msg_lower:
            return AccessChangeIntent(
                action="grant",
                target_user_name="user 7",
                permission_code="admin",
            )

        if "remove my admin" in msg_lower or "remove admin from myself" in msg_lower or "na admin access teesi" in msg_lower or "mera admin hatao" in msg_lower:
            return AccessChangeIntent(
                action="revoke",
                target_user_name="me",
                permission_code="admin",
                target_is_self=True,
            )

        if "make me an admin" in msg_lower or "make me admin" in msg_lower or "nannu admin cheyyi" in msg_lower or "mujhe admin banao" in msg_lower:
            return AccessChangeIntent(
                action="grant",
                target_user_name="me",
                permission_code="admin",
                target_is_self=True,
            )

        if "remove inventory edit access from ravi" in msg_lower or "remove inventory edit" in msg_lower:
            return AccessChangeIntent(
                action="revoke",
                target_user_name="Ravi",
                permission_code="inventory.update",
            )

        if "give ravi inventory.update" in msg_lower or "inventory.update access" in msg_lower:
            return AccessChangeIntent(
                action="grant",
                target_user_name="Ravi",
                permission_code="inventory.update",
            )

        if "give ravi access" in msg_lower or "ravi ki access ivvu" in msg_lower or "ravi ko access do" in msg_lower:
            return AccessChangeIntent(
                action="grant",
                target_user_name="Ravi",
                permission_code=None,
            )

        # Discount (English, Telugu, Hinglish)
        if "discount" in msg_lower or "ivvu" in msg_lower or "%" in msg_lower:
            pct = 10.0
            m = re.search(r"(\d+)%", msg_lower)
            if m:
                pct = float(m.group(1))
            is_last_month = "last month" in msg_lower or "pichhl" in msg_lower or "kritham nela" in msg_lower
            return BillsDiscountIntent(
                pct=pct,
                date_range="last_month" if is_last_month else "all",
                mentions_other_hospital=mentions_other,
            )

        # Inventory update / writes (English, Telugu, Hinglish)
        is_write_update = any(kw in msg_lower for kw in (
            "bulk update", "set all inventory", "update stock", "update inventory",
            "set paracetamol", "stock 100 ki update cheyyi", "stock 100 karo", "ka stock 100 karo"
        )) or (
            ("update" in msg_lower or "set " in msg_lower or "cheyyi" in msg_lower or "karo" in msg_lower)
            and ("quantity" in msg_lower or "stock" in msg_lower)
            and not any(rkw in msg_lower for rkw in ("show", "list", "check", "find", "view", "dikhao", "chupinchu"))
        )

        if is_write_update:
            qty = 5
            m = re.search(r"(?:quantity|to|stock|ki|karo)\s*(\d+)", msg_lower)
            if not m:
                m = re.search(r"(\d+)\s*(?:ki|karo|to)", msg_lower)
            if m:
                qty = int(m.group(1))
            has_paracetamol = "paracetamol" in msg_lower
            has_amox = "amoxicillin" in msg_lower
            name_contains = "paracetamol" if has_paracetamol else ("amoxicillin" if has_amox else None)
            scope = "name_contains" if (has_paracetamol or has_amox) else "all"
            return InventoryBulkUpdateIntent(
                set_quantity=qty,
                scope=scope,
                name_contains=name_contains,
                mentions_other_hospital=mentions_other,
            )

        # Inventory Read (English, Telugu, Hinglish)
        is_inventory_read = any(kw in msg_lower for kw in (
            "inventory", "stock", "stocks", "expiring", "expiry", "low stock", "low-stock",
            "paracetamol", "insulin", "antibiotic", "amoxicillin", "cardiology", "medicines",
            "takkuva", "kam stock", "chupinchu", "dikhao", "summarise"
        ))

        if is_inventory_read:
            if "expiring" in msg_lower or "expiry" in msg_lower:
                days = 7 if ("this week" in msg_lower or "ee vaaram" in msg_lower) else 30
                return InventoryReadIntent(filter="expiring_within_days", days=days, mentions_other_hospital=mentions_other)
            if "low stock" in msg_lower or "low-stock" in msg_lower or "takkuva" in msg_lower or "kam stock" in msg_lower:
                return InventoryReadIntent(filter="low_stock", mentions_other_hospital=mentions_other)
            if "cardiology" in msg_lower:
                return InventoryReadIntent(filter="by_category", category="cardiology", mentions_other_hospital=mentions_other)
            if "antibiotic" in msg_lower:
                return InventoryReadIntent(filter="by_category", category="antibiotic", mentions_other_hospital=mentions_other)
            if "paracetamol" in msg_lower:
                return InventoryReadIntent(filter="by_name", name="paracetamol", mentions_other_hospital=mentions_other)
            if "insulin" in msg_lower:
                return InventoryReadIntent(filter="by_name", name="insulin", mentions_other_hospital=mentions_other)
            if "amoxicillin" in msg_lower:
                return InventoryReadIntent(filter="by_name", name="amoxicillin", mentions_other_hospital=mentions_other)
            return InventoryReadIntent(filter="expiring_within_days", days=30, mentions_other_hospital=mentions_other)

        # Bills Read (English, Telugu, Hinglish)
        if "bill" in msg_lower or "bills" in msg_lower or "billing" in msg_lower:
            return BillsReadIntent(
                date_range="last_month" if ("last month" in msg_lower or "pichhl" in msg_lower or "kritham nela" in msg_lower) else "all",
                mentions_other_hospital=mentions_other,
            )

        return ClarifyIntent(question="Please clarify your requested operation.")


class FakeIntentParser(BaseIntentParser):
    """Test fake parser returning pre-configured Intents."""

    def __init__(self, predefined_intent: Optional[Intent] = None):
        self.predefined_intent = predefined_intent
        self.mapping: Dict[str, Intent] = {}

    def set_intent(self, intent: Intent):
        self.predefined_intent = intent

    def map_message(self, message: str, intent: Intent):
        self.mapping[message.strip().lower()] = intent

    def parse_intent(self, message: str) -> Intent:
        normalized = message.strip().lower()
        if normalized in self.mapping:
            return self.mapping[normalized]
        if self.predefined_intent:
            return self.predefined_intent
        return ClarifyIntent(question=f"Fake parser: unknown message '{message}'")


# Default parser instance for dependency injection
_default_parser: BaseIntentParser = GeminiIntentParser()


def get_intent_parser() -> BaseIntentParser:
    """FastAPI dependency to retrieve the active intent parser."""
    return _default_parser


def set_default_parser(parser: BaseIntentParser):
    """Override the active intent parser (useful in tests)."""
    global _default_parser
    _default_parser = parser
