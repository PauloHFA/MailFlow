# MailFlow

MailFlow is a secure, local-first service for transactional email and scheduled reminders.
The development environment routes SMTP to Mailpit and restricts non-production
recipients to an explicit allowlist.

## Requirements

- Python 3.12+
- Docker Desktop with Docker Compose

## Start the local environment

1. Copy `.env.example` to `.env` and set a local-only `POSTGRES_PASSWORD` and the
   addresses allowed to receive test messages in `TEST_RECIPIENT_ALLOWLIST`
   (comma-separated).
2. Run `docker compose up --build`.
3. Open the API documentation at <http://localhost:8000/docs> and Mailpit at
   <http://localhost:8025>.

The API and Mailpit ports are bound to loopback. PostgreSQL and Redis are available
only to services on the Compose network. Mailpit captures SMTP messages and does not
deliver them to external mailboxes.

In development, test, and staging configurations, a recipient not present in the
allowlist is rejected. An empty allowlist denies all recipients. Do not use the example
password for production.

## Transactional API

Compose applies the Alembic migrations before starting the API. To create a versioned
template:

```json
{
  "name": "appointment-confirmation",
  "subject": "Appointment for {{ name }}",
  "html_body": "<p>Hello {{ name }}</p>",
  "text_body": "Hello {{ name }}",
  "required_variables": ["name"],
  "message_type": "transactional"
}
```

Send this body to `POST /api/v1/templates`. Preview it with
`POST /api/v1/templates/{template_id}/preview` and
`{"variables":{"name":"Ada"}}`.

Send a transactional message with `POST /api/v1/messages`, using a unique
`Idempotency-Key` header:

```http
POST /api/v1/messages HTTP/1.1
Content-Type: application/json
Idempotency-Key: appointment-2026-001

{"recipient":"qa@example.com","template_id":"<template-uuid>","variables":{"name":"Ada"}}
```

The API stores the message before enqueueing it and returns `202` with the message ID.
Reusing the key with the same request returns the same message; reusing it for
different input returns `409`. `GET /api/v1/messages/{message_id}` returns its status
and event types.

Promotional templates cannot be sent through the transactional endpoint.

## Recipients and scheduled campaigns

Import a CSV as multipart form data with a required `email` column and optional `name`
and IANA `timezone` columns:

```powershell
curl.exe -F "file=@recipients.csv;type=text/csv" http://localhost:8000/api/v1/recipients/import
```

An optional consent record can be imported only when all four columns are present and
populated: `consent_purpose`, `legal_basis`, `consent_source`, and `consented_at`.
Consent timestamps must include a timezone. Importing an address alone never creates
consent. Consent can also be recorded and revoked through
`POST /api/v1/recipients/{recipient_id}/consents` and
`DELETE /api/v1/recipients/{recipient_id}/consents/{consent_id}`.

Create a campaign with `POST /api/v1/campaigns`, a list of existing recipient IDs,
template variables, and either a one-time `schedule.send_at` or relative reminder
`schedule.due_at` plus `schedule.days_before`. Timestamps must include a timezone.
Each recipient's stored IANA timezone is used where available; otherwise the schedule
timezone is used. Delivery is moved into the default 08:00–20:00 local send window,
and Celery Beat checks for due occurrences every minute. Promotional campaigns require
an active consent matching the requested purpose. The worker rechecks consent before
delivery and includes RFC 8058 `List-Unsubscribe` and `List-Unsubscribe-Post` headers.

Configure separate random secrets of at least 32 bytes with
`WEBHOOK_SIGNING_SECRET`, `UNSUBSCRIBE_SIGNING_SECRET`, `SUPPRESSION_HASH_SECRET`,
and `OPERATIONS_API_TOKEN`. Set `UNSUBSCRIBE_BASE_URL` to the public endpoint URL;
production requires HTTPS. The canonical webhook endpoint is
`POST /api/v1/webhooks/generic`. Send a JSON event with `event_id`, `event_type`,
`message_id`, and timezone-aware `occurred_at`; supported event types are `delivered`,
`hard_bounce`, `soft_bounce`, `complaint`, `unsubscribed`, `opened`, and `clicked`.
Sign the exact request bytes with HMAC-SHA256 over
`<unix-timestamp>.<raw-body>`, passing the timestamp in `X-MailFlow-Timestamp` and
the lowercase hex digest prefixed by `sha256=` in `X-MailFlow-Signature`.

