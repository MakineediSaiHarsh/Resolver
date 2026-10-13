"""Cross-worker API checks with a mocked Supabase RPC boundary."""
import asyncio
import copy
import json
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4
import httpx
import pytest
from app.config import Settings
from app.main import create_app
from app.models import Intent, Language, ReplyPlan
from app.repository import SupabaseRepository, digest


class Database:
    def __init__(self):
        self.now = 1800000000.0
        self.rows = {}
        self.creates = {}
        self.conflict_once = False
        self.outage = False

    async def handle(self, request):
        await asyncio.sleep(0)  # Requests on two app instances can interleave.
        assert request.headers['apikey'] == 'sb_secret_test_server_only'
        assert 'authorization' not in request.headers
        if self.outage:
            return httpx.Response(503, json={'detail': 'private provider error'})
        name = request.url.path.rsplit('/', 1)[-1]
        p = json.loads(request.content)
        result = self.execute(name, p)
        return httpx.Response(200, json=copy.deepcopy(result))

    def execute(self, name, p):
        if name == 'resolve_storage_health':
            return {'ok': True, 'schema_version': 1}
        if name == 'resolve_session_create':
            old = p.get('p_replace_id')
            if old:
                error = self.access(old, p['p_replace_hash'])
                if error:
                    return error
            times = [t for t in self.creates.get(p['p_client_hash'], []) if t > self.now - 60]
            if len(times) >= p['p_limit']:
                return {'ok': False, 'error': 'rate_limit'}
            if len(self.rows) - bool(old) >= p['p_max_sessions']:
                return {'ok': False, 'error': 'capacity'}
            payload = copy.deepcopy(p['p_payload'])
            payload['created'] = payload['touched'] = self.now
            if old:
                del self.rows[old]
            self.rows[p['p_id']] = {'payload': payload, 'revision': 0, 'expires': self.now + p['p_ttl_seconds']}
            self.creates[p['p_client_hash']] = times + [self.now]
            return {'ok': True, 'payload': payload}
        error = self.access(p['p_id'], p['p_token_hash'])
        if error:
            return error
        row = self.rows[p['p_id']]
        if name == 'resolve_session_read':
            return {'ok': True, 'payload': row['payload'], 'revision': row['revision'], 'now': self.now}
        if name == 'resolve_session_delete':
            del self.rows[p['p_id']]
            return {'ok': True}
        if name == 'resolve_session_save':
            if self.conflict_once and p['p_payload']['phase'] == 'processing':
                self.conflict_once = False
                row['payload']['evidence_available'] = False
                row['revision'] += 1
            if row['revision'] != p['p_expected_revision']:
                return {'ok': False, 'error': 'conflict'}
            row['payload'] = copy.deepcopy(p['p_payload'])
            row['payload']['touched'] = self.now
            row['revision'] += 1
            row['expires'] = self.now + p['p_ttl_seconds']
            return {'ok': True, 'revision': row['revision']}
        raise AssertionError('Unexpected RPC')

    def access(self, sid, token_hash):
        row = self.rows.get(sid)
        if not row:
            return {'ok': False, 'error': 'not_found'}
        if row['payload']['token_hash'] != token_hash:
            return {'ok': False, 'error': 'forbidden'}
        if row['expires'] <= self.now:
            return {'ok': False, 'error': 'expired'}


@asynccontextmanager
async def workers(database, count=2, provider=None, **settings):
    cfg = Settings(storage_backend='supabase', supabase_url='https://test.supabase.co',
                   supabase_secret_key='sb_secret_test_server_only', session_creates_per_minute=100, **settings)
    async with AsyncExitStack() as stack:
        clients = []
        for index in range(count):
            transport_client = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.MockTransport(database.handle)))
            repo = SupabaseRepository(cfg, transport_client)
            app = create_app(cfg, repository=repo, provider=provider if index == 0 else None)
            await stack.enter_async_context(app.router.lifespan_context(app))
            clients.append(await stack.enter_async_context(httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='https://resolve.test')))
        yield clients


async def start(client, scenario='timeout_success'):
    response = await client.post('/api/sessions', json={'scenario': scenario})
    assert response.status_code == 201
    data = response.json()
    return '/api/sessions/' + data['session']['session_id'], {'Authorization': 'Bearer ' + data['session_token']}


