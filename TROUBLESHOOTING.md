# Scale Army Automation Hub — Troubleshooting Knowledge Base

This document is a knowledge base for the internal troubleshooting agent. It covers every component of the system, common failure modes, how to diagnose them, and how to fix them — without deleting any data or automations.

---

## System Architecture Overview

The tool is a Vercel-hosted web app backed by Upstash Redis. It connects HubSpot contacts to outbound tools (Instantly, Clay, Facebook CAPI, Google Calendar, Google Sheets, Google Forms, Slack, Calendly).

**Key components:**
| Component | Purpose |
|-----------|---------|
| Vercel (Pro) | Hosts the dashboard UI and all API/sync functions |
| Upstash Redis | Stores all automation configs, dedup keys, activity logs, OAuth tokens |
| cron-job.org | External scheduler that triggers sync endpoints on a schedule |
| HubSpot | Source of contacts via lists or forms |
| Instantly | Outbound email campaign tool |
| Slack | Notification delivery |
| Google Calendar | Calendar invite sender |
| Google Sheets | CRM data sync source |
| Google Forms | Form response → Slack notifications |
| Facebook CAPI | Ad conversion reporting |
| Clay | Lead enrichment |
| Calendly | Booking webhook receiver |

**Live URL:** `https://sa-hubspot-to-instantly-campaigns-a.vercel.app`

---

## Automation Types and How They Work

### 1. HubSpot List → Instantly Campaign
- **Trigger:** Contact added to a HubSpot list
- **Path:** HubSpot webhook (`/webhook`) fires immediately OR cron (`/api/sync`) picks up every N minutes
- **Dedup key:** `sent:{email}:{campaign_id}` in Redis
- **Slack notification:** Only fires via the cron path, NOT via the webhook path

### 2. HubSpot Form → Instantly Campaign
- **Trigger:** Contact submits a HubSpot form
- **Path:** Cron (`/api/sync`) only
- **Dedup key:** `sent:{email}:{campaign_id}` in Redis

### 3. HubSpot → Clay Enrichment
- **Trigger:** Contact on a HubSpot list
- **Path:** Cron (`/api/sync_clay`)
- **Dedup key:** `sent:{email}:{auto_id}` in Redis

### 4. HubSpot → Facebook Conversions API
- **Trigger:** Contact on a HubSpot list (must have fbclid stored)
- **Path:** Cron (`/api/sync_fb`)
- **Key field:** `fbclid` extracted from the contact's first landing page URL
- **Dedup key:** `fb:{auto_id}:{email}` in Redis

### 5. HubSpot List → Google Calendar Invite
- **Trigger:** Contact on a HubSpot list
- **Path:** Cron (`/api/sync_gcal`)
- **Auth:** OAuth token stored in Redis as `gcal_token:{email}`
- **Dedup key:** `gcal:{auto_id}:{email}` in Redis

### 6. Google Sheet → HubSpot
- **Trigger:** Cron (`/api/sync_gsheet`)
- **Direction:** Sheet rows → HubSpot contacts/deals
- **Dedup key:** `sent:{pk_value}:{auto_id}` in Redis

### 7. Google Form → Slack
- **Trigger:** Cron (`/api/sync_gform`) every 5 minutes
- **Direction:** New form responses → Slack message
- **State key:** `gform_state:{auto_id}` in Redis (tracks last response timestamp)
- **First run behaviour:** Sets baseline timestamp only, sends nothing (prevents old submissions being sent)

### 8. Calendly → HubSpot
- **Trigger:** Calendly webhook (`/webhook/calendly`) fires on new booking
- **Action:** Creates/updates HubSpot contact
- **Only fires when:** At least one active Calendly automation exists

### 9. Instantly Reply/Event → HubSpot
- **Trigger:** Instantly webhook (`/webhook/instantly`) fires on reply, bounce, unsubscribe etc.
- **Action:** Updates a HubSpot contact property

---

## Sync Endpoints

| Endpoint | What it runs |
|----------|-------------|
| `/api/sync` | All HubSpot → Instantly, Clay, FB, enrichment automations |
| `/api/sync_gsheet` | All Google Sheet → HubSpot automations |
| `/api/sync_gcal` | All Google Calendar automations |
| `/api/sync_clay` | Clay-specific enrichment push |
| `/api/sync_fb` | Facebook Conversions automations |
| `/api/sync_gform` | Google Forms → Slack automations |

