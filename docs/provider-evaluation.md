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
| Retry classification | timeout/network, HTTP 408/429/5xx | timeout/network, HTTP 408/429/5xx |

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
| Bounce/complaint webhook signature and suppression | Not run | Not run |
| Mobile and Outlook rendering reviewed | Not run | Not run |
| Provider limits, quotas, and observed latency recorded | Not run | Not run |
| Decision / operator / evidence reference | Pending | Pending |

Do not send to non-test recipients until every domain/authentication and safety check is
complete. Provider webhooks require their own provider-specific, verified signature
adapter; the generic MailFlow webhook contract must not be used as a substitute.