Provider-native endpoints are also available:

- `POST /api/v1/webhooks/resend` verifies Resend's Svix signature over the exact raw
  request body using `RESEND_WEBHOOK_SECRET`.
- `POST /api/v1/webhooks/brevo` requires a random `BREVO_WEBHOOK_TOKEN` of at least
  32 bytes in `X-MailFlow-Brevo-Token`; configure the same custom header on the Brevo
  webhook. Brevo documents configurable authentication headers, not a native body
  signature, so MailFlow does not describe this as an HMAC signature.

Resend sends use the stable MailFlow message ID as `Idempotency-Key`. Resend documents
a maximum key length of 256 characters and a 24-hour retention window. Brevo has no
documented request-level idempotency contract for transactional email; MailFlow sends
the internal message ID in `X-Mailin-custom` only for webhook correlation. If a
non-idempotent provider's response is ambiguous or its worker stops during processing,
the message is marked `unknown` and is not retried automatically. Reconcile it against
the provider dashboard before taking manual action.

The `GET /health` endpoint is liveness-only. `GET /ready` checks PostgreSQL and Redis
and is used by the Compose API healthcheck. The authenticated
`GET /api/v1/metrics/operations` endpoint reports oldest pending-message age, retrying
and processing counts, dead letters, unknown delivery outcomes, and recent provider
failures. Compose starts Prometheus at <http://localhost:9090>, scrapes the API, and
loads the rules in `monitoring/alerts.yml`. They alert on API scrape failures,
PostgreSQL/Redis readiness, queue age, provider failures, dead letters, and unknown
delivery outcomes. Prometheus exposes firing rules in its Alerts page; configure an
Alertmanager receiver in the deployment to route notifications to the operator's
chosen channel.

Suggested initial thresholds (tune for the deployment):

- `mailflow_oldest_pending_age_seconds > 300`
- `mailflow_provider_failures_last_hour > 0`
- `mailflow_dead_letters > 0`
- `mailflow_unknown_delivery_messages > 0`
- `/ready` returns HTTP 503

`GET /api/v1/unsubscribe?token=...` displays a confirmation and has no side effects.
RFC 8058 clients submit `POST` to the URL with form body
`List-Unsubscribe=One-Click`; only the consent identified by the signed expiring token
is revoked. Tokens expire after one year. Hard bounces and complaints suppress the
address globally; three consecutive soft bounces also suppress it, while a delivery
event resets that streak.

When an erased recipient already has a suppression, MailFlow replaces its email with
a keyed HMAC digest using `SUPPRESSION_HASH_SECRET`. This keeps the address out of the
suppression table while preventing an accidental reimport or send. Keep the same
secret backed up and stable; rotating it requires a planned migration. The digest is
pseudonymous data, not a legal determination that the record is anonymous. Confirm
the retention policy with qualified privacy counsel.

Active campaigns can be paused, resumed, or cancelled with
`POST /api/v1/campaigns/{campaign_id}/pause`, `/resume`, and `/cancel`. Cancelling
marks undispatched schedules as complete and cancels queued/retrying messages. Paused
campaigns retain their pending schedules and messages.

The worker enforces Redis-backed per-provider and per-destination-domain rate limits
per second and per UTC day. Temporary SMTP failures use persisted exponential backoff
(two seconds initially, capped at one hour) for up to five total attempts; permanent
failures and exhausted retries are stored in the `dead_letters` table. A periodic
recovery task safely retries stale Resend messages using the same provider key; other
providers' stale messages are marked `unknown` to avoid a possible duplicate. Optional
warm-up limits are controlled by `WARMUP_ENABLED`, `WARMUP_DAILY_LIMIT`, and
`WARMUP_DOMAIN_DAILY_LIMIT`. Daily bounce and complaint thresholds pause active
promotional campaigns and emit a critical operator log.

