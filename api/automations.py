import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import requests

UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
INSTANTLY_API_KEY  = os.environ.get("INSTANTLY_API_KEY", "")
HUBSPOT_API_KEY    = os.environ.get("HUBSPOT_API_KEY", "")

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

def save_automations(automations):
    _redis_set("automations_config", automations)

def get_hs_lists():
    url = "https://api.hubapi.com/contacts/v1/lists/static?count=100"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    lists = resp.json().get("lists", [])
    return [{"id": str(l["listId"]), "name": l["name"]} for l in lists]

def get_instantly_campaigns():
    url = f"https://api.instantly.ai/api/v1/campaign/list?api_key={INSTANTLY_API_KEY}&limit=100&skip=0"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    campaigns = resp.json()
    if isinstance(campaigns, list):
        return [{"id": c.get("id", ""), "name": c.get("name", "")} for c in campaigns]
    return []

class handler(BaseHTTPRequestHandler):

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _check_auth(self):
        body = self._read_body()
        return body, body.get("password") == DASHBOARD_PASSWORD

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        token = self.headers.get("X-Auth-Token", "")
        if token != DASHBOARD_PASSWORD:
            self._json(401, {"error": "Unauthorized"})
            return

        if path.endswith("/lists"):
            try:
                self._json(200, get_hs_lists())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path.endswith("/campaigns"):
            try:
                self._json(200, get_instantly_campaigns())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path.endswith("/automations"):
            try:
                self._json(200, get_automations())
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path.endswith("/login"):
            if body.get("password") == DASHBOARD_PASSWORD:
                self._json(200, {"ok": True, "token": DASHBOARD_PASSWORD})
            else:
                self._json(401, {"error": "Wrong password"})
            return

        token = self.headers.get("X-Auth-Token", "")
        if token != DASHBOARD_PASSWORD:
            self._json(401, {"error": "Unauthorized"})
            return

        if path.endswith("/automations"):
            name       = body.get("name", "").strip()
            list_id    = str(body.get("hubspot_list_id", "")).strip()
            list_name  = body.get("hubspot_list_name", "").strip()
            camp_id    = str(body.get("instantly_campaign_id", "")).strip()
            camp_name  = body.get("instantly_campaign_name", "").strip()

            if not all([name, list_id, camp_id]):
                self._json(400, {"error": "Missing fields"})
                return

            existing = get_automations()
            for a in existing:
                if a["hubspot_list_id"] == list_id and a["instantly_campaign_id"] == camp_id:
                    self._json(409, {"error": "Automation already exists"})
                    return

            new_auto = {
                "id": f"{list_id}_{camp_id}",
                "name": name,
                "hubspot_list_id": list_id,
                "hubspot_list_name": list_name,
                "instantly_campaign_id": camp_id,
                "instantly_campaign_name": camp_name,
                "active": True,
            }
            existing.append(new_auto)
            save_automations(existing)
            self._json(200, new_auto)
        else:
            self._json(404, {"error": "Not found"})

    def do_DELETE(self):
        token = self.headers.get("X-Auth-Token", "")
        if token != DASHBOARD_PASSWORD:
            self._json(401, {"error": "Unauthorized"})
            return

        parts = self.path.strip("/").split("/")
        auto_id = parts[-1] if parts else ""
        existing = get_automations()
        updated = [a for a in existing if a.get("id") != auto_id]
        if len(updated) == len(existing):
            self._json(404, {"error": "Not found"})
            return
        save_automations(updated)
        self._json(200, {"ok": True})

    def log_message(self, *args):
        pass
