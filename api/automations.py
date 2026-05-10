import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import requests

UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
INSTANTLY_API_KEY  = os.environ.get("INSTANTLY_API_KEY", "")
HUBSPOT_API_KEY    = os.environ.get("HUBSPOT_API_KEY", "")
SLACK_BOT_TOKEN    = os.environ.get("SLACK_BOT_TOKEN", "")

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

def save_automations(automations):
    _redis_set("automations_config", automations)

def get_list_contacts(list_id):
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    contacts = []
    vid_offset = None
    while True:
        url = f"https://api.hubapi.com/contacts/v1/lists/{list_id}/contacts/all?count=100&property=email&property=firstname&property=lastname"
        if vid_offset:
            url += f"&vidOffset={vid_offset}"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            _log(f"[contacts] list {list_id} error: {e}")
            break
        for c in body.get("contacts", []):
            props = c.get("properties", {})
            email = props.get("email", {}).get("value", "").strip().lower()
            first = props.get("firstname", {}).get("value", "")
            last  = props.get("lastname",  {}).get("value", "")
            if email:
                contacts.append({"email": email, "name": f"{first} {last}".strip()})
        if not body.get("has-more", False):
            break
        vid_offset = body.get("vid-offset")
    return contacts

def get_hs_lists():
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    seen = {}

    offset = 0
    while True:
        url = f"https://api.hubapi.com/contacts/v1/lists?count=250&offset={offset}"
        _log(f"[HubSpot v1] GET offset={offset}")
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            _log(f"[HubSpot v1] status={resp.status_code}")
            resp.raise_for_status()
            body = resp.json()
            for l in body.get("lists", []):
                lid = str(l["listId"])
                if lid not in seen:
                    seen[lid] = l["name"]
            if not body.get("has-more", False):
                break
            offset = body.get("offset", offset + 250)
        except Exception as e:
            _log(f"[HubSpot v1] error: {e}")
            break

    after = None
    while True:
        url = "https://api.hubapi.com/crm/v3/lists?objectTypeId=0-1&limit=100"
        if after:
            url += f"&after={after}"
        _log(f"[HubSpot v3] GET after={after}")
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            _log(f"[HubSpot v3] status={resp.status_code} body={resp.text[:300]}")
            resp.raise_for_status()
            body = resp.json()
            for l in body.get("lists", []):
                lid = str(l.get("listId") or l.get("id", ""))
                name = l.get("name", "")
                if lid and lid not in seen:
                    seen[lid] = name
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        except Exception as e:
            _log(f"[HubSpot v3] error: {e}")
            break

    _log(f"[HubSpot] total unique lists={len(seen)}")
    return sorted(
        [{"id": lid, "name": name} for lid, name in seen.items()],
        key=lambda x: x["name"].lower()
    )

def get_hs_forms():
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    forms = []
    after = None
    while True:
        url = "https://api.hubapi.com/marketing/v3/forms?limit=100"
        if after:
            url += f"&after={after}"
        _log(f"[HubSpot forms] GET after={after}")
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            _log(f"[HubSpot forms] status={resp.status_code}")
            resp.raise_for_status()
            body = resp.json()
            for f in body.get("results", []):
                fid = f.get("id", "")
                name = f.get("name", "")
                if fid:
                    forms.append({"id": fid, "name": name})
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        except Exception as e:
            _log(f"[HubSpot forms] error: {e}")
            break
    _log(f"[HubSpot forms] total={len(forms)}")
    return sorted(forms, key=lambda x: x["name"].lower())

def get_hs_contact_properties():
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    props = []
    after = None
    while True:
        url = "https://api.hubapi.com/crm/v3/properties/contacts?limit=500"
        if after:
            url += f"&after={after}"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            for p in body.get("results", []):
                name       = p.get("name", "")
                label      = p.get("label", name)
                field_type = p.get("fieldType", "text")
                prop_type  = p.get("type", "string")
                options = []
                if prop_type == "enumeration" or field_type in ("select", "radio", "checkbox", "booleancheckbox"):
                    options = [
                        {"label": o.get("label", ""), "value": o.get("value", "")}
                        for o in p.get("options", []) if o.get("value")
                    ]
                if name:
                    props.append({"name": name, "label": label, "type": prop_type, "fieldType": field_type, "options": options})
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        except Exception as e:
            _log(f"[HubSpot properties] error: {e}")
            break
    _log(f"[HubSpot properties] total={len(props)}")
    return sorted(props, key=lambda x: x["label"].lower())

