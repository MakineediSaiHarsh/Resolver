from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4
import asyncio
import hashlib
import re
import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from .chat import ChatProvider, fallback_plan, render_reply
from .config import Settings
from .engine import DomainError, Store
from .repository import MemoryRepository, SupabaseRepository, UnavailableRepository
from .models import (ChatRequest, ChatResponse, CheckoutRequest, ConsentRequest,
                     CreateSession, EvidenceRequest, Intent, Scenario, SessionCreated, SessionView)

bearer = HTTPBearer(auto_error=False, description='Anonymous session token returned when a session is created.')


def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not credentials or credentials.scheme.lower() != 'bearer':
        raise DomainError(401, 'A Bearer session token is required.')
    return credentials.credentials


def create_app(settings: Settings | None = None, store: Store | None = None, provider=None, repository=None):
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application):
        async with httpx.AsyncClient(timeout=max(cfg.gemini_timeout_seconds, cfg.supabase_timeout_seconds)) as client:
            application.state.provider = provider or ChatProvider(cfg, client)
            if repository is not None:
                application.state.repository = repository
            elif store is not None or cfg.resolved_storage == 'memory':
                application.state.repository = MemoryRepository(store or Store(cfg))
            elif cfg.supabase_url and cfg.supabase_secret_key:
                application.state.repository = SupabaseRepository(cfg, client)
            else:
                application.state.repository = UnavailableRepository()
            yield

    api = FastAPI(title='Razorpay Resolve Demo API', version='0.2.0', lifespan=lifespan,
                  description='Simulated payments only. No live Razorpay, WhatsApp, bank, capture, refund or support integration.')
    api.state.store = store
    api.state.settings = cfg

    def repo():
        return api.state.repository

    def client_key(request):
        # Vercel supplies the public client address. Locally, ignore forwarded headers.
        if cfg.require_shared_storage and request.headers.get('x-forwarded-for'):
            return request.headers['x-forwarded-for'].split(',')[0].strip()[:64]
        return request.client.host if request.client else 'unknown'

    @api.middleware('http')
    async def prevent_stale_case_cache(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/') or request.url.path == '/health':
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    if cfg.cors_origins:
        api.add_middleware(CORSMiddleware, allow_origins=list(cfg.cors_origins),
                           allow_methods=['GET', 'POST', 'PATCH', 'DELETE'],
                           allow_headers=['Authorization', 'Content-Type'])

    @api.exception_handler(DomainError)
    async def domain_error(_, exc):
        return JSONResponse(status_code=exc.status, content={'detail': exc.detail},
                            headers={'Retry-After': '60'} if exc.status == 429 else None)

    @api.get('/health')
    async def health():
        healthy = await repo().healthy()
        return JSONResponse(status_code=200 if healthy else 503, content={
            'status': 'ok' if healthy else 'storage_unavailable', 'simulated': True,
            'chat_mode': 'gemini_configured' if cfg.gemini_api_key else 'fallback_no_key',
            'persistence': repo().name})

    @api.get('/api/config')
    async def config():
        return {'merchant': 'Thread & Loom', 'product': 'Everyday cotton shirt',
                'amount_paise': 100000, 'currency': 'INR', 'simulated': True,
                'scenarios': [s.value for s in Scenario], 'persistence': repo().name,
                'chat_mode': 'gemini_configured' if cfg.gemini_api_key else 'fallback_no_key',
                'chat_fallback_enabled': cfg.allow_chat_fallback, 'poll_interval_ms': 1500,
                'timeline_seconds': {'checkout_failure': cfg.checkout_failure_seconds,
                                     'bank_authorisation': cfg.bank_authorisation_seconds,
                                     'payment_capture': cfg.payment_capture_seconds,
                                     'order_confirmation': cfg.order_confirmation_seconds,
                                     'escalation': cfg.escalation_seconds}}

    @api.post('/api/sessions', response_model=SessionCreated, status_code=201)
    async def create_session(body: CreateSession, request: Request):
        s, token = await repo().create(body.scenario, body.language, client_key(request))
        return SessionCreated(session_token=token, session=Store(cfg).snapshot(s))

    @api.get('/api/sessions/{session_id}', response_model=SessionView)
    async def get_session(session_id: UUID, token: str = Depends(require_token),
                          after_message_id: int = Query(0, ge=0), after_event_id: int = Query(0, ge=0)):
        return await repo().transact(str(session_id), token,
            lambda engine, s: engine.snapshot(s, after_message_id, after_event_id))

    @api.post('/api/sessions/{session_id}/checkout', response_model=SessionView)
    async def checkout(session_id: UUID, body: CheckoutRequest, token: str = Depends(require_token)):
        def operation(engine, s):
            if not s.evidence_available:
                raise DomainError(409, 'Current evidence is unavailable. Checkout is held.')
            engine.checkout(s, body.request_id)
            return engine.snapshot(s)
        return await repo().transact(str(session_id), token, operation)

    @api.post('/api/sessions/{session_id}/retry', response_model=SessionView)
    async def retry(session_id: UUID, body: CheckoutRequest, token: str = Depends(require_token)):
        def operation(engine, s):
            engine.checkout(s, body.request_id, retry=True)
            return engine.snapshot(s)
        return await repo().transact(str(session_id), token, operation)

    @api.post('/api/sessions/{session_id}/support', response_model=SessionView)
    async def support(session_id: UUID, token: str = Depends(require_token)):
        def operation(engine, s):
            engine.handoff(s)
            return engine.snapshot(s)
        return await repo().transact(str(session_id), token, operation)

    @api.patch('/api/sessions/{session_id}/consent', response_model=SessionView)
    async def consent(session_id: UUID, body: ConsentRequest, token: str = Depends(require_token)):
        def operation(engine, s):
            if s.consent != body.allowed:
                if not body.allowed:
                    s.message('You have opted out. Resolve will stop further buyer messages.', source='controller')
                s.consent = body.allowed
                s.event('consent_changed', allowed=body.allowed)
            return engine.snapshot(s)
        return await repo().transact(str(session_id), token, operation)

    @api.patch('/api/sessions/{session_id}/demo/evidence', response_model=SessionView)
    async def evidence(session_id: UUID, body: EvidenceRequest, token: str = Depends(require_token)):
        def operation(engine, s):
            if s.evidence_available != body.available:
                s.evidence_available = body.available
                s.event('evidence_availability_changed', available=body.available)
            engine.advance(s)
            return engine.snapshot(s)
        return await repo().transact(str(session_id), token, operation)

    @api.post('/api/sessions/{session_id}/messages', response_model=ChatResponse)
    async def chat(session_id: UUID, body: ChatRequest, token: str = Depends(require_token)):
        sid, request_key, lease_id = str(session_id), str(body.request_id), str(uuid4())
        text_hash = hashlib.sha256(body.text.encode()).hexdigest()

        def prepare(engine, s):
            if not s.consent:
                raise DomainError(403, 'Buyer communication is disabled after opt-out.')
            prior = s.idempotency.get(request_key)
            if prior and (prior['operation'] != 'chat' or prior['text_hash'] != text_hash):
                raise DomainError(409, 'Request ID was already used for different content.')
            if prior and prior.get('reply_id'):
                reply = next(m for m in s.messages if m.id == prior['reply_id'])
                return ChatResponse(reply=reply, session=engine.snapshot(s), chat_source=prior['source'])
            if s.chat_lease_id and s.chat_lease_until > engine.clock():
                raise DomainError(409, 'A chat reply is being prepared. Retry the same request after it completes.')
            if not prior:
                if s.chat_count >= cfg.max_chat_messages:
                    raise DomainError(429, 'Message limit reached. Start a new demo session.')
                s.chat_count += 1
                safe_text = re.sub(r'(?<!\d)\d{12,19}(?!\d)', '[redacted number]', body.text)
                credential_question = bool(re.search(r'\b(otp|pin|password|cvv)\b', safe_text, re.I))
                if credential_question:
                    safe_text = '[Message about payment credentials; values not retained]'
                s.message(safe_text, source='buyer', role='user')
                prior = {'operation': 'chat', 'text_hash': text_hash, 'safe_text': safe_text,
                         'credential_question': credential_question, 'tries': 0}
                s.idempotency[request_key] = prior
            if prior['tries'] >= 2:
                raise DomainError(429, 'Chat retry limit reached. Please check payment status.')
            prior['tries'] += 1
            s.chat_lease_id, s.chat_lease_until = lease_id, engine.clock() + cfg.gemini_timeout_seconds + 30
            return {'context': {'order': s.order.model_dump(mode='json'), 'phase': s.phase,
                                'case_status': s.case_status, 'evidence_available': s.evidence_available,
                                'payments': [p.model_dump(mode='json') for p in s.payments],
                                'allowed_actions': engine.actions(s), 'simulated': True},
                    'history': [{'role': m.role, 'text': m.text} for m in s.messages[-12:]],
                    'text': prior['safe_text'], 'credentials': prior['credential_question'], 'language': s.language}

        prepared = await repo().transact(sid, token, prepare)
        if isinstance(prepared, ChatResponse):
            return prepared

        def release(engine, s):
            if s.chat_lease_id == lease_id:
                s.chat_lease_id, s.chat_lease_until = None, 0

        try:
            if prepared['credentials']:
                plan, source = None, 'controller_credentials'
            else:
                try:
                    plan, source = await asyncio.wait_for(api.state.provider.analyze(
                        prepared['context'], prepared['history'], prepared['text'], prepared['language']),
                        timeout=cfg.gemini_timeout_seconds)
                except TimeoutError:
                    if not cfg.allow_chat_fallback:
                        raise DomainError(503, 'AI chat timed out. Payment status remains available.') from None
                    plan, source = fallback_plan(prepared['text'], prepared['language']), 'fallback_timeout'

            def finish(engine, s):
                if s.chat_lease_id != lease_id:
                    raise DomainError(409, 'This reply was superseded. Check the current case before retrying.')
                try:
                    if not s.consent:
                        raise DomainError(403, 'Communication stopped while the reply was being prepared.')
                    if plan is None:
                        text = 'Do not share your UPI PIN, OTP, password or card security code in this chat. Payment approval belongs inside your payment app.'
                    elif plan.intent == Intent.STOP:
                        text = 'You have opted out. Resolve will stop further buyer messages.'
                    else:
                        s.language = plan.language
                        if plan.intent == Intent.SUPPORT and s.case_status not in {'not_open', 'resolved'}:
                            engine.handoff(s)
                        text = render_reply(plan, s)
                    reply = s.message(text, source=source)
                    s.idempotency[request_key].update(reply_id=reply.id, source=source)
                    if plan is not None and plan.intent == Intent.STOP:
                        s.consent = False
                        s.event('consent_changed', allowed=False)
                    return ChatResponse(reply=reply, session=engine.snapshot(s), chat_source=source)
                finally:
                    release(engine, s)

            # Re-load after Gemini: another worker may have updated the payment or consent.
            return await repo().transact(sid, token, finish)
        except (Exception, asyncio.CancelledError):
            try:
                await repo().transact(sid, token, release)
            except (DomainError, asyncio.CancelledError):
                pass  # Another worker can recover when an abandoned lease expires.
            raise

    @api.post('/api/sessions/{session_id}/reset', response_model=SessionCreated, status_code=201)
    async def reset(session_id: UUID, body: CreateSession, request: Request, token: str = Depends(require_token)):
        s, new_token = await repo().reset(str(session_id), token, body.scenario, body.language, client_key(request))
        return SessionCreated(session_token=new_token, session=Store(cfg).snapshot(s))

    @api.delete('/api/sessions/{session_id}', status_code=204)
    async def delete(session_id: UUID, token: str = Depends(require_token)):
        await repo().delete(str(session_id), token)

    bundled_frontend = Path(__file__).resolve().parent.parent / 'frontend'
    if cfg.frontend_dir or (bundled_frontend / 'index.html').is_file():
        root = Path(cfg.frontend_dir).resolve() if cfg.frontend_dir else bundled_frontend
        if not (root / 'index.html').is_file():
            raise ValueError('FRONTEND_DIR must contain index.html')
        api.mount('/', StaticFiles(directory=str(root), html=True), name='frontend')
    else:
        @api.get('/', include_in_schema=False)
        async def root():
            return RedirectResponse('/docs')
    return api


app = create_app()
