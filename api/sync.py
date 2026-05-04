import json
import os
import sys
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request

UPSTASH_URL       = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN     = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
INSTANTLY_API_KEY = os.environ.get("INSTANTLY_API_KEY", "")
HUBSPOT_API_KEY   = os.environ.get("HUBSPOT_API_KEY", "")
SYNC_SECRET       = os.environ.get("SYNC_SECRET", "")

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

def _redis_get(key):
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    val = data.get("result")
    return json.loads(val) if val else None

def _redis_set(key, value):
    url = f"{UPSTASH_URL}/set/{key}"
    body = json.dumps(value).encode()
    req = Request(url, data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        return json.loads(r.read())

def get_automations():
    data = _redis_get("automations_config")
    return data if isinstance(data, list) else []

def already_sent(email, campaign_id):
    key = f"sent:{email.lower()}:{campaign_id}"
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    return result.get("result") is not None

def mark_as_sent(email, campaign_id):
    key = f"sent:{email.lower()}:{campaign_id}"
    url = f"{UPSTASH_URL}/set/{key}/1"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        r.read()

def get_list_contacts(list_id):
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    contacts = []
    vid_offset = None

    while True:
        url = f"https://api.hubapi.com/contacts/v1/lists/{list_id}/contacts/all?count=100&property=email&property=firstname&property=lastname&property=company"
        if vid_offset:
            url += f"&vidOffset={vid_offset}"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            _log(f"[sync] HubSpot list {list_id} fetch error: {e}")
            break

        for c in body.get("contacts", []):
            props = c.get("properties", {})
            email = props.get("email", {}).get("value", "").strip().lower()
            if email:
                contacts.append({
                    "email": email,
                    "firstname": props.get("firstname", {}).get("value", ""),
                    "lastname":  props.get("lastname",  {}).get("value", ""),
                    "company":   props.get("company",   {}).get("value", ""),
                })

        if not body.get("has-more", False):
            break
        vid_offset = body.get("vid-offset")

    _log(f"[sync] list {list_id} has {len(contacts)} contacts")
    return contacts

def add_to_instantly(email, first_name, last_name, company, campaign_id):
    headers = {
        "Authorization": f"Bearer {INSTANTLY_API_KEY}",
        "Content-Type": "application/json"
    }
    resp = requests.post("https://api.instantly.ai/api/v2/leads", headers=headers, json={
        "campaign_id": campaign_id,
        "leads": [{"email": email, "first_name": first_name, "last_name": last_name, "company_name": company}],
    }, timeout=10)
    _log(f"[sync] Instantly add {email} status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        secret = self.headers.get("X-Sync-Secret", "")
        if SYNC_SECRET and secret != SYNC_SECRET:
            self._json(401, {"error": "Unauthorized"})
            return
        self._run_sync()

    def _run_sync(self):
        automations = get_automations()
        active = [a for a in automations if a.get("active")]
        _log(f"[sync] running for {len(active)} active automations")

        total_processed = total_duplicates = total_errors = 0

        for automation in active:
            list_id     = automation["hubspot_list_id"]
            campaign_id = automation["instantly_campaign_id"]
            _log(f"[sync] automation list={list_id} campaign={campaign_id}")

            contacts = get_list_contacts(list_id)

            for c in contacts:
                email = c["email"]
                try:
                    if already_sent(email, campaign_id):
                        total_duplicates += 1
                        continue
                    add_to_instantly(email, c["firstname"], c["lastname"], c["company"], campaign_id)
                    mark_as_sent(email, campaign_id)
                    _log(f"[sync] added {email} -> campaign {campaign_id}")
                    total_processed += 1
                except Exception as e:
                    _log(f"[sync] error for {email}: {e}")
                    total_errors += 1

        result = {"processed": total_processed, "duplicates": total_duplicates, "errors": total_errors}
        _log(f"[sync] done: {result}")
        self._json(200, result)

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
