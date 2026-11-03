# Scale Army Automation Hub

An internal automation tool that syncs contacts from HubSpot lists into Instantly.ai campaigns, HubSpot forms, Clay tables, and Google Sheets — automatically, on a cron schedule.

Built on Vercel (serverless Python) with Upstash Redis for state management.

---

## What It Does

- **HubSpot → Instantly** — Automatically enrolls or unenrolls contacts from HubSpot lists into Instantly email campaigns
- **HubSpot → HubSpot Forms** — Submits contacts from a list into a HubSpot form to trigger workflows
- **HubSpot → Clay** — Pushes contacts to Clay tables via webhook for enrichment
- **Google Sheet → HubSpot** — Pulls enriched data from a Google Sheet and upserts it back into HubSpot contacts/companies/deals
- **Webhooks** — Receives inbound events from Calendly, Instantly, Facebook, and Google Forms

All automations are managed through a password-protected browser dashboard. No contacts are ever processed twice — a permanent dedup layer in Redis ensures each contact is only sent to each destination once.

---

## Stack

| Layer | Technology |
|---|---|
| Frontend | Single-page app (`index.html`) — vanilla JS, no framework |
| Backend | Python serverless functions on Vercel |
| State / Storage | Upstash Redis (REST API) |
| Email campaigns | Instantly.ai API v2 |
| CRM | HubSpot API (v1 + v3) |
| Notifications | Slack API |
| Enrichment | Clay webhooks, Google Sheets API v4 |
| Auth | Google OAuth 2.0 (service account for Sheets) |

---

## Project Structure

```
├── index.html                  # Full frontend UI
├── vercel.json                 # URL routing
├── requirements.txt            # Python dependencies
└── api/
    ├── automations.py          # Dashboard API — auth, CRUD, data fetching
    ├── sync.py                 # Core sync engine (triggered by cron)
    ├── sync_gsheet.py          # Google Sheet → HubSpot sync
    ├── sync_clay.py            # Clay push handler
    ├── sync_fb.py              # Facebook lead form sync
    ├── sync_gcal.py            # Google Calendar sync
    ├── sync_gform.py           # Google Forms sync
    ├── webhook.py              # Inbound webhook receiver + /status
    ├── calendly_webhook.py     # Calendly event handler
    ├── instantly_webhook.py    # Instantly reply/bounce handler
    ├── google_oauth.py         # Google OAuth flow
    └── gsheet.py              # Google Sheets helpers
```

---

## Environment Variables

Set these in the Vercel dashboard under Project Settings → Environment Variables:

| Variable | Required | Description |
|---|---|---|
| `DASHBOARD_PASSWORD` | ✅ | Login password for the UI |
| `UPSTASH_REDIS_REST_URL` | ✅ | Upstash Redis base URL |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Upstash Redis auth token |
| `INSTANTLY_API_KEY` | ✅ | Instantly v2 API key |
| `HUBSPOT_API_KEY` | ✅ | HubSpot private app token |
| `HUBSPOT_PORTAL_ID` | ✅ | HubSpot portal/account ID |
| `SYNC_SECRET` | ✅ | Secret header required to trigger `/api/sync` |
| `SLACK_BOT_TOKEN` | Optional | Slack bot token for notifications |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Optional | Full JSON of Google service account credentials |
| `CLAY_API_KEY` | Optional | Clay API key |
| `CALENDLY_API_KEY` | Optional | Calendly personal access token |

---

## Triggering a Sync

The sync runs automatically via a cron job (cron-job.org or similar) hitting:

```
GET /api/sync
X-Sync-Secret: YOUR_SYNC_SECRET
```

To trigger manually:

```bash
curl -X GET https://your-vercel-url.vercel.app/api/sync \
  -H "X-Sync-Secret: YOUR_SYNC_SECRET"
```

The endpoint returns `{"ok": true}` immediately and runs the sync in the background.

---

## Diagnostic Endpoints

These are useful for debugging when leads aren't appearing in Instantly:

| Endpoint | What it does |
|---|---|
| `/api/instantly/debug?campaign_id=UUID` | 6-step diagnostic: API key, campaign status, automation config, dedup key count, HubSpot list count, live test add |
| `/api/instantly/reset-dedup?campaign_id=UUID&confirm=yes` | Clears all dedup keys for a campaign so contacts can be re-processed |
| `/api/google/debug` | Tests Google Sheets connectivity |
| `/api/google/service-account-email` | Shows the service account email to share Google Sheets with |
| `/status` | System health check |

---

## Safety Guarantees

- **No mass deletion** — no endpoint or button deletes all automations at once
- **No mass dedup reset** — the reset endpoint requires a specific `campaign_id` and `confirm=yes`
- **Sync-safe saves** — the sync re-fetches automation config before saving to avoid overwriting UI changes made during a run
- **Dedup is permanent** — once a contact is processed for a target, they will never be re-processed unless the dedup key is explicitly cleared
- **Empty-list protection** — if Redis returns an empty list during a sync, the save is skipped entirely rather than overwriting with an empty array

---

## Full Documentation

See [`TOOL_SOP.md`](./TOOL_SOP.md) for the complete SOP — architecture deep dive, Redis schema, sync engine flow, all API endpoints, and troubleshooting reference.
