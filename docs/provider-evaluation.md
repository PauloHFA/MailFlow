# Resend/Brevo staging evaluation

## Status

The Resend and Brevo HTTP adapters are implemented and covered by deterministic
`httpx.MockTransport` tests. No authenticated staging evaluation has been performed:
this workspace does not contain provider API keys, a verified sender domain, or
staging inboxes. Do not treat the mock tests as evidence of provider acceptance,
domain alignment, inbox placement, or customer-mail rendering.

The application defaults to Mailpit. To enable either real provider, set `EMAIL_PROVIDER`
and its API key in an untracked `.env`/secret manager. Keep `APP_ENV=staging` and
`TEST_RECIPIENT_ALLOWLIST` restricted to controlled test inboxes. Never add keys to
this repository.

## Adapter contract comparison

| Capability | Resend | Brevo |
|---|---|---|
| API endpoint | `POST https://api.resend.com/emails` | `POST https://api.brevo.com/v3/smtp/email` |
| Authentication | `Authorization: Bearer` | `api-key` |
| HTML and plain text | `html`, `text` | `htmlContent`, `textContent` |
| Message identifier | `id` | `messageId` |
| Custom headers | `headers` | `headers` |
| Attachments | Base64 PDF, 5 MiB aggregate cap | Base64 PDF, 5 MiB aggregate cap |
| Retry classification | 408/429/5xx and network errors retried with same key | 429 and pre-send connection failures retried; ambiguous outcomes marked `unknown` |
| Request idempotency | `Idempotency-Key`, 24-hour provider window | No documented request-level key; ambiguous outcomes are not retried |
| Webhook authentication | Svix signature over raw body | Custom shared header configured on the webhook (not a body signature) |

Both adapters reject unsafe headers, unallowlisted staging recipients, invalid
attachments, redirects, malformed success responses, and missing message IDs. This is
a contract-level implementation comparison only; it is not a provider recommendation.

## Required authenticated staging run

Complete this table only after testing with credentials and a sender domain authorized
for this project. Record date, provider account/plan and the test domain, but never
record API keys or personal mailbox addresses.

| Check | Resend | Brevo |
|---|---|---|
| Date / account plan | Not run | Not run |
| Provider dashboard says sender domain verified | Not run | Not run |
| `GET /api/v1/domain/check` SPF / DKIM / DMARC all pass | Not run | Not run |
| Aligned From-domain confirmed in original message headers | Not run | Not run |
| Allowlisted plain-text + HTML test accepted | Not run | Not run |
| Inbox/spam sampled in Gmail, Outlook, Yahoo, iCloud | Not run | Not run |
| Promotional List-Unsubscribe and one-click behavior | Not run | Not run |
| Bounce/complaint webhook authentication and suppression | Not run | Not run |
| Mobile and Outlook rendering reviewed | Not run | Not run |
| Provider limits, quotas, and observed latency recorded | Not run | Not run |
| Decision / operator / evidence reference | Pending | Pending |

Do not send to non-test recipients until every domain/authentication and safety check is
complete. Provider webhooks require their own provider-specific authentication adapter;
the generic MailFlow webhook contract must not be used as a substitute.

## Implemented webhook and retry behavior

- Resend: configure `RESEND_WEBHOOK_SECRET` from the webhook details page and point
  the webhook at `/api/v1/webhooks/resend`. MailFlow validates `svix-id`,
  `svix-timestamp`, and `svix-signature` against the exact raw body. Subscribe only to
  the mapped email events: delivered, bounced, delivery delayed, complained,
  suppressed, opened, and clicked.
- Brevo: configure `BREVO_WEBHOOK_TOKEN` as a random secret of at least 32 bytes and
  add it as the `X-MailFlow-Brevo-Token` custom header on the Brevo webhook configured
  for `/api/v1/webhooks/brevo`. Brevo documents configurable authentication headers;
  this is shared-token authentication, not an HMAC body signature.
- Both native endpoints deduplicate events and map hard/soft bounce, complaint or
  unsubscribe, delivery, open, and click events. MailFlow adds its message UUID in
  Brevo's `X-Mailin-custom` email header so native events can be correlated.
- Resend sends use the MailFlow message UUID as `Idempotency-Key`. Provider
  idempotency expires after 24 hours; delayed recovery beyond that window is marked
  `unknown` rather than retried. Brevo has no documented request idempotency behavior,
  so ambiguous network responses and worker interruptions are marked `unknown`.
  Reconcile these against the provider dashboard before manually resending.

The authenticated `GET /api/v1/metrics/operations` endpoint exposes queue age,
retrying/processing counts, dead letters, unknown outcomes, and recent provider
failures. The Compose Prometheus service scrapes `/metrics` and loads the operational
rules in `monitoring/alerts.yml`; configure an Alertmanager receiver in a deployment
to route notifications. `GET /ready` probes PostgreSQL and Redis; `GET /health`
remains a liveness-only check.

Provider-specific webhook endpoints and authentication are implemented, but their
behavior must still be verified with authenticated staging deliveries before
production use.

Erasing a recipient now replaces an existing suppression address with a keyed HMAC
digest. Set `SUPPRESSION_HASH_SECRET` to a stable, random value of at least 32 bytes
for API and worker services; rotating it requires a planned migration. This digest is
pseudonymous data, not a legal determination that the record is anonymous. Obtain
privacy counsel's approval for the retention policy.
