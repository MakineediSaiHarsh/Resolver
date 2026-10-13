from __future__ import annotations
import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4
from .config import Settings
from .models import Event, Language, Message, Order, PaymentAttempt, Scenario, SessionView


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class DomainError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


@dataclass
class Session:
    id: str
    token_hash: str = field(repr=False)
    scenario: Scenario
    language: Language
    created: float
    touched: float
    order: Order
    phase: str = 'shopping'
    case_status: str = 'not_open'
    consent: bool = True
    evidence_available: bool = True
    support_requested: bool = False
    version: int = 0
    payments: list[PaymentAttempt] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    timeline: list[tuple[float, str]] = field(default_factory=list)
    idempotency: dict = field(default_factory=dict)
    chat_count: int = 0
    chat_lease_id: str | None = None
    chat_lease_until: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    chat_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def message(self, text, source='controller', role='assistant'):
        if role == 'assistant' and not self.consent:
            return None
        msg = Message(id=len(self.messages)+1, role=role, text=text,
                      source=source, created_at=timestamp())
        self.messages.append(msg)
        return msg

    def event(self, kind, **data):
        self.version += 1
        self.events.append(Event(id=len(self.events)+1, type=kind,
                                 created_at=timestamp(), data=data))


class Store:
    def __init__(self, settings: Settings, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.sessions: dict[str, Session] = {}
        self.creates: dict[str, list[float]] = {}

    def create(self, scenario, language, client_key):
        now = self.clock()
        self.sessions = {k: s for k, s in self.sessions.items()
                         if now-s.touched < self.settings.session_ttl_seconds}
        self.creates = {k: [t for t in v if now-t < 60] for k, v in self.creates.items()
                        if any(now-t < 60 for t in v)}
        ip_key = hashlib.sha256(client_key.encode()).hexdigest()
        recent = self.creates.setdefault(ip_key, [])
        if len(recent) >= self.settings.session_creates_per_minute:
            raise DomainError(429, 'Too many new sessions. Try again in a minute.')
        if len(self.sessions) >= self.settings.max_sessions:
            raise DomainError(503, 'Demo session capacity reached. Try again later.')
        recent.append(now)
        token = secrets.token_urlsafe(32)
        sid = str(uuid4())
        session = Session(id=sid, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                          scenario=scenario, language=language, created=now, touched=now,
                          order=Order(id='TL-'+uuid4().hex[:8].upper()))
        session.message('Welcome to Thread & Loom. The Everyday cotton shirt is ₹1,000. This is a simulated purchase.',
                        source='merchant', role='merchant')
        session.event('session_created', scenario=scenario.value)
        self.sessions[sid] = session
        return session, token

    def get(self, sid, token):
        session = self.sessions.get(sid)
        if not session:
            raise DomainError(404, 'Session not found. Start a new demo session.')
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not hmac.compare_digest(digest, session.token_hash):
            raise DomainError(403, 'Invalid session token.')
        now = self.clock()
        if now-session.touched >= self.settings.session_ttl_seconds:
            del self.sessions[sid]
            raise DomainError(410, 'Demo session expired. Start a new session.')
        session.touched = now
        return session

    def advance(self, s):
        # Derive due events from server time; visitor inactivity is not payment evidence.
        if not s.evidence_available:
            return
        now = self.clock()
        while s.timeline and s.timeline[0][0] <= now:
            _, kind = s.timeline.pop(0)
            self.process(s, kind)

    def can_retry(self, s):
        return (s.consent and s.evidence_available and s.order.status == 'unpaid'
                and s.phase == 'actionable_failure' and not s.support_requested
                and not any(p.status in {'created', 'authorized', 'captured'} for p in s.payments))

    def actions(self, s):
        actions = ['check_status', 'inspect_case', 'reset']
        if s.phase == 'shopping' and s.evidence_available:
            actions.append('checkout')
        if self.can_retry(s):
            actions.append('retry')
        if s.consent and s.case_status not in {'not_open', 'resolved'}:
            actions.append('support')
        if s.consent:
            actions.append('chat')
            actions.append('withdraw_consent')
        return actions

    def checkout(self, s, request_id, retry=False):
        key, operation = str(request_id), 'retry' if retry else 'checkout'
        prior = s.idempotency.get(key)
        if prior:
            if prior['operation'] != operation:
                raise DomainError(409, 'Request ID was already used for another operation.')
            return
        if retry and not self.can_retry(s):
            raise DomainError(409, 'Retry is not permitted for the current case.')
        if not retry and s.phase != 'shopping':
            raise DomainError(409, 'Checkout already started. Check the current case instead.')
        now, cfg = self.clock(), self.settings
        s.payments.append(PaymentAttempt(id='pay_demo_'+uuid4().hex[:12], status='created'))
        s.phase = 'processing'
        s.event('checkout_started', method='upi', retry=retry)
        s.idempotency[key] = {'operation': operation}
        if retry:
            s.timeline = [(now+cfg.checkout_failure_seconds, 'retry_captured'),
                          (now+cfg.checkout_failure_seconds+1, 'order_confirmed')]
        else:
            s.timeline = [(now+cfg.checkout_failure_seconds, 'payment_failed'),
                          (now+cfg.checkout_failure_seconds+1, 'resolver_started')]
            if s.scenario != Scenario.ACTIONABLE_FAILURE:
                s.timeline.append((now+cfg.checkout_failure_seconds+.4, 'bank_debit_alert'))
            if s.scenario == Scenario.TIMEOUT_SUCCESS:
                s.timeline += [(now+cfg.bank_authorisation_seconds, 'bank_authorized'),
                               (now+cfg.payment_capture_seconds, 'payment_captured'),
                               (now+cfg.order_confirmation_seconds, 'order_confirmed')]
            elif s.scenario == Scenario.TIMEOUT_UNRESOLVED:
                s.timeline.append((now+cfg.escalation_seconds, 'support_prepared'))
            elif s.scenario == Scenario.ALREADY_PAID:
                s.timeline += [(now+cfg.bank_authorisation_seconds*.6, 'other_attempt_paid'),
                               (now+cfg.bank_authorisation_seconds, 'earlier_refund_initiated'),
                               (now+cfg.order_confirmation_seconds+2, 'refund_processed')]
            elif s.scenario == Scenario.EXPIRED_ORDER:
                s.timeline.append((now+cfg.bank_authorisation_seconds*.5, 'order_expired'))
        s.timeline.sort(key=lambda item: item[0])

    def handoff(self, s):
        if s.case_status == 'not_open':
            raise DomainError(409, 'There is no payment case to hand over yet.')
        if s.case_status == 'resolved':
            return
        if not s.support_requested:
            s.support_requested = True
            s.case_status = 'support_pending'
            s.event('support_prepared', delivered_to_real_support=False)
            s.message('Your payment details and conversation are prepared for support. This demo has not contacted a real support team; the case remains unresolved.')

    def process(self, s, kind):
        if kind == 'payment_failed':
            p = s.payments[0]
            p.status = 'failed'
            p.error_reason = 'insufficient_funds' if s.scenario == Scenario.ACTIONABLE_FAILURE else 'bank_response_timeout'
            s.phase = 'actionable_failure' if s.scenario == Scenario.ACTIONABLE_FAILURE else 'payment_uncertain'
            s.case_status = 'open'
        elif kind == 'bank_debit_alert':
            s.event(kind, amount_paise=100000, bank_balance_verified=False,
                    notification_text='Demo bank alert: ₹1,000 debited.')
            return
        elif kind == 'resolver_started':
            from .chat import status_text
            s.message(status_text(s, s.language))
        elif kind == 'bank_authorized':
            s.payments[0].status = 'authorized'
            s.phase = 'authorized'
            s.message('The simulated bank response has arrived: payment is authorised. Capture and the merchant order confirmation are still pending. Please do not make another payment.')
        elif kind in {'payment_captured', 'retry_captured'}:
            p = s.payments[-1] if kind == 'retry_captured' else s.payments[0]
            if p.status not in {'created', 'authorized'}:
                raise DomainError(409, 'Capture event is invalid for the current attempt.')
            p.status = 'captured'
            s.phase = 'captured'
            s.message('The payment is captured. I am waiting for the merchant to confirm the order; you do not need to pay again.')
        elif kind == 'order_confirmed':
            if not any(p.status == 'captured' for p in s.payments) or s.order.status == 'expired':
                raise DomainError(409, 'Order confirmation requires a captured payment and a valid order.')
            s.order.status, s.phase, s.case_status = 'paid', 'order_confirmed', 'resolved'
            s.message(f'Your ₹1,000 payment is confirmed, and Thread & Loom has confirmed order {s.order.id}. You do not need to make another payment. Your receipt is ready.')
        elif kind == 'other_attempt_paid':
            s.payments.append(PaymentAttempt(id='pay_demo_'+uuid4().hex[:12], status='captured'))
            s.order.status, s.phase = 'paid', 'paid_tracking_earlier'
            s.message('Your order is paid through another simulated attempt. Recovery has stopped. The earlier timed-out attempt is still being tracked.')
        elif kind == 'earlier_refund_initiated':
            s.payments[0].status = 'authorized'
            s.payments[0].refund_status = 'initiated'
            s.phase = 'refund_pending'
            s.message('The earlier payment became authorised after the same order was already paid. The simulated gateway has initiated its refund, representing Razorpay’s documented same-order handling. Bank credit is not verified.')
        elif kind == 'refund_processed':
            s.payments[0].refund_status = 'processed'
            s.phase, s.case_status = 'refund_processed', 'resolved'
            s.message('The gateway record shows the earlier refund as processed. Your order remains paid through the other attempt. This does not verify when the credit appears in your bank account.')
        elif kind == 'order_expired':
            s.order.status, s.phase = 'expired', 'order_expired'
            s.message('This order has expired, so recovery payment is blocked. The unresolved payment concern can be prepared for support.')
        elif kind == 'support_prepared':
            self.handoff(s)
            return
        else:
            raise DomainError(422, 'Unknown simulated event.')
        s.event(kind)

    def snapshot(self, s, after_message_id=0, after_event_id=0):
        receipt = None
        if s.order.status == 'paid' and s.evidence_available:
            captured = next((p for p in s.payments if p.status == 'captured'), None)
            if captured:
                receipt = {'order_id': s.order.id, 'payment_id': captured.id,
                           'amount_paise': 100000, 'currency': 'INR', 'simulated': True}
        return SessionView(session_id=s.id, scenario=s.scenario, language=s.language,
                           phase=s.phase, case_status=s.case_status, version=s.version,
                           order=s.order.model_copy(deep=True),
                           payments=[p.model_copy(deep=True) for p in s.payments],
                           consent=s.consent, evidence_available=s.evidence_available,
                           allowed_actions=self.actions(s),
                           messages=[m for m in s.messages if m.id > after_message_id],
                           events=[e for e in s.events if e.id > after_event_id],
                           last_message_id=len(s.messages), last_event_id=len(s.events), receipt=receipt)