## Operations, reports, and domain checks

The scheduler emails an aggregate delivery report as a PDF every day at 00:00 UTC
when `DAILY_REPORT_ENABLED=true` and `REPORT_RECIPIENT_EMAIL` is configured. The report
covers the previous 24 hours and contains message-status and delivery-event counts, not
recipient addresses. Non-production delivery is still restricted by
`TEST_RECIPIENT_ALLOWLIST`. Email attachments are currently limited to validated PDF
files and 5 MB total.

Protect operational and personal-data endpoints with a random `OPERATIONS_API_TOKEN`
of at least 32 bytes. Send it in the `Authorization` header using the Bearer scheme.
Domain authentication checks are available at
`GET /api/v1/domain/check?domain=example.com&dkim_selectors=mail`; repeat
`dkim_selectors` for multiple selectors. The response reports SPF, DKIM, and DMARC
status and outstanding DNS items. DKIM can only be verified when the provider's
selector is known and supplied.

`GET /api/v1/metrics/delivery` returns aggregate message status and event counts for
the previous 24 hours. Optional timezone-aware `start_at` and `end_at` query
parameters select another period of up to 366 days. The
`GET /api/v1/exports/messages.csv` and `GET /api/v1/exports/consents.csv` endpoints
export corresponding records, defaulting to the previous 30 days. Exports require the
operations token, are limited to 50,000 rows and 366 days, and neutralize spreadsheet
formula cells.
The existing `GET /metrics` endpoint continues to expose Prometheus HTTP metrics.

## Email provider configuration

The local default is `EMAIL_PROVIDER=mailpit`; it captures mail and never delivers to
external inboxes. `EMAIL_PROVIDER=resend` or `EMAIL_PROVIDER=brevo` selects the matching
HTTP API adapter for workers and scheduled reports. Set the corresponding
`RESEND_API_KEY` or `BREVO_API_KEY` in the untracked local `.env` file or a secret
manager. Missing keys make application configuration fail rather than silently using
another provider. API credentials are not logged.

Non-production environments still require recipients to be present in
`TEST_RECIPIENT_ALLOWLIST`, including when a real-provider adapter is selected. Before
any real staging send, verify the sender domain with the provider and confirm aligned
SPF, DKIM, and DMARC records using the authenticated operations endpoint. Start with
allowlisted test inboxes only; use `docs/provider-evaluation.md` to record the Resend
and Brevo staging results. The HTTP adapters are tested with mocked transports; these
tests do not claim real-provider delivery or inbox placement.

`MESSAGE_RETENTION_DAYS` controls automatic deletion of messages and their delivery
events/dead letters by the daily worker task. Its default `0` disables automatic
purging. Recipient and consent records are retained until explicitly erased through
the operations-only `DELETE /api/v1/recipients/{recipient_id}` endpoint, which removes
the recipient's messages, events, consent records, and schedules in one database
transaction. Existing suppression entries are retained as keyed HMAC digests so the
erased address cannot be accidentally re-imported or contacted. A successful erasure
returns `204`; unknown IDs return `404`. Configure and preserve
`SUPPRESSION_HASH_SECRET`; the digest is pseudonymous data, and the retention policy
still requires privacy-counsel review.

## Run tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m pytest -m performance
```

The regular suite excludes the separately marked 5,000-message queue load test. Run
`python -m pytest -m performance` to execute that local synthetic load test; it tests
queue dispatch uniqueness, not latency or inbox placement at an external provider.

For a local database, set `DATABASE_URL` to a PostgreSQL connection URL and run
`alembic upgrade head`.

## Current endpoints

- `GET /health` — application liveness.
- `GET /metrics` — Prometheus-format process and HTTP request metrics.
