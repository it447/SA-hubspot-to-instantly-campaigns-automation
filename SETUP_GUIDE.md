# Automation Hub — Setup Guide

A private dashboard for your team to manage HubSpot → Instantly automations.
No code editing. No YAML. Just a form.

Total setup time: ~25 minutes.

---

## WHAT YOUR TEAM GETS

- Login page (password protected)
- Dashboard showing all active automations
- "New automation" form with live dropdowns — HubSpot lists and Instantly
  campaigns load automatically, no ID copying needed
- One-click removal of any automation
- Built-in duplicate protection — leads never get sent twice

---

## FILE STRUCTURE

Upload to GitHub exactly like this:

```
your-repo/
├── api/
│   ├── webhook.py        ← receives HubSpot events (don't touch)
│   └── automations.py    ← powers the dashboard API (don't touch)
├── index.html            ← the dashboard your team logs into
├── requirements.txt      ← Python libraries (don't touch)
└── vercel.json           ← routing config (don't touch)
```

---

## STEP 1 — Get your API keys

Instantly API key:
→ Instantly → Settings → API → copy key

HubSpot API key:
→ HubSpot → Settings → Integrations → Private Apps → Create private app
→ Name it "Automation Hub"
→ Scopes: crm.objects.contacts.read + crm.lists.read
→ Create → copy Access Token

---

## STEP 2 — Set up Upstash (free database)

1. Go to upstash.com → sign up
2. Create Database → name it "automation-hub"
3. Scroll to REST API section → copy:
   - UPSTASH_REDIS_REST_URL
   - UPSTASH_REDIS_REST_TOKEN

---

## STEP 3 — Upload to GitHub

1. github.com → + → New repository
2. Name: automation-hub → Private → Create
3. Upload all files keeping the api/ folder intact
4. Commit

---

## STEP 4 — Deploy on Vercel

1. vercel.com → sign in with GitHub → New Project → select repo → Import
2. Default settings → Deploy → wait ~1 minute

Add environment variables (Settings → Environment Variables):

| Name                     | Value                        |
|--------------------------|------------------------------|
| INSTANTLY_API_KEY        | your Instantly key           |
| HUBSPOT_API_KEY          | your HubSpot access token    |
| UPSTASH_REDIS_REST_URL   | from Upstash                 |
| UPSTASH_REDIS_REST_TOKEN | from Upstash                 |
| DASHBOARD_PASSWORD       | pick a team password         |

Save → Deployments → Redeploy

---

## STEP 5 — Connect HubSpot webhook

1. HubSpot → Settings → Private Apps → Automation Hub → Webhooks tab
2. Create subscription:
   - Event: Contact → List membership → Added to list
   - URL: https://your-project.vercel.app/webhook
3. Save

---

## USING THE DASHBOARD

Share this URL with your team: https://your-project.vercel.app

To add an automation:
1. Click "New automation"
2. Give it a name
3. Pick a HubSpot list from the dropdown
4. Pick an Instantly campaign from the dropdown
5. Click "Create automation"

That's it. No code, no IDs, no YAML.

To remove an automation: click "Remove" next to it on the dashboard.

---

## COST

- GitHub: Free
- Vercel: Free (Hobby plan)
- Upstash: Free (10,000 commands/day)
- Instantly + HubSpot: what you already pay