async def checkout(client, base, headers, key=None):
    return await client.post(base + '/checkout', headers=headers, json={'request_id': str(key or uuid4()), 'method': 'upi'})


def test_session_and_chat_survive_switching_workers_and_replayed_requests():
    async def check():
        db = Database()
        async with workers(db, count=3) as (a, b, restarted):
            base, headers = await start(a)
            payment = await checkout(a, base, headers)
            assert payment.status_code == 200
            sid = base.rsplit('/', 1)[1]
            assert db.rows[sid]['payload']['timeline'][0][0] > 1800000000
            assert headers['Authorization'].split()[1] not in json.dumps(db.rows)
            db.now += 4
            assert (await b.get(base, headers=headers)).json()['phase'] == 'payment_uncertain'
            key = str(uuid4())
            body = {'request_id': key, 'text': 'Should I pay again?'}
            first = (await b.post(base + '/messages', headers=headers, json=body)).json()
            replay = (await a.post(base + '/messages', headers=headers, json=body)).json()
            assert first['reply']['id'] == replay['reply']['id']
            assert sum(m['role'] == 'user' for m in replay['session']['messages']) == 1
            db.now += 34
            final = (await restarted.get(base, headers=headers)).json()
            assert final['receipt']['amount_paise'] == 100000
            assert final['order']['status'] == 'paid'
            assert (await restarted.get('/health')).json()['persistence'] == 'supabase'
            assert 'chat_lease_id' not in final
    asyncio.run(check())


@pytest.mark.parametrize('scenario,elapsed,status', [
    ('timeout_success', 42, 'paid'), ('timeout_unresolved', 46, 'unpaid'),
    ('actionable_failure', 4, 'unpaid'), ('already_paid', 41, 'paid'), ('expired_order', 18, 'expired')])
def test_every_payment_scenario_uses_shared_records(scenario, elapsed, status):
    async def check():
        db = Database()
        async with workers(db) as (a, b):
            base, headers = await start(a, scenario)
            await checkout(a, base, headers)
            db.now += elapsed
            result = (await b.get(base, headers=headers)).json()
            assert result['order']['status'] == status
            if scenario == 'timeout_unresolved':
                assert result['case_status'] == 'support_pending'
            if scenario == 'already_paid':
                assert result['payments'][0]['refund_status'] == 'processed'
            if scenario == 'actionable_failure':
                response = await b.post(base + '/retry', headers=headers, json={'request_id': str(uuid4())})
                assert response.status_code == 200
                db.now += 3
                assert (await a.get(base, headers=headers)).json()['order']['status'] == 'paid'
    asyncio.run(check())


def test_simultaneous_checkout_and_conflict_recheck_cannot_duplicate_or_overwrite():
    async def check():
        db = Database()
        async with workers(db) as (a, b):
            base, headers = await start(a)
            key = uuid4()
            one, two = await asyncio.gather(checkout(a, base, headers, key), checkout(b, base, headers, key))
            assert one.status_code == two.status_code == 200
            assert one.json()['payments'][0]['id'] == two.json()['payments'][0]['id']
            assert len((await a.get(base, headers=headers)).json()['payments']) == 1
            another, h2 = await start(a)
            db.conflict_once = True
            response = await checkout(a, another, h2)
            assert response.status_code == 409
            state = (await b.get(another, headers=h2)).json()
            assert state['evidence_available'] is False
            assert state['payments'] == []
    asyncio.run(check())


class HoldingProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(self, *args):
        self.started.set()
        await self.release.wait()
        return ReplyPlan(intent=Intent.WAITING, language=Language.EN), 'gemini'


