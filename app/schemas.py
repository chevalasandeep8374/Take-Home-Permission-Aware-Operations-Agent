from typing import Literal, Optional, Union, Annotated, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, TypeAdapter


class BaseIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mentions_other_hospital: bool = False
    target_is_self: bool = False


class InventoryReadIntent(BaseIntent):
    type: Literal["inventory_read"] = "inventory_read"
    filter: Literal["expiring_within_days", "low_stock", "by_name", "by_category"] = "expiring_within_days"
    days: Optional[int] = None
    name: Optional[str] = None
    category: Optional[str] = None


class InventoryBulkUpdateIntent(BaseIntent):
    type: Literal["inventory_bulk_update"] = "inventory_bulk_update"
    set_quantity: int
    scope: Literal["all", "category", "name_contains"] = "all"
    category: Optional[str] = None
    name_contains: Optional[str] = None


class InventoryDeleteIntent(BaseIntent):
    type: Literal["inventory_delete"] = "inventory_delete"
    scope: str = "all"


class BillsReadIntent(BaseIntent):
    type: Literal["bills_read"] = "bills_read"
    date_range: Optional[str] = None
    patient: Optional[str] = None


class BillsDiscountIntent(BaseIntent):
    type: Literal["bills_discount"] = "bills_discount"
    pct: float
    date_range: Optional[str] = "all"


class BillsUpdateStatusIntent(BaseIntent):
    type: Literal["bills_update_status"] = "bills_update_status"
    status: str
    date_range: Optional[str] = None


class UsersReadIntent(BaseIntent):
    type: Literal["users_read"] = "users_read"
    name: Optional[str] = None


class AccessChangeIntent(BaseIntent):
    type: Literal["access_change"] = "access_change"
    action: Literal["grant", "revoke"]
    target_user_name: str
    permission_code: Optional[str] = None


class ClarifyIntent(BaseIntent):
    type: Literal["clarify"] = "clarify"
    question: str


class UnsupportedIntent(BaseIntent):
    type: Literal["unsupported"] = "unsupported"
    reason: str


class UndoIntent(BaseIntent):
    type: Literal["undo"] = "undo"
    target: Optional[str] = "last"


class ApprovalIntent(BaseIntent):
    type: Literal["approval"] = "approval"
    action: Literal["list", "request", "approve", "reject"] = "list"
    request_id: Optional[str] = None  # e.g. "REQ-1" or "1"
    note: Optional[str] = None


# Discriminated union of all supported intents
Intent = Annotated[
    Union[
        InventoryReadIntent,
        InventoryBulkUpdateIntent,
        InventoryDeleteIntent,
        BillsReadIntent,
        BillsDiscountIntent,
        BillsUpdateStatusIntent,
        UsersReadIntent,
        AccessChangeIntent,
        ClarifyIntent,
        UnsupportedIntent,
        UndoIntent,
        ApprovalIntent,
    ],
    Field(discriminator="type"),
]

intent_adapter = TypeAdapter(Intent)