**All sync endpoints respond with `{"ok": true, "status": "sync started"}` immediately, then do the work. This prevents cron-job.org from timing out.**

---

## cron-job.org Configuration

The external scheduler at cron-job.org triggers the sync endpoints. All jobs should be **enabled** (green icon). If a job shows **Inactive** or **orange**, it has been auto-disabled after failures.

**How to re-enable a disabled cron job:**
1. Log in to cron-job.org
2. Click Edit on the disabled job
3. Enable it and save
4. Check History to see what HTTP error caused the failure

**Common failure codes:**
| HTTP Code | Meaning |
|-----------|---------|
| 200 | Success |
| 401 | Unauthorized — SYNC_SECRET mismatch or not set |
| 402 | Payment Required — Vercel billing issue (usually during account transfer) |
| 404 | Wrong URL — project URL changed |
| 500 | Server error — check Vercel logs |
| Timeout | Sync took longer than cron-job.org's 30s limit (fixed by early-200 pattern) |

**cron-job.org auto-disables jobs after consecutive failures.** After fixing the underlying issue, always manually re-enable the job.

---

## Common Issues and How to Diagnose Them

---

### ISSUE: Leads not getting emails / not being enrolled in Instantly

**Step 1 — Check cron-job.org**
- Are the jobs enabled (green)?
- What does the History show? Look for the HTTP status code.
- If Inactive or Failed: re-enable the job and check what error code appeared

**Step 2 — Manually trigger the sync**
Open browser on the dashboard and run in console:
```js
fetch('/api/sync').then(r=>r.json()).then(console.log)
```
Then check Vercel logs for `[sync]` lines.

**Step 3 — Check Vercel logs**
Go to Vercel dashboard → Logs. Look for:
- `[sync] running for N active automations` — confirms automations loaded
- `[sync] done: processed=X duplicates=Y` — processed=0 with duplicates=N means contacts are already marked sent
- `[sync] error for email@example.com` — individual contact errors

**Step 4 — Check if contacts are already marked as sent**
If `duplicates=N` and `processed=0`, the contacts were already enrolled (possibly via the HubSpot webhook path) and won't be re-enrolled. This is expected behaviour — dedup prevents double-sending.

**Step 5 — Check the automation is active**
Go to the dashboard → find the automation → confirm the toggle is ON (active).

**Step 6 — Check HubSpot list has contacts**
Confirm the HubSpot list the automation is pointing to actually has contacts in it.

---

### ISSUE: Slack notifications not firing

**Most common causes in order of likelihood:**

1. **Contact was enrolled via HubSpot webhook, not cron** — the webhook path (`/webhook`) does NOT send Slack notifications. Only the cron path (`/api/sync`) does. If a contact was marked sent by the webhook, the cron skips them, so the Slack notification is permanently missed for that contact.

2. **`slack_enabled` not turned on** — check the automation settings on the dashboard. The Slack notification toggle must be ON.

3. **`slack_channel` not set** — the Slack channel must be selected on the automation.

4. **`slack_message` template empty** — the message template must be filled in.

5. **SLACK_BOT_TOKEN not set** — check Vercel environment variables for `SLACK_BOT_TOKEN`.

6. **Cron not running** — see "Leads not getting emails" above.

**To test Slack manually:**
Trigger the sync manually from the browser console and watch Vercel logs for `[slack]` lines. Any Slack error will appear there.

---

### ISSUE: Google Calendar automation not running / last run was days ago

**Most likely cause: OAuth token is corrupted or expired**

**Step 1 — Trigger the sync manually**
```js
fetch('/api/sync_gcal').then(r=>r.json()).then(console.log)
```

**Step 2 — Check Vercel logs for `[gcal_sync]`**
Look for:
- `token error: No OAuth token found` → Google account needs to be re-connected
- `token error: 'list' object has no attribute 'get'` → Token is corrupted, re-connect the account
- `token error: Token refresh failed` → Refresh token expired or revoked, re-connect
- `Calendar API error 401` → Token invalid, re-connect
- `sent=0 skipped=N errors=0` → All contacts already have invites (normal if automation ran before)

**Step 3 — Re-connect the Google account**
1. Go to the dashboard → open the Google Calendar automation
2. Find the Send From email field
3. Click Connect Google Account for that email
4. Complete the OAuth flow
5. Trigger the sync again to confirm it works

**Step 4 — Check cron-job.org**
The Google Calendar cron job may have been disabled. Re-enable it if so.

