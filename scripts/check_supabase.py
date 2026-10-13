"""Check your configured Supabase project without revealing keys or session tokens."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from uuid import uuid4

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from dotenv import load_dotenv
import httpx
from app.config import Settings
from app.engine import DomainError
from app.models import Language, Scenario
from app.repository import SupabaseRepository


async def check():
    load_dotenv(root / '.env')
    cfg = Settings.from_env()
    if not cfg.supabase_url or not cfg.supabase_secret_key:
        raise RuntimeError('Set SUPABASE_URL and SUPABASE_SECRET_KEY in the local .env first.')
    cfg = replace(cfg, checkout_failure_seconds=.1, bank_authorisation_seconds=1.3,
                  payment_capture_seconds=1.4, order_confirmation_seconds=1.5,
                  escalation_seconds=2, session_ttl_seconds=60)
    identities = []
    async with httpx.AsyncClient() as client:
        first = SupabaseRepository(cfg, client)
        other_worker = SupabaseRepository(cfg, client)
        if not await first.healthy():
            raise RuntimeError('Supabase is not ready. Check the URL/key and run the supplied migration SQL.')
        print('PASS Supabase connection and schema version')
        try:
            s, token = await first.create(Scenario.TIMEOUT_SUCCESS, Language.EN, 'verification-' + str(uuid4()))
            identities.append((s.id, token))
            request_id = uuid4()
            def pay(engine, case):
                engine.checkout(case, request_id)
                return engine.snapshot(case)
            await first.transact(s.id, token, pay)
            await other_worker.transact(s.id, token, pay)
            assert len((await other_worker.transact(s.id, token, lambda engine, case: engine.snapshot(case))).payments) == 1
            print('PASS shared record and checkout idempotency')
            await asyncio.sleep(1.6)
            final = await other_worker.transact(s.id, token, lambda engine, case: engine.snapshot(case))
            assert final.order.status == 'paid' and final.receipt
            print('PASS database-clock payment timeline and receipt')
            replacement, new_token = await other_worker.reset(s.id, token, Scenario.TIMEOUT_UNRESOLVED,
                                                              Language.EN, 'verification-reset-' + str(uuid4()))
            identities.append((replacement.id, new_token))
            try:
                await first.transact(s.id, token, lambda engine, case: engine.snapshot(case))
                raise AssertionError('Old identity was still available after reset')
            except DomainError as exc:
                assert exc.status == 404
            print('PASS atomic reset invalidates the previous identity')
        finally:
            for sid, token in identities:
                try:
                    await first.delete(sid, token)
                except DomainError as exc:
                    if exc.status not in {404, 410}:
                        print('Cleanup unavailable; the short-lived verification session will expire.')
        print('Supabase checks completed. No Gemini or real payment call was made.')


if __name__ == '__main__':
    try:
        asyncio.run(check())
    except (DomainError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
