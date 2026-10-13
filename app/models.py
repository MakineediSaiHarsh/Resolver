from enum import StrEnum
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class Scenario(StrEnum):
    TIMEOUT_SUCCESS = "timeout_success"
    TIMEOUT_UNRESOLVED = "timeout_unresolved"
    ACTIONABLE_FAILURE = "actionable_failure"
    ALREADY_PAID = "already_paid"
    EXPIRED_ORDER = "expired_order"


class Language(StrEnum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class Intent(StrEnum):
    DEBIT = "debit_concern"
    RETRY = "retry"
    WAITING = "waiting"
    REFUND = "refund"
    SUPPORT = "support"
    STOP = "stop"
    PRODUCT = "product"
    GENERAL = "general"
    OUT_OF_SCOPE = "out_of_scope"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateSession(StrictModel):
    scenario: Scenario = Scenario.TIMEOUT_SUCCESS
    language: Language = Language.EN


class CheckoutRequest(StrictModel):
    request_id: UUID
    method: Literal["upi"] = "upi"


class ChatRequest(StrictModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=1500)


class ConsentRequest(StrictModel):
    allowed: bool


class EvidenceRequest(StrictModel):
    available: bool


class ReplyPlan(StrictModel):
    intent: Intent
    language: Language
    acknowledgement: Literal["neutral", "understand", "frustrating"] = "neutral"


class Message(BaseModel):
    id: int
    role: Literal["merchant", "user", "assistant", "system"]
    text: str
    source: str
    created_at: str


class Event(BaseModel):
    id: int
    type: str
    created_at: str
    data: dict = Field(default_factory=dict)
    simulated: bool = True


class PaymentAttempt(BaseModel):
    id: str
    status: str
    amount_paise: int = 100000
    method: str = "upi"
    error_reason: str | None = None
    refund_status: str | None = None


class Order(BaseModel):
    id: str
    merchant: str = "Thread & Loom"
    product: str = "Everyday cotton shirt"
    quantity: int = 1
    amount_paise: int = 100000
    currency: str = "INR"
    status: str = "unpaid"


class SessionView(BaseModel):
    session_id: str
    scenario: Scenario
    language: Language
    phase: str
    case_status: str
    version: int
    order: Order
    payments: list[PaymentAttempt]
    consent: bool
    evidence_available: bool
    allowed_actions: list[str]
    messages: list[Message]
    events: list[Event]
    last_message_id: int
    last_event_id: int
    receipt: dict | None
    next_poll_ms: int = 1500
    simulated: bool = True
    timing: str = "accelerated_demo"


class SessionCreated(BaseModel):
    session_token: str
    session: SessionView


class ChatResponse(BaseModel):
    reply: Message
    session: SessionView
    chat_source: str