---

### ISSUE: Google OAuth — connected email not appearing in dropdown

**Cause:** The OAuth flow completed but the email wasn't stored correctly in Redis.

**Step 1 — Re-connect the account**
Go to the automation → click Connect Google Account → complete the flow again.

**Step 2 — Check the debug endpoint**
```
https://sa-hubspot-to-instantly-campaigns-a.vercel.app/api/google/debug
```
This shows all connected emails in Redis. If the email is missing, the OAuth storage failed.

**Step 3 — Verify scopes**
The OAuth flow must include `userinfo.email` scope to store the email correctly. If the popup closes but the email doesn't appear, try disconnecting and reconnecting.

---

### ISSUE: Dashboard takes very long to load (45-60 seconds)

**Cause:** The `/api/activity` endpoint was fetching data sequentially. This was fixed with parallel fetching using ThreadPoolExecutor.

If slow loading returns:
- Check Vercel logs for `[activity]` errors
- Check Redis is responding (Upstash console → Monitor)
- Check if there are many automations with large log lists

---

### ISSUE: Calendly automation firing when it's turned off

**Cause:** The Calendly webhook fires regardless of automation status. The code checks for active automations before creating a HubSpot contact.

**Verify the automation is inactive:**
- Go to dashboard → find the Calendly automation → confirm toggle is OFF
- If it's OFF and HubSpot contacts are still being created, check Vercel logs for `[calendly]` — it should show `no active automations — skipping HubSpot upsert`

**Note on Calendly retries:** Calendly retries failed webhooks for up to 7 days. If the automation was active when the booking happened, you may still see HubSpot contacts being created from old retried webhook calls even after turning the automation off.

---

### ISSUE: Google Forms responses not being sent to Slack

**Step 1 — Check the cron**
Is the `sync_gform` cron job running on cron-job.org?

**Step 2 — Check the service account has access**
The Google service account email must be added as an **Editor** on the Google Form. Go to the form → Share → add the service account email.

**Step 3 — First run behaviour**
On the very first run, the automation sets a baseline timestamp and sends nothing. This is intentional — it prevents all historical submissions from being sent. Only submissions after the automation was created will be sent.

**Step 4 — Trigger manually**
```js
fetch('/api/sync_gform').then(r=>r.json()).then(console.log)
```
Check Vercel logs for `[gform]` lines.

---

### ISSUE: HubSpot → Facebook Conversions not working

**Step 1 — Verify contacts have fbclid**
The automation only sends contacts who have a Facebook Click ID stored in HubSpot. Check the HubSpot list — contacts must have `hs_analytics_first_url` or a similar field containing `fbclid=`.

**Step 2 — Check Pixel ID and Access Token**
Go to the automation settings and verify the Pixel ID and Facebook Access Token are correct and not expired.

**Step 3 — Trigger manually**
```js
fetch('/api/sync_fb').then(r=>r.json()).then(console.log)
```
Check Vercel logs for `[fb_sync]` lines.

---

### ISSUE: Google Sheet sync not updating HubSpot

**Step 1 — Check the service account**
The Google service account must have at least Viewer access to the Google Sheet.

**Step 2 — Check column mappings**
Go to the automation settings — each column must be mapped to a HubSpot property. If the column name in the sheet changed, the mapping breaks silently.

**Step 3 — Check primary key**
The primary key column (email, domain, etc.) must exist in every row. Rows without a primary key are skipped.

**Step 4 — Check dedup**
Contacts already processed are skipped. If a contact was already synced and nothing is happening, it means they're in the sent cache. They'll only be re-synced if new columns are added to the mapping.

---

### ISSUE: Cron jobs were working then stopped after Vercel project transfer

**Cause:** Vercel returned 402 Payment Required during the transfer period. cron-job.org auto-disabled jobs after repeated failures.

**Fix:**
1. Confirm Vercel Pro account is active and billing is set up
2. Go to cron-job.org → re-enable all disabled jobs
3. Check History — once you see 200 responses, they're working again

---

## Environment Variables (Vercel)

All secrets are stored as Vercel environment variables. If an integration stops working, check the relevant variable is set:

| Variable | Used by |
|----------|---------|
| `HUBSPOT_API_KEY` | All HubSpot API calls |
| `INSTANTLY_API_KEY` | Instantly campaign enrollment |
| `SLACK_BOT_TOKEN` | All Slack notifications |
| `UPSTASH_REDIS_REST_URL` | Redis storage |
| `UPSTASH_REDIS_REST_TOKEN` | Redis authentication |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Sheets, Google Forms |
| `GOOGLE_OAUTH_CLIENT_ID` | Google Calendar OAuth |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google Calendar OAuth |
| `CALENDLY_API_KEY` | Calendly webhook registration |
| `CALENDLY_WEBHOOK_SECRET` | Calendly webhook signature verification |
| `CLAY_API_KEY` | Clay enrichment |
| `DASHBOARD_PASSWORD` | Dashboard login |
| `SYNC_SECRET` | Sync endpoint authentication (optional) |

---

## Redis Key Structure

Understanding what's stored in Redis helps diagnose issues:

| Key Pattern | What it stores |
|-------------|---------------|
| `automations_config` | All automation configurations (the master list) |
| `sent:{email}:{target_id}` | Dedup marker — contact already enrolled |
| `gcal:{auto_id}:{email}` | Dedup marker — calendar invite already sent |
| `fb:{auto_id}:{email}` | Dedup marker — FB conversion already sent |
| `gcal_token:{email}` | Google OAuth token for Calendar |
| `gcal_connected_emails` | SET of all connected Google account emails |
| `logs:{auto_id}` | Activity log list for an automation (max 10,000 entries) |
| `gform_state:{auto_id}` | Last response timestamp for Google Forms polling |
| `day_totals:{YYYY-MM-DD}` | Daily enrollment count for dashboard stats |

**Important:** Never manually delete `automations_config` — this will wipe all automation settings. Never delete `sent:*`, `gcal:*`, or `fb:*` dedup keys — this will cause contacts to be re-enrolled/re-sent.

---

## How to Check Vercel Logs

1. Go to vercel.com → your project
2. Click **Logs** in the top nav
3. Filter by function name or search for `[sync]`, `[gcal_sync]`, `[gform]`, `[calendly]`, `[webhook]` etc.
4. Look for `[error]` level entries

**Log prefixes by component:**
| Prefix | Component |
|--------|-----------|
| `[sync]` | Main sync (HubSpot → Instantly) |
| `[gcal_sync]` | Google Calendar sync |
| `[fb_sync]` | Facebook Conversions sync |
| `[clay_sync]` | Clay enrichment sync |
| `[sync_gsheet]` | Google Sheets sync |
| `[gform]` | Google Forms sync |
| `[calendly]` | Calendly webhook |
| `[webhook]` | HubSpot webhook |
| `[instantly_webhook]` | Instantly webhook |
| `[slack]` | Slack notifications |
| `[oauth_callback]` | Google OAuth flow |

---

## How to Manually Trigger Any Sync

Open the dashboard in a browser, press F12 to open DevTools, go to the Console tab, and paste:

```js
// Main sync
fetch('/api/sync').then(r=>r.json()).then(console.log)

// Google Calendar
fetch('/api/sync_gcal').then(r=>r.json()).then(console.log)

// Google Sheets
fetch('/api/sync_gsheet').then(r=>r.json()).then(console.log)

// Facebook Conversions
fetch('/api/sync_fb').then(r=>r.json()).then(console.log)

// Clay
fetch('/api/sync_clay').then(r=>r.json()).then(console.log)

// Google Forms
fetch('/api/sync_gform').then(r=>r.json()).then(console.log)
```

All return `{"ok": true, "status": "sync started"}` immediately. Check Vercel logs for actual results.

---

## Golden Rules — What Never to Do

1. **Never delete `automations_config` from Redis** — wipes all automations
2. **Never bulk-delete `sent:*` keys** — causes every contact to be re-enrolled
3. **Never delete `gcal_token:*` keys** — disconnects Google Calendar accounts
4. **Never force-push to main branch** — use PRs
5. **Never add more than 12 Python files to `/api/`** — Vercel Hobby plan limit (now on Pro this limit is lifted)
6. **Never disable a cron job without first understanding why it failed** — check History first

---

## Upstash Redis Cost Monitoring

Current usage: ~170k commands/month at $0.34.

**What drives commands:**
- Dashboard loads → reads automations + activity logs
- Each cron run → reads automations config + writes dedup keys + writes logs
- Google Forms polling (every 5 min) → ~288 runs/day × ~4 commands = ~1,200/day

**To check usage:** Upstash console → your database → Usage tab → shows command breakdown by type.
