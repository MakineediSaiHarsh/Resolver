# Razorpay Resolve backend

Complete WhatsApp-style payment-resolution demo, served by one FastAPI app. The frontend is plain HTML/CSS/JavaScript, with no React, build step or separate frontend server. It uses synthetic ₹1,000 UPI purchases and isolated anonymous sessions. Supabase shared storage is included for Vercel deployments. No live payments, WhatsApp connection, bank access, automatic real refunds or real support delivery is configured.

For Vercel, follow [DEPLOYMENT.md](DEPLOYMENT.md): run the Supabase migration, configure server environment variables and import this folder as a FastAPI project. The same frontend/API contract works locally and on Vercel.

## Run locally

Use Python 3.12, matching the deployment configuration. Open a terminal in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe run.py
```

Open `http://127.0.0.1:8000/` for the website. The bundled `frontend/` folder is served automatically when `FRONTEND_DIR` is blank. Interactive API documentation remains at `/docs`, health at `/health`, and public demo settings at `/api/config`.

In PyCharm, open this folder as a project, select its virtualenv interpreter, install `requirements.txt`, then right-click `run.py` and choose Run. Keep the working directory set to this project folder. If you already have this backend running, stop and restart it after adding the frontend files and updated `app/main.py`. Keep your existing `.env`; do not replace your configured key with the example file.

## Experience the website

1. A merchant conversation and ₹1,000 product card appear. The first click animates automatically after a short introduction. Choose Pause to stop the opening or Start now to begin immediately.
2. A Razorpay-style checkout opens, UPI is selected, and its Pay button is clicked automatically. You can also click it yourself.
3. The backend records the payment timeout. The checkout shows the failed confirmation and returns to the conversation; a simulated bank debit notification appears.
4. Resolve explains the current payment state. Type a question or choose a suggested question to call the backend chat endpoint.
5. Server-timed evidence advances through authorisation, capture and merchant confirmation. The receipt becomes available only when the backend returns it.
6. Choose Replay to restart, or open the controls icon to select another scenario, change the starting language, simulate unavailable evidence or turn buyer communication off.

Refreshing resumes the same session while its token and in-memory backend state exist. Replaying replaces the session and invalidates its old token. Browser input is rendered as text, not HTML. The website displays the actual chat source; a configured key is not advertised as a successful Gemini connection before a response succeeds.

For live Gemini interpretation, put your key in `.env` as `GEMINI_API_KEY` and restart the server. Do not paste it into frontend JavaScript or commit `.env`. `GEMINI_MODEL` defaults to `gemini-3.8-flash` and can be changed to an available compatible model.

With no key, the backend still works using a labelled local fallback. Each chat response reports `chat_source`: `gemini`, `fallback_no_key`, `fallback_provider_error`, `fallback_timeout`, or `controller_credentials`. `gemini_configured` in health means a key is present, not that a live call has been verified.

## What the backend does

- Creates a separate order, payment history, conversation and event history for each visitor.
- Returns a session ID and a random bearer token. Subsequent access requires that token.
- Simulates checkout failure, a debit notification, late authorisation, capture and merchant order confirmation.
- Holds retry while an outcome is uncertain and exposes it after an actionable failure.
- Rechecks the latest case after every model response, so a slow AI reply uses newly confirmed payment evidence.
- Handles opt-out, unavailable evidence, support preparation, reset and duplicate requests.
- Exposes a receipt only when a captured payment and a paid merchant order are recorded.
- Supports English, Hindi and Hinglish response copy.

## Conversation design

Gemini interprets a free-form question and returns a validated intent/language/acknowledgement plan. The backend renders the financial explanation from current case evidence and approved copy. The model has no payment tools and no authority to alter amounts, capture, refund or order status. Invalid output falls back explicitly when fallback is enabled.

This is bounded AI interpretation with controlled response copy; it does not stream unrestricted model-written financial claims. The fallback is a small local intent matcher and must not be presented as a live Gemini response.

## Timed simulation

The default successful timeout scenario advances from checkout start:

| Time | Event |
|---|---|
| 0 seconds | UPI attempt starts |
| 2 seconds | Attempt fails with `bank_response_timeout` |
| 2.4 seconds | Simulated bank debit alert appears |
| 3 seconds | Resolve's initial message appears |
| 35 seconds | Simulated bank authorisation |
| 37 seconds | Payment capture |
| 38 seconds | Merchant order confirmation and receipt |

Timings are accelerated demo settings, not actual Razorpay or bank SLAs. They are measured from checkout, independently of visitor activity. Status polling and chat requests apply due server-timed events; no long sleeping request or durable job queue is used.

## Scenarios

- `timeout_success`: uncertain payment eventually captured and order confirmed.
- `timeout_unresolved`: confirmation stays unresolved; support handoff is prepared at 45 seconds.
- `actionable_failure`: insufficient-funds failure allows an eligible UPI retry.
- `already_paid`: another simulated attempt pays the order; the earlier late attempt is tracked through gateway refund processing. Bank credit is not claimed.
- `expired_order`: order expires while the payment remains uncertain; recovery is blocked.

## Frontend connection

The bundled plain HTML/CSS/JS frontend calls `POST /api/sessions`, stores the returned session ID and token in `sessionStorage`, then attaches `Authorization: Bearer <session_token>` to case requests. User and assistant text are rendered with `textContent`.

Use a new `crypto.randomUUID()` for each intended checkout, retry or chat action. Reuse the same request ID when retrying the same network request. Reusing an ID for different content returns `409`.

Poll `GET /api/sessions/{id}` about every 1.5 seconds. Pass `after_message_id` and `after_event_id` using the last received cursors to receive only new entries. Deduplicate messages/events by ID and display the returned current session status. A replayed chat request returns the original reply ID; it should not be displayed twice.

Read `allowed_actions` rather than deciding retry eligibility in the browser. Show the global simulated-payment label and the actual `chat_source` mode. The bank debit event is an in-app demo notification; it is not proof of a real bank balance.

For same-origin hosting, leave `FRONTEND_DIR` blank to serve the bundled `frontend/`. An absolute path to another folder containing `index.html` overrides it. API and documentation routes take precedence over static assets. For a separate frontend during development, set `CORS_ORIGINS` to the permitted origins.

## Deployment and limitations

With no Supabase configuration, local `STORAGE_BACKEND=auto` uses in-memory storage; run one worker, and restarts erase these local sessions. With `STORAGE_BACKEND=supabase` and the URL/secret key configured, sessions persist in shared Postgres records and work across multiple app instances. Vercel always requires shared storage and does not silently fall back to memory. Sessions expire after 30 minutes of inactivity in either mode.

Supabase timing uses the database's Unix clock. Updates use an independent database revision to reject stale writes. Creation quotas and reset are database transactions. Chat uses an expiring shared lease, then re-reads evidence/consent after the model responds. Payment events still advance on API requests, so no background timer must remain alive.

On a Python web host, install `requirements.txt`, set the environment variables, bind the host to `0.0.0.0` and use the assigned `PORT`. Do not assume local files persist on the host. TLS, host limits and proxy settings must be configured for the chosen service.

Creation limits, session capacity and chat limits bound the demo. The limits are not a complete production abuse-prevention system. Use synthetic information in this demo. Production requires merchant authentication, validated payment integrations and webhooks, consent records, durable storage, operational support and model evaluation.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

Tests use controllable clocks and mocked Gemini/Supabase HTTP boundaries, covering payment states, isolation, idempotency, unavailable evidence, opt-out, delayed replies, invalid model output and multiple app instances sharing records. No real payment or live Gemini call is performed by these tests.

To check the actual migration in a local embedded Postgres engine, optionally install Node.js and run from `supabase/tests`:

```powershell
npm install
npm test
```

These SQL checks use PGlite only as a development tool; the deployed Python app does not need Node.js or PGlite. To verify your hosted Supabase project after setting its variables in `.env`, run `python scripts/check_supabase.py` using the project interpreter. That script creates and removes its own temporary demo records.

## Primary references

- [FastAPI](https://fastapi.tiangolo.com/)
- [Gemini Interactions API](https://ai.google.dev/api/interactions-api)
- [Gemini key management](https://ai.google.dev/gemini-api/docs/api-key)
- [Razorpay late authorisation](https://razorpay.com/docs/payments/payments/late-authorisation/)
