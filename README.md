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
and Celery Beat checks for due occurrences every minute. Promotional campaigns are
rejected until one-click unsubscribe and provider-suppression webhook support is
implemented; consent records alone do not enable promotional delivery.

Active campaigns can be paused, resumed, or cancelled with
`POST /api/v1/campaigns/{campaign_id}/pause`, `/resume`, and `/cancel`. Cancelling
marks undispatched schedules as complete and cancels queued/retrying messages. Paused
campaigns retain their pending schedules and messages.

The worker enforces Redis-backed per-provider and per-destination-domain rate limits
per second and per UTC day. Temporary SMTP failures use persisted exponential backoff
(two seconds initially, capped at one hour) for up to five total attempts; permanent
failures and exhausted retries are stored in the `dead_letters` table. A periodic
recovery task requeues messages left in `processing` after a worker crash. Optional
warm-up limits are controlled by `WARMUP_ENABLED`, `WARMUP_DAILY_LIMIT`, and
`WARMUP_DOMAIN_DAILY_LIMIT`. Daily bounce and complaint thresholds pause active
promotional campaigns and emit a critical operator log; provider feedback events will
be connected through the webhook phase.

## Run tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

For a local database, set `DATABASE_URL` to a PostgreSQL connection URL and run
`alembic upgrade head`.

## Current endpoints

- `GET /health` — application liveness.
- `GET /metrics` — Prometheus-format process and HTTP request metrics.