def get_slack_channels():
    if not SLACK_BOT_TOKEN:
        return []
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    channels = []
    cursor = None
    while True:
        url = "https://slack.com/api/conversations.list?types=public_channel,private_channel&limit=200&exclude_archived=true"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise Exception(data.get("error", "Slack API error"))
            for ch in data.get("channels", []):
                if ch.get("is_member", False):
                    channels.append({"id": ch["id"], "name": f"#{ch['name']}"})
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        except Exception as e:
            _log(f"[Slack] channels error: {e}")
            break
    return sorted(channels, key=lambda x: x["name"].lower())

def get_instantly_campaigns():
    url = "https://api.instantly.ai/api/v2/campaigns?limit=100"
    headers = {"Authorization": f"Bearer {INSTANTLY_API_KEY}"}
    _log(f"[Instantly] GET {url} (key present: {bool(INSTANTLY_API_KEY)})")
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        _log(f"[Instantly] status={resp.status_code} body={resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            campaigns = data
        elif isinstance(data, dict):
            campaigns = data.get("items", data.get("campaigns", data.get("data", [])))
        else:
            campaigns = []
        _log(f"[Instantly] returned {len(campaigns)} campaigns")
        return [{"id": c.get("id", ""), "name": c.get("name", "")} for c in campaigns]
    except requests.HTTPError as e:
        raise Exception(f"Instantly HTTP {e.response.status_code}: {e.response.text[:400]}")
    except Exception as e:
        raise Exception(f"Instantly request failed: {e}")

def _apply_slack_fields(target, body):
    slack_enabled = bool(body.get("slack_enabled", False))
    target["slack_enabled"] = slack_enabled
    if slack_enabled:
        target["slack_channel"]      = body.get("slack_channel", "")
        target["slack_channel_name"] = body.get("slack_channel_name", "")
        target["slack_message"]      = body.get("slack_message", "")
    else:
        target["slack_channel"]      = ""
        target["slack_channel_name"] = ""
        target["slack_message"]      = ""

def _apply_alert_fields(target, body):
    alert_enabled = bool(body.get("alert_enabled", False))
    target["alert_enabled"] = alert_enabled
    if alert_enabled:
        target["alert_threshold"]        = int(body.get("alert_threshold", 0))
        target["alert_schedule"]         = body.get("alert_schedule", "daily")
        target["alert_day"]              = int(body.get("alert_day", 0))
        target["alert_time"]             = body.get("alert_time", "08:00")
        target["alert_slack_channel"]    = body.get("alert_slack_channel", "")
        target["alert_slack_channel_name"] = body.get("alert_slack_channel_name", "")
        target["alert_message"]          = body.get("alert_message", "")
    else:
        target["alert_threshold"]        = 0
        target["alert_schedule"]         = "daily"
        target["alert_day"]              = 0
        target["alert_time"]             = "08:00"
        target["alert_slack_channel"]    = ""
        target["alert_slack_channel_name"] = ""
        target["alert_message"]          = ""

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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
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
        elif path.endswith("/forms"):
            try:
                self._json(200, get_hs_forms())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path.endswith("/properties"):
            try:
                self._json(200, get_hs_contact_properties())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path.endswith("/slack/channels"):
            try:
                self._json(200, get_slack_channels())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path.endswith("/automations"):
            try:
                self._json(200, get_automations())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif "/contacts/" in path:
            list_id = path.split("/contacts/")[-1].strip("/")
            try:
                self._json(200, get_list_contacts(list_id))
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
            name          = body.get("name", "").strip()
            list_id       = str(body.get("hubspot_list_id", "")).strip()
            list_name     = body.get("hubspot_list_name", "").strip()
            delivery_type = body.get("delivery_type", "instantly")

            if not all([name, list_id, delivery_type]):
                self._json(400, {"error": "Missing fields"})
                return

            existing = get_automations()

            if delivery_type == "instantly":
                camp_id   = str(body.get("instantly_campaign_id", "")).strip()
                camp_name = body.get("instantly_campaign_name", "").strip()
                if not camp_id:
                    self._json(400, {"error": "Missing Instantly campaign"})
                    return
                for a in existing:
                    if a.get("hubspot_list_id") == list_id and a.get("instantly_campaign_id") == camp_id:
                        self._json(409, {"error": "Automation already exists"})
                        return
                action = body.get("action", "enroll")
                if action not in ("enroll", "unenroll"):
                    action = "enroll"
                delay_hours = int(body.get("delay_hours", 0))
                filters = [f for f in body.get("filters", []) if isinstance(f, dict) and f.get("property")]
                new_auto = {
                    "id": f"{list_id}_{camp_id}",
                    "name": name,
                    "delivery_type": "instantly",
                    "hubspot_list_id": list_id,
                    "hubspot_list_name": list_name,
                    "instantly_campaign_id": camp_id,
                    "instantly_campaign_name": camp_name,
                    "action": action,
                    "delay_hours": delay_hours,
                    "filters": filters,
                    "active": True,
                }

            elif delivery_type == "hubspot_form":
                form_id   = str(body.get("hubspot_form_id", "")).strip()
                form_name = body.get("hubspot_form_name", "").strip()
                if not form_id:
                    self._json(400, {"error": "Missing HubSpot form"})
                    return
                for a in existing:
                    if a.get("hubspot_list_id") == list_id and a.get("hubspot_form_id") == form_id:
                        self._json(409, {"error": "Automation already exists"})
                        return
                filters = [f for f in body.get("filters", []) if isinstance(f, dict) and f.get("property")]
                new_auto = {
                    "id": f"{list_id}_form_{form_id}",
                    "name": name,
                    "delivery_type": "hubspot_form",
                    "hubspot_list_id": list_id,
                    "hubspot_list_name": list_name,
                    "hubspot_form_id": form_id,
                    "hubspot_form_name": form_name,
                    "filters": filters,
                    "active": True,
                }
            else:
                self._json(400, {"error": "Invalid delivery_type"})
                return

            _apply_slack_fields(new_auto, body)
            _apply_alert_fields(new_auto, body)

            existing.append(new_auto)
            save_automations(existing)
            self._json(200, new_auto)
        else:
            self._json(404, {"error": "Not found"})

    def do_PATCH(self):
        token = self.headers.get("X-Auth-Token", "")
        if token != DASHBOARD_PASSWORD:
            self._json(401, {"error": "Unauthorized"})
            return

        parts = self.path.strip("/").split("/")
        auto_id = parts[-1] if parts else ""
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        existing = get_automations()
        found = False
        for a in existing:
            if a.get("id") == auto_id:
                if "active" in body:
                    a["active"] = bool(body["active"])
                if "name" in body:
                    a["name"] = str(body["name"]).strip()
                if "hubspot_list_id" in body:
                    a["hubspot_list_id"] = str(body["hubspot_list_id"]).strip()
                    a["hubspot_list_name"] = body.get("hubspot_list_name", "")
                if "instantly_campaign_id" in body:
                    a["instantly_campaign_id"] = str(body["instantly_campaign_id"]).strip()
                    a["instantly_campaign_name"] = body.get("instantly_campaign_name", "")
                if "hubspot_form_id" in body:
                    a["hubspot_form_id"] = str(body["hubspot_form_id"]).strip()
                    a["hubspot_form_name"] = body.get("hubspot_form_name", "")
                if "action" in body:
                    action = body["action"]
                    a["action"] = action if action in ("enroll", "unenroll") else "enroll"
                if "delay_hours" in body:
                    a["delay_hours"] = int(body["delay_hours"])
                if "filters" in body:
                    a["filters"] = [f for f in body["filters"] if isinstance(f, dict) and f.get("property")]
                if "slack_enabled" in body:
                    _apply_slack_fields(a, body)
                if "alert_enabled" in body:
                    _apply_alert_fields(a, body)
                found = True
                break

        if not found:
            self._json(404, {"error": "Not found"})
            return
        save_automations(existing)
        self._json(200, {"ok": True})

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
