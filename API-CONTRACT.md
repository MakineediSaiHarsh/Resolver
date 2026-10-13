# Frontend API contract

Base URL: the same origin as the website, or your configured backend URL. Every financial record is simulated.

The API contract is preserved on Vercel. `/health` now verifies shared-storage readiness and reports `persistence: supabase` in shared mode; unavailable storage returns `503`. `/api/config` also identifies the persistence mode. No Supabase URL or secret key is sent to the browser.

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Readiness and configured chat mode |
| GET | `/api/config` | Product, price, scenarios and timings |
| POST | `/api/sessions` | Create an anonymous visitor session |
| GET | `/api/sessions/{id}` | Current state and new messages/events |
| POST | `/api/sessions/{id}/checkout` | Start the simulated UPI attempt |
| POST | `/api/sessions/{id}/retry` | Retry only when the controller permits |
| POST | `/api/sessions/{id}/messages` | Interpret and answer a buyer message |
| POST | `/api/sessions/{id}/support` | Prepare a simulated support handoff |
| PATCH | `/api/sessions/{id}/consent` | Explicit opt-out or demo opt-in |
| PATCH | `/api/sessions/{id}/demo/evidence` | Demonstrate unavailable/restored evidence |
| POST | `/api/sessions/{id}/reset` | Create a new session/token and invalidate the old one |
| DELETE | `/api/sessions/{id}` | Remove the current in-memory session |

## Create a session

```json
{"scenario":"timeout_success","language":"en"}
```

Returns `201` with `session_token` and `session`. Use `session.session_id` as `{id}`. All session routes require `Authorization: Bearer <session_token>`; health/config/create do not.

## Checkout or retry

```json
{"request_id":"86dd1784-330c-41ee-9860-68cf1d3800b1","method":"upi"}
```

The backend fixes the merchant, product, quantity, INR amount and order. The client cannot set a price or mark the order paid. Returns the current session. The same request ID repeats the existing action; a different checkout ID after checkout started returns `409`.

## Chat

```json
{"request_id":"1efdb7f0-39a7-48db-a772-14926fab19b1","text":"Paise kat gaye. Should I pay again?"}
```

Returns `reply`, `session` and `chat_source`. Each message has an increasing `id`, `role`, `text`, `source`, and timestamp. No model-written status is accepted; the explanation is rendered against the latest state after interpretation finishes.

Concurrent chat requests from another tab/worker may return `409` while a reply is being prepared. Retry the same request ID after that reply completes. A crashed worker's chat lease expires after the configured model timeout plus 30 seconds; a stale worker cannot publish a superseded reply. Payment polling and consent changes remain available during model calls.

## Poll

`GET /api/sessions/{id}?after_message_id=3&after_event_id=5`

The current session includes:

- `phase`, `case_status`, `version`, `order` and `payments`.
- `consent`, `evidence_available` and `allowed_actions`.
- New `messages` and `events`, plus their latest cursors.
- `receipt` only after the merchant order is paid with a captured attempt and evidence is available.
- `simulated: true`, `timing: accelerated_demo`, `next_poll_ms: 1500`.

Use `bank_debit_alert` to display the mock notification and `order_confirmed` for the completed-order screen. Authorisation, capture and merchant confirmation are distinct events. A paid order can still be tracking an earlier refund.

## Consent, evidence and reset

Consent body: `{"allowed":false}`. This stops future assistant messages and chat, while payment status remains inspectable.

Evidence body: `{"available":false}`. Financial actions are held; restoring evidence catches up due simulated events.

Reset body uses the create-session schema. Replace the saved session ID/token and reset both cursors to zero.

## Error handling

Errors have a `detail` field. Handle `401/403` for missing/invalid token or opt-out, `404/410` for gone/expired sessions, `409` for blocked actions or request-ID conflicts, `422` for invalid input, `429` for demo limits and `503` for unavailable chat/capacity. Payment-status polling continues when AI chat is unavailable.

Do not expose the Gemini key in browser code. Render text as text, not HTML. Display fallback chat modes honestly.
