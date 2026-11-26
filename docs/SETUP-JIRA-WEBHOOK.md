# Jira webhook setup

Configure one Jira webhook that sends Story creation events to the API.

| Item | Value |
|---|---|
| Jira site | `JIRA_BASE_URL` |
| Project keys | `JIRA_PROJECT_KEYS` (comma-separated; each project still needs a repository mapping) |
| Story issue type | `Story` (`JIRA_STORY_ISSUE_TYPES`) |
| Event | `jira:issue_created` (`JIRA_WEBHOOK_EVENT`) |
| Webhook name and JQL | Not visible to this project; check in Jira (Settings, System, WebHooks). Recommended JQL: `project in (<your keys>) AND issuetype = Story` |
| Callback URL | `https://<public-host>/webhooks/jira` (placeholder until deployment; locally behind a tunnel such as ngrok to `127.0.0.1:8000`) |
| HMAC secret | `JIRA_WEBHOOK_SECRET` (the same secret value as before; configure the same value in the Jira webhook "Secret" field) |

## Request contract

- **Method and body:** `POST`, `Content-Type: application/json`, body at most `JIRA_WEBHOOK_MAX_BODY_BYTES`.
- **Signature:** header `X-Hub-Signature: sha256=<hex HMAC of the raw body>`, verified in constant time over the exact bytes received.
- **Delivery id:** header `X-Atlassian-Webhook-Identifier` is required and used for dedupe. `X-Atlassian-Webhook-Retry` and `X-Atlassian-Webhook-Flow` are recorded.
- **Payload:** the envelope `webhookEvent` plus `issue.id`, `issue.key`, `issue.fields.project.key` and `issue.fields.issuetype.name`. The payload itself is never stored, only its SHA-256.

## Responses and duplicate behaviour

| Case | Result |
|---|---|
| Accepted | 200 `{"status":"accepted"}`. One receipt, one request and one intake job, in one transaction. |
| Same delivery id again | 200 `duplicate`. Nothing new is written. |
| New delivery id for the same Story | 200 `duplicate`. Recorded as `DUPLICATE_ISSUE`; no second request or job. |
| Out of scope (project, type or event) | 200 `ignored`, with the reason recorded |
| Bad or missing signature | 401 with a generic body |
| Malformed request | 400 |
| Wrong content type | 415 |
| Body too large | 413 |
| Webhook not configured | 503 |
| Database error | 500 with no detail |

## Verification procedure (local)

1. `docker compose up -d --wait api`.
2. Expose `127.0.0.1:8000` with a tunnel and set the Jira webhook URL to `https://<tunnel>/webhooks/jira`.
3. Create a Story in `KAN`.
4. Expect one row in `jira_deliveries` and `code_change_requests`, and a `code_change.intake` job in `jobs`.

**Not yet verified with real Jira:** no real Jira delivery has reached either project. Webhook intake has been verified with unit tests, Postgres integration tests and a local HTTP check (an unsigned request is rejected with 401).

## Reporting (optional)

`JIRA_REPORTING_ENABLED=true` adds one outcome comment through `POST /rest/api/3/issue/{key}/comment`. The API token's account needs the **Add Comments** permission in `KAN`. This has not been verified, and the setting stays false until you verify it.
