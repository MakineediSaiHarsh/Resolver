# Application verification

Verified on 13 October 2026 with Python 3.12.

## Completed checks

- 41 automated Python tests passed. No test failures. These include all original payment/frontend checks plus independent app instances sharing a mocked Supabase RPC service.
- Real local HTTP smoke check returned `200` from health, created a session, started a UPI checkout and returned a chat reply with `chat_source: fallback_no_key`.
- Exported the generated OpenAPI contract to `openapi.json`.
- Tested session isolation, bearer-token access, expiry, reset, duplicate requests, chat quotas, opt-out and support preparation.
- Tested uncertain-payment retry blocking, actionable-failure retry, late authorisation, capture, merchant confirmation, order expiry and the same-order earlier-refund scenario.
- Tested missing/restored evidence and a slow model response arriving after order confirmation.
- Mocked the Gemini HTTP transport to verify request format, bounded structured output, invalid/forged model output rejection and provider failures.
- Real Chrome browser checks passed on desktop (1440×960) and mobile (390×844): animated checkout, failed confirmation, debit notification, fallback chat, final paid receipt, session recovery after refresh and no horizontal mobile overflow.
- Browser checks also passed for scenario replay, eligible retry, checkout returning after successful retry, unavailable/restored evidence, opt-out/opt-in and simulated support handoff. No browser JavaScript errors were recorded.
- A final focused check verified product image sizing, desktop/mobile screenshots and a typed question sent using Enter, including the displayed buyer message and cleared composer after the response.
- After the shared-storage adaptation, the complete desktop/mobile browser checks passed again using the Supabase repository with a mocked HTTP boundary and a real advancing wall clock.
- 41 additional SQL checks passed against PostgreSQL 18.6 in PGlite 0.5.9. The actual migration was compiled and executed, including service-role access, denied anonymous access, RLS/table permissions, database timestamps, stale revision rejection, capacity/rate limits, atomic reset, expiry and deletion. Re-running the migration preserved an existing session.
- A production Supabase check script is included at `scripts/check_supabase.py`; it was not run against a hosted project because no hosted project credentials were supplied.

## Limits of this verification

A live Gemini call has not been performed. Configure your own key in `.env`, restart the app and verify a response whose `chat_source` is `gemini` before presenting live AI as connected. The health value `gemini_configured` only reports configuration.

A hosted Supabase project and a Vercel deployment have not been created or verified in this adaptation. HTTP integration tests mock Supabase's RPC gateway; SQL tests execute the migration in an embedded Postgres engine. The local engine's major version may differ from your Supabase project. Run the included hosted check script after applying the migration and setting your server variables.

All payment, debit-notification, refund and support events are simulated. No bank, Razorpay, WhatsApp or merchant integration has been called. Browser integration was tested with the local fallback; a real Gemini response still requires verification with your key.

One dependency warning came from Starlette's test client about its future HTTP client migration; it did not affect test results.

## Tested direct dependency versions

| Package | Version |
|---|---|
| FastAPI | 0.143.0 |
| Uvicorn | 0.54.0 |
| HTTPX | 0.28.1 |
| python-dotenv | 1.2.4 |
| pytest | 9.1.1 |

Unconfigured local runs use memory on one worker. Configured Supabase runs use shared records; Vercel requires that mode. Sessions expire after 30 minutes without activity.
