# Deploy Resolve on Vercel with Supabase

The website and FastAPI API stay in one Vercel project. Supabase stores the anonymous sessions, order/payment state, conversation, request ledger and chat locks. No Supabase Auth or browser database client is needed.

## 1. Create the Supabase project

1. Create a project in your Supabase account and wait for the database to become ready.
2. Open the project's SQL Editor.
3. Copy the entire contents of `supabase/migrations/202610130001_resolve_sessions.sql` into a new query and run it.
4. In the project's API settings, copy its HTTPS project URL and create/copy a **secret** key beginning with `sb_secret_`. A legacy **service_role** JWT also works. A publishable/anon key does not work for this backend.

Use a dedicated demo project. The migration creates only `resolve_sessions`, `resolve_creation_limits` and five `resolve_*` functions. It enables RLS, denies direct table access, and grants function execution only to `service_role`. Do not grant the functions to `anon` or `authenticated` to fix a connection error.

## 2. Verify it from PyCharm

Keep your existing Gemini configuration. Add these values to the project's local `.env`:

```dotenv
STORAGE_BACKEND=supabase
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_SECRET_KEY=YOUR_SERVER_ONLY_SECRET_KEY
```

Never put the secret key in `frontend/`, a public document, GitHub or a chat message. The app sends it to Supabase from the Python backend only.

Run the verification script using the project interpreter:

```powershell
.\.venv\Scripts\python.exe scripts/check_supabase.py
```

It creates temporary simulated sessions, checks shared state, idempotency, payment timing and reset, then deletes its sessions. It prints no keys or session tokens and does not call Gemini.

Restart `run.py`, open the website, and confirm `/health` reports:

```json
{"status":"ok","persistence":"supabase"}
```

The response includes other fields too. A `503` means shared storage is unavailable or not configured; the deployed app will not switch to local memory.

## 3. Put the project in a repository

Create a GitHub repository containing the **contents of `resolve-backend/`**. Its root should contain:

```text
app/
frontend/
supabase/
scripts/
requirements.txt
pyproject.toml
vercel.json
.python-version
.gitignore
```

Keep `.env`, `.venv`, `.vercel`, caches and credentials out of the repository. `.env.example` is safe to include because its key values are blank. If you upload through GitHub's website, exclude private files yourself; `.gitignore` does not filter manual uploads.

## 4. Import the repository in Vercel

1. Choose Add New Project and import the repository.
2. Select the directory containing `requirements.txt` and `app/` as the project Root Directory. If you created the repository as described above, this is the repository root.
3. The framework is FastAPI. Keep Build Command and Output Directory overrides unset.
4. Add the following **server environment variables**, selecting the deployment environments you will use:

| Variable | Value |
|---|---|
| `STORAGE_BACKEND` | `supabase` |
| `SUPABASE_URL` | Your Supabase HTTPS project URL |
| `SUPABASE_SECRET_KEY` | Your backend secret key |
| `GEMINI_API_KEY` | Your Gemini key, if using live interpretation |
| `GEMINI_MODEL` | Your available Flash model; default `gemini-3.8-flash` |
| `ALLOW_CHAT_FALLBACK` | `true` for a labelled fallback when Gemini is unavailable |

Do not give these keys a frontend/public variable prefix. Leave `FRONTEND_DIR` and `CORS_ORIGINS` unset: the bundled frontend and API use the same origin. Vercel imports `app.main:app` directly; it does not run `run.py` or load your local `.env`.

5. Deploy. The included files select Python 3.12, the FastAPI entrypoint, a 60-second API execution limit and CDN serving for static frontend assets. `vercel.json` excludes private environment files, tests and local caches from the function bundle.

## 5. Check the production link

- Open `/health`: expect HTTP `200`, `status: ok`, and `persistence: supabase`.
- Open `/`: the automatic checkout sequence should begin.
- Ask a payment question. Check the visible reply source; `gemini_configured` in health only means a key is present. A successful live reply must report `gemini`.
- Wait for the merchant confirmation and open the receipt.
- Refresh: the same browser session should resume rather than create a new payment.
- Try Replay and the alternate scenarios in demo controls.
- Open the **production** URL in an incognito window to verify judges can use it without signing in or requesting access.

Use a separate Supabase project for preview deployments if you want preview data isolated from production. Projects sharing the same database also share the demo capacity and creation limits.

## How shared storage works

Each session is a bounded JSON record with a private token hash and a separate database revision. A mutation reads the row, applies the existing controller and saves only if its revision is unchanged. On a conflict it re-reads and rechecks eligibility. Session creation, quota checks and reset are database transactions.

Timers use Unix timestamps supplied by the database, not a process-local monotonic clock. Polling or chat applies due simulated events; no long-running timer or background job needs to survive a function restart.

Chat uses a shared, expiring lease while Gemini is called. Other instances may still update payment evidence or consent. The final reply is rendered against a newly read case. An abandoned lease can be reclaimed after the configured model timeout plus 30 seconds; a superseded worker cannot publish its old reply.

Sessions expire after 30 minutes without activity. Expired rows and old creation-limit records are cleaned during session creation. This prototype stores conversation history in one session record for a small demo; it is not a production payment system.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/health` returns `503` | Project URL, backend secret key, Data API availability, and whether the migration was run successfully. |
| Website loads but cannot create a session | Supabase readiness and environment variables in the deployed environment. Redeploy after changing variables. |
| Asset or homepage returns `404` | Project Root Directory, included `frontend/`, and entrypoint configuration. |
| Chat says fallback | Gemini key, model availability and quota. Payment tracking remains available. |
| Chat returns `409` while a reply is pending | Let the existing reply finish, then retry the same request. After a crashed function, an abandoned chat lease expires automatically. |
| Browser asks for a Vercel login | Verify the production deployment's access settings and retest in incognito. |

The adaptation has local and mocked integration verification. Your hosted Supabase connection, Vercel build and live Gemini call must still be checked with your accounts. Hosting can still have cold starts, database availability limits and API quotas; this change removes reliance on one warm server's memory.

If you choose Supabase Free, its projects can pause after one week of inactivity. Check that the project is active before evaluation and restore it from the dashboard if paused. See [Supabase pricing](https://supabase.com/pricing).

## Primary documentation

- [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi)
- [Supabase API keys](https://supabase.com/docs/guides/getting-started/api-keys)
- [Supabase database functions](https://supabase.com/docs/guides/database/functions)
- [Supabase Data API](https://supabase.com/docs/guides/api)
