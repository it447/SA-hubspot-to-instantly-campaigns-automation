# Scale Army Automation Hub — Standard Operating Procedure

**Tool:** HubSpot → Instantly Campaigns Automation  
**Hosted on:** Vercel (serverless)  
**State layer:** Upstash Redis (REST API)  
**Last updated:** August 2026

---

## 1. What This Tool Does

The Automation Hub is an internal web application that automatically syncs contacts from HubSpot lists into third-party destinations — primarily Instantly.ai email campaigns, HubSpot forms, Clay data tables, and Google Sheets. It runs on a cron schedule and is fully managed through a browser-based dashboard that requires a password to access.

At its core, it solves one problem: **you should not have to manually add contacts to an Instantly campaign or trigger a HubSpot form submission.** Once an automation is configured, it fires automatically on every cron run.

---

## 2. Architecture Overview

```
Browser (index.html)
       │
       ▼
Vercel Serverless Functions (Python)
       │
       ├── api/automations.py   ← Dashboard API (CRUD, lists, campaigns)
       ├── api/sync.py          ← Core sync engine (runs on cron)
       ├── api/sync_gsheet.py   ← Google Sheet → HubSpot sync
       ├── api/sync_clay.py     ← Clay push handler
       ├── api/sync_fb.py       ← Facebook form sync
       ├── api/sync_gcal.py     ← Google Calendar sync
       ├── api/sync_gform.py    ← Google Forms sync
       ├── api/webhook.py       ← Inbound webhook handler + /status
       ├── api/calendly_webhook.py  ← Calendly event handler
       ├── api/instantly_webhook.py ← Instantly reply/event handler
       └── api/google_oauth.py  ← Google OAuth flow
       │
       ▼
Upstash Redis (all state: automations, dedup keys, logs, counts)
       │
       ├── HubSpot API (contact lists, form submissions)
       ├── Instantly API v2 (lead enrollment/unenrollment)
       ├── Clay Webhook (data push)
       ├── Slack API (notifications and alerts)
       └── Google APIs (Sheets, Calendar, Forms)
```

---

## 3. Infrastructure & Environment

### 3.1 Vercel
- All Python files in `/api/` are deployed as individual serverless functions.
- `vercel.json` routes every URL to the correct Python handler.
- No server to manage — Vercel spins up a container per request and destroys it after.
- Vercel function timeout: 10 seconds for most routes. The sync endpoint responds immediately with `{"ok": true}` then runs the sync in the background to avoid timeout issues.

### 3.2 Upstash Redis
- Used as the **only persistent state store**. There is no database.
- Accessed exclusively via Upstash's HTTP REST API — no Redis client library needed.
- All reads and writes go through helper functions in `sync.py`: `_redis_get`, `_redis_set_json`, `_redis_set_raw`, `_redis_incr`, `_redis_expire`.

### 3.3 Environment Variables (set in Vercel dashboard)
| Variable | Purpose |
|---|---|
| `UPSTASH_REDIS_REST_URL` | Base URL for Upstash Redis REST API |
| `UPSTASH_REDIS_REST_TOKEN` | Auth token for Redis |
| `DASHBOARD_PASSWORD` | Login password for the UI |
| `INSTANTLY_API_KEY` | Instantly v2 API key |
| `HUBSPOT_API_KEY` | HubSpot private app token |
| `HUBSPOT_PORTAL_ID` | HubSpot account/portal ID (default: 22650739) |
| `SYNC_SECRET` | Secret header required to trigger `/api/sync` |
| `SLACK_BOT_TOKEN` | Slack bot token for notifications |
| `CLAY_API_KEY` | Clay API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON of Google service account credentials |
| `CALENDLY_API_KEY` | Calendly personal access token |

---

## 4. Redis Data Schema

All application state lives in Redis. Here is every key pattern used:

| Key Pattern | Type | Contents |
|---|---|---|
| `automations_config` | JSON array | The full list of all automation objects |
| `sent:{email}:{target_id}` | String `"1"` | Dedup flag — set once a contact has been processed for a given target |
| `first_seen:{email}:{target_id}` | String (Unix timestamp) | When a contact was first seen, used for delay logic |
| `logs:{auto_id}` | Redis List (LPUSH/LTRIM capped at 10,000) | Enrollment log entries per automation |
| `enroll_count:{auto_id}:{YYYY-MM-DD}` | Integer (INCR) | Daily enrollment count per automation, expires after 30 days |
| `alert_sent:{auto_id}:{YYYY-MM-DD}` | String `"1"` | Prevents duplicate daily alerts, expires after 2 days |
| `alert_sent_week:{auto_id}:{YYYY-W##}` | String `"1"` | Prevents duplicate weekly alerts, expires after 8 days |
| `gsheet_token_cache` | JSON | Cached Google OAuth access token with expiry |

