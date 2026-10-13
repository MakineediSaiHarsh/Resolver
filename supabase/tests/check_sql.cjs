const {PGlite} = require('@electric-sql/pglite');
const fs = require('fs');
const path = require('path');
const {randomUUID} = require('crypto');
const assert = require('assert/strict');

const root = path.resolve(__dirname, '../..');
let checks = 0;
function check(actual, expected, label) { assert.deepEqual(actual, expected, label); checks++; }
const hash = 'a'.repeat(64), wrongHash = 'b'.repeat(64);

(async () => {
  const db = new PGlite();
  try {
    await db.exec('create role anon; create role authenticated; create role service_role;');
    const migration = fs.readFileSync(path.join(root, 'supabase/migrations/202610130001_resolve_sessions.sql'), 'utf8');
    await db.exec(migration);
    const query = async (text, values = []) => (await db.query(text, values)).rows;
    const scalar = async (text, values = []) => (await query(text, values))[0].result;
    const payload = (id, phase = 'shopping') => ({id, token_hash: hash, schema_version: 1, clock: 'unix', created: 0, touched: 0, phase});
    const create = (id, client = 'c'.repeat(64), limit = 12, capacity = 200, oldId = null, oldHash = null) => scalar(
      'select public.resolve_session_create($1::uuid,$2::text,$3::jsonb,$4::text,$5::integer,$6::integer,$7::double precision,$8::uuid,$9::text) as result',
      [id, hash, JSON.stringify(payload(id)), client, limit, capacity, 1800, oldId, oldHash]);
    const read = (id, token = hash) => scalar('select public.resolve_session_read($1::uuid,$2::text) as result', [id, token]);
    const save = (id, revision, data, token = hash) => scalar('select public.resolve_session_save($1::uuid,$2::text,$3::bigint,$4::jsonb,$5::double precision) as result', [id, token, revision, JSON.stringify(data), 1800]);

    check(await scalar("select has_function_privilege('anon','public.resolve_storage_health()','execute') as result"), false, 'anon cannot call private health RPC');
    check(await scalar("select has_function_privilege('authenticated','public.resolve_session_read(uuid,text)','execute') as result"), false, 'authenticated cannot read sessions');
    check(await scalar("select has_function_privilege('service_role','public.resolve_session_save(uuid,text,bigint,jsonb,double precision)','execute') as result"), true, 'server can save');
    for (const role of ['anon', 'authenticated', 'service_role']) {
      check(await scalar(`select has_table_privilege('${role}','public.resolve_sessions','SELECT') as result`), false, `${role} cannot read raw session rows`);
    }
    const security = await query("select relname, relrowsecurity from pg_class where relname in ('resolve_sessions','resolve_creation_limits')");
    check(security.length, 2, 'two private tables');
    check(security.every(row => row.relrowsecurity), true, 'RLS enabled');
    await db.exec('set role service_role;');
    check(await scalar('select public.resolve_storage_health() as result'), {ok: true, schema_version: 1}, 'server health RPC');

    const first = randomUUID();
    const created = await create(first, 'c'.repeat(64), 1, 1);
    check(created.ok, true, 'create under service role');
    check(created.payload.created > 1700000000, true, 'database Unix timestamp');
    check((await create(first, 'c'.repeat(64), 1, 1)).ok, true, 'create replay is idempotent');
    check((await read(first, wrongHash)).error, 'forbidden', 'token isolation');
    check((await read(randomUUID())).error, 'not_found', 'missing session');
    let record = await read(first);
    check(record.revision, 0, 'initial revision');
    check(record.now >= created.payload.created, true, 'database clock returned');
    const processing = {...record.payload, phase: 'processing'};
    check((await save(first, 0, processing)).revision, 1, 'atomic save increments revision');
    check((await save(first, 0, {...processing, phase: 'stale'})).error, 'conflict', 'stale writer rejected');
    check((await read(first)).payload.phase, 'processing', 'winner not overwritten');
    check((await save(first, 1, processing, wrongHash)).error, 'forbidden', 'unauthorized save rejected');
    check((await save(first, 1, {...processing, id: randomUUID()})).error, 'invalid_payload', 'identity cannot change');
    check((await save(first, 1, {...processing, clock: 'monotonic'})).error, 'invalid_payload', 'instance-local clock rejected');

    const refusedReset = randomUUID();
    check((await create(refusedReset, 'd'.repeat(64), 12, 1, first, wrongHash)).error, 'forbidden', 'reset token verified');
    check((await read(first)).ok, true, 'failed reset retains old session');
    check((await read(refusedReset)).error, 'not_found', 'failed reset creates nothing');
    check((await create(randomUUID(), 'c'.repeat(64), 1, 1, first, hash)).error, 'rate_limit', 'reset respects creation quota');
    check((await read(first)).ok, true, 'quota failure retains old session');
    check((await create(randomUUID(), 'd'.repeat(64), 12, 1)).error, 'capacity', 'capacity enforced');
    const second = randomUUID();
    check((await create(second, 'd'.repeat(64), 12, 1, first, hash)).ok, true, 'reset fits at capacity atomically');
    check((await read(first)).error, 'not_found', 'reset invalidates old identity');
    check((await read(second)).ok, true, 'new identity readable');

    await db.exec('reset role;');
    await db.exec(migration);
    check((await read(second)).ok, true, 'rerunning migration preserves sessions');
    await db.query("update public.resolve_sessions set expires_at = now() - interval '1 second' where id = $1::uuid", [second]);
    await db.exec('set role service_role;');
    check((await read(second)).error, 'expired', 'expiry enforced by database');
    check((await save(second, 0, payload(second))).error, 'expired', 'expired session cannot be renewed');
    const third = randomUUID();
    check((await create(third, 'e'.repeat(64), 12, 1)).ok, true, 'creation prunes expired capacity');
    check((await read(second)).error, 'not_found', 'expired row removed');
    check((await scalar('select public.resolve_session_delete($1::uuid,$2::text) as result', [third, wrongHash])).error, 'forbidden', 'delete verifies token');
    check((await read(third)).ok, true, 'failed delete preserves session');
    check((await scalar('select public.resolve_session_delete($1::uuid,$2::text) as result', [third, hash])).ok, true, 'authorized deletion');
    check((await read(third)).error, 'not_found', 'deleted session gone');

    await db.exec('set role anon;');
    let denied = false;
    try { await read(third); } catch (error) { denied = /permission denied/.test(error.message); }
    check(denied, true, 'actual anonymous RPC execution denied');
    await db.exec('reset role;');
    const version = (await query('select version() as version'))[0].version;
    console.log(`PASS ${checks} SQL checks: migration, role permissions, clock, revision conflicts, quotas, atomic reset, expiry and deletion`);
  } finally { await db.close(); }
})().catch(error => { console.error(JSON.stringify({message:error.message, position:error.position, internalPosition:error.internalPosition, internalQuery:error.internalQuery, where:error.where, detail:error.detail})); process.exitCode = 1; });