@pytest.mark.parametrize('opt_out', [False, True])
def test_other_worker_payment_and_consent_changes_apply_before_ai_reply(opt_out):
    async def check():
        db, provider = Database(), HoldingProvider()
        async with workers(db, provider=provider) as (a, b):
            base, headers = await start(a)
            await checkout(a, base, headers)
            db.now += 4
            body = {'request_id': str(uuid4()), 'text': 'How long?'}
            task = asyncio.create_task(a.post(base + '/messages', headers=headers, json=body))
            await provider.started.wait()
            duplicate = await b.post(base + '/messages', headers=headers, json=body)
            assert duplicate.status_code == 409
            if opt_out:
                await b.patch(base + '/consent', headers=headers, json={'allowed': False})
            db.now += 34
            await b.get(base, headers=headers)
            provider.release.set()
            result = await task
            if opt_out:
                assert result.status_code == 403
                assert all(m['source'] != 'gemini' for m in (await b.get(base, headers=headers)).json()['messages'])
            else:
                assert result.status_code == 200
                assert 'merchant has confirmed' in result.json()['reply']['text']
            assert db.rows[base.rsplit('/', 1)[1]]['payload']['chat_lease_id'] is None
    asyncio.run(check())


def test_expired_chat_lease_recovers_and_rejects_a_superseded_reply():
    async def check():
        db, provider = Database(), HoldingProvider()
        async with workers(db, provider=provider) as (a, b):
            base, headers = await start(a)
            await checkout(a, base, headers)
            db.now += 4
            task = asyncio.create_task(a.post(base + '/messages', headers=headers,
                                             json={'request_id': str(uuid4()), 'text': 'How long?'}))
            await provider.started.wait()
            db.now += 43
            replacement = await b.post(base + '/messages', headers=headers,
                                       json={'request_id': str(uuid4()), 'text': 'Has the payment arrived?'} )
            assert replacement.status_code == 200
            provider.release.set()
            stale = await task
            assert stale.status_code == 409
            assert all(m['source'] != 'gemini' for m in (await b.get(base, headers=headers)).json()['messages'])
    asyncio.run(check())


def test_reset_expiry_delete_and_invalid_token_apply_across_workers():
    async def check():
        db = Database()
        async with workers(db, max_sessions=1) as (a, b):
            base, headers = await start(a)
            assert (await b.get(base, headers={'Authorization': 'Bearer wrong'})).status_code == 403
            reset = await b.post(base + '/reset', headers=headers, json={'scenario': 'timeout_unresolved'})
            assert reset.status_code == 201
            assert (await a.get(base, headers=headers)).status_code == 404
            data = reset.json()
            new = '/api/sessions/' + data['session']['session_id']
            h2 = {'Authorization': 'Bearer ' + data['session_token']}
            assert (await a.delete(new, headers=h2)).status_code == 204
            assert (await b.get(new, headers=h2)).status_code == 404
            third, h3 = await start(a)
            db.now += 1801
            assert (await b.get(third, headers=h3)).status_code == 410
    asyncio.run(check())


def test_storage_outage_does_not_fall_back_to_process_memory(caplog):
    async def check():
        db = Database()
        db.outage = True
        async with workers(db) as (a, _):
            assert (await a.get('/health')).status_code == 503
            response = await a.post('/api/sessions', json={})
            assert response.status_code == 503
            assert 'No local-memory fallback' in response.json()['detail']
            assert db.rows == {}
    asyncio.run(check())
    assert 'sb_secret_test_server_only' not in caplog.text
    assert 'private provider error' not in caplog.text


def test_vercel_requires_shared_storage_and_credentials_are_not_repr(monkeypatch):
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.delenv('SUPABASE_URL', raising=False)
    monkeypatch.delenv('SUPABASE_SECRET_KEY', raising=False)
    monkeypatch.delenv('SUPABASE_SERVICE_ROLE_KEY', raising=False)
    monkeypatch.delenv('STORAGE_BACKEND', raising=False)
    cfg = Settings.from_env()
    assert cfg.resolved_storage == 'supabase'
    with pytest.raises(ValueError):
        Settings(storage_backend='memory', require_shared_storage=True)
    assert 'private-key-value' not in repr(Settings(supabase_secret_key='private-key-value'))


def test_unconfigured_supabase_serves_frontend_but_holds_case_actions():
    async def check():
        app = create_app(Settings(storage_backend='supabase'))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://resolve.test') as client:
                assert (await client.get('/')).status_code == 200
                assert (await client.get('/health')).status_code == 503
                assert (await client.post('/api/sessions', json={})).status_code == 503
    asyncio.run(check())