### Critical: The Dedup Key
The key `sent:{email}:{target_id}` is the most important key in the system. It is permanent (no TTL) and means: **"this contact has already been processed for this target — never touch them again."**

- For Instantly enrollments: `target_id` = Instantly campaign UUID
- For HubSpot form submissions: `target_id` = HubSpot form UUID
- For Clay pushes: `target_id` = `clay:{auto_id}` (separate namespace)

**If this key exists, the contact will be skipped on every future sync run.** This is intentional — it prevents re-enrolling people who have already received outreach.

**To re-process contacts for a campaign**, use `/api/instantly/reset-dedup?campaign_id=YOUR_CAMPAIGN_ID&confirm=yes` — this scans and deletes all matching dedup keys.

---

## 5. Automation Object Schema

Each automation is stored as a JSON object inside the `automations_config` array. Here are all the fields:

```json
{
  "id":                   "uuid-v4",
  "name":                 "My Automation Name",
  "active":               true,
  "delivery_type":        "instantly",
  "action":               "enroll",
  "hubspot_list_id":      "123",
  "instantly_campaign_id": "uuid-of-campaign",
  "hubspot_form_id":      "uuid-of-form",
  "delay_hours":          0,
  "filters": [
    { "property": "company", "operator": "exists", "value": "" }
  ],
  "slack_enabled":        true,
  "slack_channel":        "C0XXXXXX",
  "slack_message":        "New lead enrolled: {{first_name}} {{last_name}} ({{email}}) from {{company}}",
  "alert_enabled":        false,
  "alert_threshold":      10,
  "alert_schedule":       "daily",
  "alert_time":           "08:00",
  "alert_day":            0,
  "alert_slack_channel":  "C0XXXXXX",
  "alert_message":        "⚠️ Only {{count}} enrollments today for {{automation_name}}",
  "clay_enabled":         false,
  "clay_webhook_url":     "https://clay.com/webhook/...",
  "clay_column_mappings": [
    { "hs_property": "email",     "clay_column": "Email" },
    { "hs_property": "company",   "clay_column": "Company" }
  ],
  "enrichment_gsheet_enabled": false,
  "sheet_url":            "https://docs.google.com/spreadsheets/d/...",
  "sheet_tab":            "Sheet1",
  "object_type":          "contact",
  "primary_key_column":   "Email",
  "primary_key_type":     "email",
  "column_mappings": [
    { "column": "LinkedIn URL", "property": "linkedin_url" }
  ],
  "gsheet_schedule_type": "interval",
  "gsheet_interval_minutes": 60,
  "gsheet_run_time":      "08:00",
  "gsheet_run_day":       0,
  "last_run":             "2026-08-27T12:00:00Z",
  "last_gsheet_run":      "2026-08-27T12:00:00Z"
}
```

### Delivery Types
| `delivery_type` | What happens |
|---|---|
| `instantly` | Contacts added to an Instantly campaign via v2 API |
| `hubspot_form` | Contacts submitted to a HubSpot form |
| `enrichment` | Contacts pushed to Clay and/or data pulled from Google Sheet into HubSpot |
| `gsheet_sync` | Handled separately by `/api/sync_gsheet` — not run by the main sync |

### Action Types (Instantly only)
| `action` | What happens |
|---|---|
| `enroll` | Adds contact as a lead in the campaign |
| `unenroll` | Looks up and deletes the lead from the campaign |

---

## 6. The Sync Engine (`api/sync.py`)

This is the core of the tool. It is triggered by a cron job via an HTTP GET to `/api/sync` with the header `X-Sync-Secret: {SYNC_SECRET}`.

### 6.1 Sync Flow (step by step)

