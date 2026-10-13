"""The same payment controller, with local or shared session persistence."""
import asyncio
import copy
import hashlib
import logging
import time
import httpx
from .engine import DomainError, Session, Store
from .models import Event, Language, Message, Order, PaymentAttempt, Scenario

logger = logging.getLogger(__name__)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def dump_session(s):
    return {
        'schema_version': 1, 'clock': 'unix', 'id': s.id, 'token_hash': s.token_hash,
        'scenario': s.scenario.value, 'language': s.language.value,
        'created': s.created, 'touched': s.touched, 'order': s.order.model_dump(mode='json'),
        'phase': s.phase, 'case_status': s.case_status, 'consent': s.consent,
        'evidence_available': s.evidence_available, 'support_requested': s.support_requested,
        'version': s.version, 'chat_count': s.chat_count,
        'payments': [p.model_dump(mode='json') for p in s.payments],
        'messages': [m.model_dump(mode='json') for m in s.messages],
        'events': [e.model_dump(mode='json') for e in s.events],
        'timeline': [[due, kind] for due, kind in s.timeline],
        'idempotency': copy.deepcopy(s.idempotency),
        'chat_lease_id': s.chat_lease_id, 'chat_lease_until': s.chat_lease_until
    }


def load_session(data):
    if data.get('schema_version') != 1 or data.get('clock') != 'unix':
        raise ValueError('Unsupported session record')
    return Session(
        id=data['id'], token_hash=data['token_hash'], scenario=Scenario(data['scenario']),
        language=Language(data['language']), created=float(data['created']), touched=float(data['touched']),
        order=Order.model_validate(data['order']), phase=data['phase'], case_status=data['case_status'],
        consent=data['consent'], evidence_available=data['evidence_available'],
        support_requested=data['support_requested'], version=data['version'], chat_count=data['chat_count'],
        payments=[PaymentAttempt.model_validate(p) for p in data['payments']],
        messages=[Message.model_validate(m) for m in data['messages']],
        events=[Event.model_validate(e) for e in data['events']],
        timeline=[(float(due), kind) for due, kind in data['timeline']],
        idempotency=copy.deepcopy(data['idempotency']),
        chat_lease_id=data.get('chat_lease_id'), chat_lease_until=float(data.get('chat_lease_until', 0)))


class MemoryRepository:
    name = 'in_memory_single_worker'

    def __init__(self, engine):
        self.engine = engine

    async def healthy(self):
        return True

    async def create(self, scenario, language, client_key):
        return self.engine.create(scenario, language, client_key)

    async def transact(self, sid, token, operation):
        s = self.engine.get(sid, token)
        async with s.lock:
            self.engine.get(sid, token)  # A reset/delete may have won while this call waited.
            self.engine.advance(s)
            return operation(self.engine, s)

    async def reset(self, sid, token, scenario, language, client_key):
        old = self.engine.get(sid, token)
        async with old.lock:
            self.engine.get(sid, token)
            del self.engine.sessions[sid]
            try:
                return self.engine.create(scenario, language, client_key)
            except DomainError:
                self.engine.sessions[sid] = old
                raise

    async def delete(self, sid, token):
        s = self.engine.get(sid, token)
        async with s.lock:
            self.engine.sessions.pop(sid, None)


class UnavailableRepository:
    name = 'supabase'

    async def healthy(self):
        return False

    async def _unavailable(self, *args, **kwargs):
        raise DomainError(503, 'Shared storage is not configured. Set SUPABASE_URL and SUPABASE_SECRET_KEY on the server.')

    create = transact = reset = delete = _unavailable


class SupabaseRepository:
    name = 'supabase'

    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def rpc(self, name, parameters):
        key = self.settings.supabase_secret_key
        headers = {'apikey': key, 'Content-Type': 'application/json'}
        # New secret keys are not JWTs. Legacy service_role JWTs need the bearer header too.
        if key.startswith('eyJ'):
            headers['Authorization'] = 'Bearer ' + key
        try:
            response = await self.client.post(
                self.settings.supabase_url.rstrip('/') + '/rest/v1/rpc/' + name,
                headers=headers, json=parameters, timeout=self.settings.supabase_timeout_seconds)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or 'ok' not in result:
                raise ValueError('Invalid storage response')
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning('Shared storage unavailable (%s)', type(exc).__name__)
            raise DomainError(503, 'Shared storage is unavailable. No local-memory fallback was used. Please try again.') from None
        if not result['ok']:
            errors = {
                'not_found': (404, 'Session not found. Start a new demo session.'),
                'forbidden': (403, 'Invalid session token.'),
                'expired': (410, 'Demo session expired. Start a new session.'),
                'rate_limit': (429, 'Too many new sessions. Try again in a minute.'),
                'capacity': (503, 'Demo session capacity reached. Try again later.')}
            if result.get('error') == 'conflict':
                return result
            status, detail = errors.get(result.get('error'), (503, 'Shared storage could not complete the request.'))
            raise DomainError(status, detail)
        return result

    async def healthy(self):
        try:
            response = await self.rpc('resolve_storage_health', {})
            return response.get('schema_version') == 1
        except DomainError:
            return False

    async def _create(self, scenario, language, client_key, old_sid=None, old_token=None):
        engine = Store(self.settings, clock=time.time)
        s, token = engine.create(scenario, language, client_key)
        response = await self.rpc('resolve_session_create', {
            'p_id': s.id, 'p_token_hash': s.token_hash, 'p_payload': dump_session(s),
            'p_client_hash': digest(client_key), 'p_limit': self.settings.session_creates_per_minute,
            'p_max_sessions': self.settings.max_sessions, 'p_ttl_seconds': self.settings.session_ttl_seconds,
            'p_replace_id': old_sid, 'p_replace_hash': digest(old_token) if old_token else None})
        return load_session(response['payload']), token

    async def create(self, scenario, language, client_key):
        return await self._create(scenario, language, client_key)

    async def reset(self, sid, token, scenario, language, client_key):
        # Old deletion, new insertion and quota checks happen in one database transaction.
        return await self._create(scenario, language, client_key, sid, token)

    async def delete(self, sid, token):
        await self.rpc('resolve_session_delete', {'p_id': sid, 'p_token_hash': digest(token)})

    async def transact(self, sid, token, operation):
        try:
            async with asyncio.timeout(15):
                for attempt in range(6):
                    record = await self.rpc('resolve_session_read', {'p_id': sid, 'p_token_hash': digest(token)})
                    s = load_session(record['payload'])
                    now = float(record['now'])
                    engine = Store(self.settings, clock=lambda: now)
                    s.touched = now
                    engine.advance(s)
                    failure = None
                    try:
                        result = operation(engine, s)
                    except DomainError as exc:
                        failure = exc
                    saved = await self.rpc('resolve_session_save', {
                        'p_id': sid, 'p_token_hash': digest(token), 'p_expected_revision': record['revision'],
                        'p_payload': dump_session(s), 'p_ttl_seconds': self.settings.session_ttl_seconds})
                    if saved['ok']:
                        if failure:
                            raise failure
                        return result
                    # A different worker changed this row. Re-read and re-run against current facts.
                    await asyncio.sleep(.015 * (attempt + 1))
        except TimeoutError:
            raise DomainError(503, 'Shared storage took too long. Please retry the same request.') from None
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning('Invalid shared session record (%s)', type(exc).__name__)
            raise DomainError(503, 'Shared session data could not be read. Please start a new demo.') from None
        raise DomainError(503, 'The case is receiving simultaneous updates. Please retry the same request.')
