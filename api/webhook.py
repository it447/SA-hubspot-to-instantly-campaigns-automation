import json
import os
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request

UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
INSTANTLY_API_KEY = os.environ.get("INSTANTLY_API_KEY", "")
HUBSPOT_API_KEY   = os.environ.get("HUBSPOT_API_KEY", "")

def _redis_get(key):
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {os.environ.get('UPSTASH_REDIS_REST_TOKEN','')}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    val = data.get("result")
    return json.loads(val) if val else None

def get_automations():
    data = _redis_get("automations_config")
    return data if isinstance(data, list) else []

def already_sent(email, campaign_id):
    key = f"sent:{email.lower()}:{campaign_id}"
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {os.environ.get('UPSTASH_REDIS_REST_TOKEN','')}"})
    with urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    return result.get("result") is not None

def mark_as_sent(email, campaign_id):
    key = f"sent:{email.lower()}:{campaign_id}"
    url = f"{UPSTASH_URL}/set/{key}/1"
    req = Request(url, headers={"Authorization": f"Bearer {os.environ.get('UPSTASH_REDIS_REST_TOKEN','')}"})
    with urlopen(req, timeout=5) as r:
        r.read()

def get_contact_details(contact_id):
    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    resp = requests.get(url, params={"properties": "email,firstname,lastname,company"}, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json().get("properties", {})

def add_to_instantly(email, first_name, last_name, company, campaign_id):
    resp = requests.post("https://api.instantly.ai/api/v1/lead/add", json={
        "api_key": INSTANTLY_API_KEY,
        "campaign_id": campaign_id,
        "skip_if_in_workspace": True,
        "leads": [{"email": email, "first_name": first_name, "last_name": last_name, "company_name": company}],
    }, timeout=10)
    resp.raise_for_status()

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        automations = get_automations()
        self._json(200, {"status": "ok", "automations": len(automations)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        events = json.loads(self.rfile.read(length)) if length else []

        automations = get_automations()
        lookup = {a["hubspot_list_id"]: a for a in automations if a.get("active")}

        processed = duplicates = skipped = 0

        for event in events:
            if event.get("subscriptionType") != "contact.listMembership":
                skipped += 1; continue

            list_id    = str(event.get("listId", ""))
            contact_id = str(event.get("objectId", ""))
            automation = lookup.get(list_id)
            if not automation:
                skipped += 1; continue

            try:
                props = get_contact_details(contact_id)
            except Exception:
                skipped += 1; continue

            email = props.get("email", "").strip().lower()
            if not email:
                skipped += 1; continue

            campaign_id = automation["instantly_campaign_id"]

            try:
                if already_sent(email, campaign_id):
                    duplicates += 1; continue
            except Exception:
                pass

            try:
                add_to_instantly(email, props.get("firstname",""), props.get("lastname",""), props.get("company",""), campaign_id)
                mark_as_sent(email, campaign_id)
                processed += 1
            except Exception:
                skipped += 1

        self._json(200, {"processed": processed, "duplicates": duplicates, "skipped": skipped})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