1. **Auth check** — verifies `X-Sync-Secret` header matches env var.
2. **Immediate 200 response** — returns `{"ok": true}` right away so cron-job.org doesn't timeout.
3. **Load active automations** — reads `automations_config` from Redis, filters to only `active: true` and excludes `gsheet_sync` type.
4. **Load sent cache** — performs a single Redis SCAN of all `sent:*` keys into a Python `set` in memory. This avoids making one Redis call per contact.
5. **Loop over each automation:**
   - **Enrichment type:** Runs Clay push and/or GSheet pull, then `continue` to next automation.
   - **Instantly/form type:**
     - Fetches all contacts from HubSpot list (paginated, 100 per page).
     - For each contact:
       - Check dedup cache — skip if already processed.
       - Check delay — if `delay_hours > 0`, record first-seen timestamp and skip until enough time has passed.
       - Check filters — if contact fails any filter condition, mark as sent (skip forever) and continue.
       - Call the appropriate API (Instantly add, Instantly delete, or HubSpot form submit).
       - Increment daily enrollment counter.
       - Send Slack enrollment notification if enabled.
       - Mark as sent in Redis + local cache.
       - Log the enrollment.
     - After all contacts: check alert threshold and send Slack alert if needed.
     - Update `last_run` timestamp on the automation object.
6. **Safe save** — re-fetches `automations_config` from Redis (in case it was modified during the run), merges only `last_run` timestamps, and saves back. **Never overwrites automation configs written by the UI during a sync run.**

### 6.2 HubSpot Contact Fetching

Uses HubSpot Contacts v1 API:
```
GET /contacts/v1/lists/{list_id}/contacts/all?count=100&property=email&property=firstname&property=lastname&property=company&property=company_domain
```

Paginates using `vidOffset` until `has-more: false`. Returns all contacts with email, first name, last name, company, company_domain, plus any additional properties needed by filters.

### 6.3 Instantly API

**Add a lead (enroll):**
```
POST https://api.instantly.ai/api/v2/leads
Authorization: Bearer {INSTANTLY_API_KEY}
Content-Type: application/json

{
  "campaign_id": "uuid",
  "email":       "lead@example.com",
  "first_name":  "John",
  "last_name":   "Doe",
  "company_name":"Acme Corp"
}
```
Critical: the payload is a **flat object**, not a `{"leads": [...]}` array. Using the array format returns a 400 error "Email is required when creating a lead."

**Look up a lead (for unenroll):**
```
POST https://api.instantly.ai/api/v2/leads/list
Authorization: Bearer {INSTANTLY_API_KEY}
Content-Type: application/json

{
  "campaign_id": "uuid",
  "email":       "lead@example.com",
  "limit":       1
}
```
Critical: this is a POST, not GET. The old v1 endpoint `GET /api/v1/lead/get` no longer exists.

**Delete a lead (unenroll):**
```
DELETE https://api.instantly.ai/api/v2/leads/{lead_id}
Authorization: Bearer {INSTANTLY_API_KEY}
```

### 6.4 Delay Logic

If `delay_hours > 0` is set on an automation:
1. First time a contact is seen → write `first_seen:{email}:{target_id}` = current Unix timestamp. Skip contact this run.
2. Every subsequent run → read the `first_seen` key, compare elapsed time to `delay_hours`. Skip until enough time has passed.
3. Once delay elapsed → process normally (add to Instantly, submit form, etc.).

### 6.5 Filter Logic

Filters are applied after delay check, before the API call. Each filter specifies:
- `property`: a HubSpot contact property name
- `operator`: `exists`, `equals`, `not_equals`, `contains`, `not_contains`
- `value`: the comparison value

