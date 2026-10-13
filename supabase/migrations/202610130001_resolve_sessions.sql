-- Run once in the SQL Editor of a dedicated Supabase project.
-- Only the backend's secret/service_role key may execute these functions.
begin;

create table if not exists public.resolve_sessions (
  id uuid primary key,
  token_hash text not null check (length(token_hash) = 64),
  payload jsonb not null,
  revision bigint not null default 0,
  expires_at timestamptz not null
);
create index if not exists resolve_sessions_expiry on public.resolve_sessions(expires_at);

create table if not exists public.resolve_creation_limits (
  client_hash text primary key,
  attempts double precision[] not null default '{}',
  updated_at timestamptz not null default now()
);

alter table public.resolve_sessions enable row level security;
alter table public.resolve_creation_limits enable row level security;
revoke all on public.resolve_sessions from public, anon, authenticated, service_role;
revoke all on public.resolve_creation_limits from public, anon, authenticated, service_role;

create or replace function public.resolve_storage_health()
returns jsonb language sql security definer set search_path = '' as $$
  select jsonb_build_object('ok', true, 'schema_version', 1);
$$;

create or replace function public.resolve_session_create(
  p_id uuid, p_token_hash text, p_payload jsonb, p_client_hash text,
  p_limit integer, p_max_sessions integer, p_ttl_seconds double precision,
  p_replace_id uuid default null, p_replace_hash text default null
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_now timestamptz;
  epoch double precision;
  previous public.resolve_sessions%rowtype;
  recent double precision[];
  saved jsonb;
begin
  -- Serializes capacity and rate-limit checks across all app instances.
  perform pg_advisory_xact_lock(724621130001::bigint);
  v_now := clock_timestamp();
  epoch := extract(epoch from v_now);
  if p_ttl_seconds <= 0 or p_limit < 1 or p_max_sessions < 1
     or length(p_token_hash) <> 64 or length(p_client_hash) <> 64
     or p_payload->>'id' is distinct from p_id::text
     or p_payload->>'token_hash' is distinct from p_token_hash
     or p_payload->>'clock' is distinct from 'unix'
     or p_payload->>'schema_version' is distinct from '1' then
    return jsonb_build_object('ok', false, 'error', 'invalid_payload');
  end if;

  -- Retrying this exact backend operation does not consume another creation quota.
  select * into previous from public.resolve_sessions where id = p_id;
  if found then
    if previous.token_hash <> p_token_hash then
      return jsonb_build_object('ok', false, 'error', 'forbidden');
    end if;
    return jsonb_build_object('ok', true, 'payload', previous.payload);
  end if;

  if p_replace_id is not null then
    select * into previous from public.resolve_sessions where id = p_replace_id for update;
    if not found then return jsonb_build_object('ok', false, 'error', 'not_found'); end if;
    if previous.token_hash is distinct from p_replace_hash then
      return jsonb_build_object('ok', false, 'error', 'forbidden');
    end if;
    if previous.expires_at <= v_now then
      return jsonb_build_object('ok', false, 'error', 'expired');
    end if;
  end if;

  delete from public.resolve_sessions where expires_at <= v_now;
  delete from public.resolve_creation_limits where updated_at < v_now - interval '1 minute';
  select attempts into recent from public.resolve_creation_limits where client_hash = p_client_hash;
  select coalesce(array_agg(item), '{}'::double precision[]) into recent
    from unnest(coalesce(recent, '{}'::double precision[])) as item where item > epoch - 60;
  if cardinality(recent) >= p_limit then
    return jsonb_build_object('ok', false, 'error', 'rate_limit');
  end if;
  if (select count(*) from public.resolve_sessions) - (case when p_replace_id is null then 0 else 1 end) >= p_max_sessions then
    return jsonb_build_object('ok', false, 'error', 'capacity');
  end if;

  saved := jsonb_set(jsonb_set(p_payload, '{created}', to_jsonb(epoch)), '{touched}', to_jsonb(epoch));
  if p_replace_id is not null then delete from public.resolve_sessions where id = p_replace_id; end if;
  insert into public.resolve_sessions(id, token_hash, payload, expires_at)
    values(p_id, p_token_hash, saved, v_now + make_interval(secs => p_ttl_seconds));
  insert into public.resolve_creation_limits(client_hash, attempts, updated_at)
    values(p_client_hash, array_append(recent, epoch), v_now)
    on conflict (client_hash) do update set attempts = excluded.attempts, updated_at = excluded.updated_at;
  return jsonb_build_object('ok', true, 'payload', saved);
end;
$$;

create or replace function public.resolve_session_read(p_id uuid, p_token_hash text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare item public.resolve_sessions%rowtype; v_now timestamptz;
begin
  select * into item from public.resolve_sessions where id = p_id;
  if not found then return jsonb_build_object('ok', false, 'error', 'not_found'); end if;
  if item.token_hash is distinct from p_token_hash then return jsonb_build_object('ok', false, 'error', 'forbidden'); end if;
  v_now := clock_timestamp();
  if item.expires_at <= v_now then return jsonb_build_object('ok', false, 'error', 'expired'); end if;
  return jsonb_build_object('ok', true, 'payload', item.payload, 'revision', item.revision,
                           'now', extract(epoch from v_now));
end;
$$;

create or replace function public.resolve_session_save(
  p_id uuid, p_token_hash text, p_expected_revision bigint,
  p_payload jsonb, p_ttl_seconds double precision
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare item public.resolve_sessions%rowtype; v_now timestamptz; saved jsonb;
begin
  select * into item from public.resolve_sessions where id = p_id for update;
  if not found then return jsonb_build_object('ok', false, 'error', 'not_found'); end if;
  if item.token_hash is distinct from p_token_hash then return jsonb_build_object('ok', false, 'error', 'forbidden'); end if;
  v_now := clock_timestamp();
  if item.expires_at <= v_now then return jsonb_build_object('ok', false, 'error', 'expired'); end if;
  if item.revision <> p_expected_revision then return jsonb_build_object('ok', false, 'error', 'conflict'); end if;
  if p_ttl_seconds <= 0 or p_payload->>'id' is distinct from p_id::text
     or p_payload->>'token_hash' is distinct from p_token_hash
     or p_payload->>'clock' is distinct from 'unix'
     or p_payload->>'schema_version' is distinct from '1' then
    return jsonb_build_object('ok', false, 'error', 'invalid_payload');
  end if;
  saved := jsonb_set(p_payload, '{touched}', to_jsonb(extract(epoch from v_now)));
  update public.resolve_sessions set payload = saved, revision = revision + 1,
    expires_at = v_now + make_interval(secs => p_ttl_seconds) where id = p_id;
  return jsonb_build_object('ok', true, 'revision', item.revision + 1);
end;
$$;

create or replace function public.resolve_session_delete(p_id uuid, p_token_hash text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare item public.resolve_sessions%rowtype;
begin
  select * into item from public.resolve_sessions where id = p_id for update;
  if not found then return jsonb_build_object('ok', false, 'error', 'not_found'); end if;
  if item.token_hash is distinct from p_token_hash then return jsonb_build_object('ok', false, 'error', 'forbidden'); end if;
  delete from public.resolve_sessions where id = p_id;
  return jsonb_build_object('ok', true);
end;
$$;

-- Postgres grants function execution to PUBLIC by default. Explicitly remove it.
revoke execute on function public.resolve_storage_health() from public, anon, authenticated;
revoke execute on function public.resolve_session_create(uuid,text,jsonb,text,integer,integer,double precision,uuid,text) from public, anon, authenticated;
revoke execute on function public.resolve_session_read(uuid,text) from public, anon, authenticated;
revoke execute on function public.resolve_session_save(uuid,text,bigint,jsonb,double precision) from public, anon, authenticated;
revoke execute on function public.resolve_session_delete(uuid,text) from public, anon, authenticated;
grant execute on function public.resolve_storage_health() to service_role;
grant execute on function public.resolve_session_create(uuid,text,jsonb,text,integer,integer,double precision,uuid,text) to service_role;
grant execute on function public.resolve_session_read(uuid,text) to service_role;
grant execute on function public.resolve_session_save(uuid,text,bigint,jsonb,double precision) to service_role;
grant execute on function public.resolve_session_delete(uuid,text) to service_role;

notify pgrst, 'reload schema';
commit;