If a contact fails any filter, it is marked as sent (so it's never re-evaluated) and skipped. This means filtered contacts will never be enrolled even if their data changes.

---

## 7. Dashboard API (`api/automations.py`)

All browser interactions go through this file. It handles authentication and every data operation for the UI.

### 7.1 Authentication
- Every request must include header: `X-Auth-Token: {DASHBOARD_PASSWORD}`
- `DASHBOARD_PASSWORD` is set as a Vercel environment variable
- The login screen in `index.html` collects the password and stores it in `localStorage` for the session

### 7.2 Key API Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/login` | Validates password, returns `{"ok": true}` or 401 |
| `GET` | `/api/automations` | Returns all automation configs from Redis |
| `POST` | `/api/automations` | Creates a new automation |
| `PUT` | `/api/automations/{id}` | Updates an existing automation |
| `DELETE` | `/api/automations/{id}` | Deletes one automation (never mass-deletes) |
| `POST` | `/api/automations/{id}/toggle` | Toggles `active` true/false |
| `GET` | `/api/lists` | Fetches all HubSpot lists (v1 + v3 API, deduped) |
| `GET` | `/api/campaigns` | Fetches all Instantly campaigns |
| `GET` | `/api/forms` | Fetches all HubSpot forms |
| `GET` | `/api/pipelines` | Fetches HubSpot deal pipelines |
| `GET` | `/api/properties` | Fetches HubSpot contact properties |
| `GET` | `/api/contacts/{list_id}` | Returns contacts in a specific HubSpot list |
| `GET` | `/api/slack/channels` | Lists all Slack channels the bot is in |
| `GET` | `/api/activity` | Returns recent enrollment logs across all automations |
| `GET` | `/api/logs/{auto_id}` | Returns logs for one specific automation |
| `GET` | `/api/instantly/debug` | 6-step diagnostic for an Instantly campaign |
| `GET` | `/api/instantly/reset-dedup` | Clears dedup keys for a campaign (requires `&confirm=yes`) |
| `GET` | `/api/google/service-account-email` | Shows the service account email for GSheet sharing |
| `GET` | `/api/google/connected-accounts` | Lists connected Google accounts |
| `GET` | `/api/google/debug` | Tests GSheet connectivity |

### 7.3 The Debug Endpoint (`/api/instantly/debug?campaign_id=...`)

Added to diagnose enrollment failures. Runs 6 checks and returns a JSON summary:
1. API key is set in environment
2. Campaign exists in Instantly and its status code (-1=paused, 0=draft, 1=active, 2=completed)
3. Automation config in Redis that references this campaign
4. Count of `sent:*:{campaign_id}` dedup keys in Redis (via SCAN)
5. HubSpot list contact count
6. Live test: actually attempts to add a dummy lead to Instantly and reports the full response

Returns `action_needed` field summarizing what is wrong.

---

## 8. Webhook Handlers

### `/webhook` → `api/webhook.py`
Generic inbound webhook. Also handles `/status` — returns system health.

### `/webhook/calendly` → `api/calendly_webhook.py`
Receives Calendly `invitee.created` and `invitee.canceled` events. Auto-registers the Calendly webhook on startup if not already registered.

### `/webhook/instantly` → `api/instantly_webhook.py`
Receives Instantly reply and status events (e.g., email replied, bounced, unsubscribed).

---

## 9. Google Sheet Integration

### 9.1 Google Sheet → HubSpot Enrichment (Enrichment automations)
Pulls data from a Google Sheet and upserts it into HubSpot contacts, companies, or deals.

Flow:
1. Get OAuth token from Google using service account credentials (cached in Redis for 55 minutes)
2. Read sheet data via Sheets API v4
3. Match rows by primary key column (email, domain, or HubSpot object ID)
4. Batch upsert into HubSpot using CRM v3 batch API (100 records per batch)

Schedule options: interval (every N minutes), daily (specific time EST), weekly (specific day + time EST).

### 9.2 Service Account Setup
1. Create a Google Cloud service account
2. Download its JSON credentials
3. Set the full JSON as `GOOGLE_SERVICE_ACCOUNT_JSON` in Vercel environment variables
4. Share the target Google Sheet with the service account email (view-only access sufficient)

### 9.3 `/api/sync_gsheet` → `api/sync_gsheet.py`
Separate sync endpoint for `gsheet_sync` delivery type. Not called by the main sync engine. Has its own cron trigger.

---

## 10. Slack Integration

### 10.1 Enrollment Notifications
When `slack_enabled: true` on an automation, every successful enrollment sends a message to `slack_channel`. The message uses a template with variables:
- `{{email}}`, `{{first_name}}`, `{{last_name}}`, `{{company}}`

### 10.2 Threshold Alerts
When `alert_enabled: true`, the system sends an alert if the enrollment count falls below `alert_threshold`:
- **Daily alerts**: checks at/after `alert_time` EST, only fires once per day
- **Weekly alerts**: checks on `alert_day` (0=Monday) at `alert_time`, only fires once per week
- Alert message variables: `{{count}}`, `{{automation_name}}`, `{{threshold}}`, `{{date}}`

### 10.3 Bot Setup
1. Create a Slack app at api.slack.com
2. Add `chat:write` and `channels:read` scopes
3. Install to workspace and copy the bot token
4. Set as `SLACK_BOT_TOKEN` in Vercel environment
5. Invite the bot to each channel it needs to post in

---

## 11. Creating a New Automation (Step-by-Step)

1. Log in to the dashboard with the password.
2. Click **+ New Automation** on the Automations page.
3. Fill in:
   - **Name** — descriptive internal label
   - **HubSpot List** — select the list containing your target contacts
   - **Delivery Type** — Instantly, HubSpot Form, or Enrichment
   - **Campaign / Form** — the destination to enroll contacts into
   - **Action** — Enroll or Unenroll (Instantly only)
4. Optionally configure:
   - **Delay (hours)** — wait N hours after first seeing a contact before processing
   - **Filters** — only process contacts matching certain property conditions
   - **Slack Notifications** — get notified per enrollment
   - **Threshold Alerts** — get alerted if daily/weekly enrollment volume drops below a number
5. Click **Save**. The automation is now stored in Redis and will run on the next cron trigger.
6. Use the toggle to activate or pause the automation without deleting it.

---

## 12. Triggering a Sync Manually

The sync runs automatically via cron-job.org. To trigger it manually:

```bash
curl -X GET https://sa-hubspot-to-instantly-campaigns-a.vercel.app/api/sync \
  -H "X-Sync-Secret: YOUR_SYNC_SECRET"
```

The endpoint returns immediately with `{"ok": true, "status": "sync started"}` — the actual sync runs after the response is sent.

---

## 13. Cron Setup

The sync is triggered by cron-job.org (or similar) hitting `/api/sync` with the secret header. Recommended schedule: every 15–30 minutes. The cron service must support custom headers.

---

## 14. Safety Guarantees

The following protections are built into the system:

1. **No mass deletion**: There is no button or endpoint that deletes all automations. Individual automations can be deleted one at a time only.
2. **No mass dedup reset**: The reset-dedup endpoint requires `campaign_id` and `confirm=yes` — it cannot clear all dedup keys at once.
3. **Sync-safe save**: After a sync run, the system re-fetches the latest automation config from Redis before saving, so changes made in the UI during a sync are never overwritten.
4. **Dedup is permanent**: The `sent:` key has no TTL. A contact processed once for a target will never be re-processed unless the key is explicitly deleted.
5. **Empty-list protection**: If a re-fetch of automations returns an empty list (suggesting a Redis error), the system logs a warning and skips the save entirely rather than overwriting with an empty array.

---

## 15. Troubleshooting Quick Reference

### Contacts not appearing in Instantly
1. Run `/api/instantly/debug?campaign_id={UUID}` — check all 6 steps.
2. Check Step 4: if dedup key count matches your list size, all contacts have already been processed. Run reset-dedup.
3. Check Step 6 (test add): if status 400, the campaign ID is wrong or the campaign is deleted in Instantly.
4. If test add returns 200 but leads still don't appear in Instantly UI, it is an Instantly platform issue (account-level problem, sending account health, plan limits) — not a code issue.

### Automation not running
- Check that `active: true` is set on the automation.
- Check that `delivery_type` is not `gsheet_sync` (those have a separate endpoint).
- Verify the cron job is firing and sending the correct `X-Sync-Secret` header.
- Check Vercel function logs for errors.

### Wrong password at login
- `DASHBOARD_PASSWORD` must be set as a Vercel environment variable, not in code.
- After changing it in Vercel, redeploy (or wait for Vercel to pick it up).
- Clear `localStorage` in the browser if the old token is cached.

### Google Sheet not syncing
- Confirm the sheet is shared with the service account email (`/api/google/service-account-email`).
- Run `/api/google/debug` to test connectivity.
- Check that `GOOGLE_SERVICE_ACCOUNT_JSON` is set correctly — it must be the full JSON, not a file path.

---

## 16. File Map

| File | Role |
|---|---|
| `index.html` | Entire frontend — single-page app, vanilla JS, no framework |
| `api/automations.py` | Dashboard API: auth, CRUD, HubSpot/Instantly/Slack data fetching |
| `api/sync.py` | Core sync engine: HubSpot → Instantly/form/Clay, dedup, alerts |
| `api/sync_gsheet.py` | Dedicated GSheet sync handler |
| `api/sync_clay.py` | Clay-specific sync handler |
| `api/sync_fb.py` | Facebook lead form sync |
| `api/sync_gcal.py` | Google Calendar sync |
| `api/sync_gform.py` | Google Forms sync |
| `api/webhook.py` | Inbound webhook receiver + /status health check |
| `api/calendly_webhook.py` | Calendly event handler |
| `api/instantly_webhook.py` | Instantly reply/bounce/status handler |
| `api/google_oauth.py` | Google OAuth flow for connected accounts |
| `api/gsheet.py` | Google Sheets helper functions |
| `vercel.json` | URL routing — maps every path to the correct Python file |
| `requirements.txt` | Python dependencies for Vercel |
