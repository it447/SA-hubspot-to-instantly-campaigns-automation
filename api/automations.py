import json
import os
import sys
import re
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import requests

UPSTASH_URL        = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN      = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
INSTANTLY_API_KEY  = os.environ.get("INSTANTLY_API_KEY", "")
HUBSPOT_API_KEY    = os.environ.get("HUBSPOT_API_KEY", "")
SLACK_BOT_TOKEN    = os.environ.get("SLACK_BOT_TOKEN", "")
CLAY_API_KEY       = os.environ.get("CLAY_API_KEY", "")

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
        try:
            resp = requests.get(url, headers=headers, timeout=10)
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
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            body = resp.json()
            for l in body.get("lists", []):
                lid  = str(l.get("listId") or l.get("id", ""))
                name = l.get("name", "")
                if lid and lid not in seen:
                    seen[lid] = name
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        except Exception as e:
            _log(f"[HubSpot v3] error: {e}")
            break
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
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            body = resp.json()
            for f in body.get("results", []):
                fid  = f.get("id", "")
                name = f.get("name", "")
                if fid:
                    forms.append({"id": fid, "name": name})
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        except Exception as e:
            _log(f"[HubSpot forms] error: {e}")
            break
    return sorted(forms, key=lambda x: x["name"].lower())

def get_hs_properties(object_type="contacts"):
    hs_object = {
        "contact":   "contacts",
        "contacts":  "contacts",
        "company":   "companies",
        "companies": "companies",
        "deal":      "deals",
        "deals":     "deals",
    }.get(object_type, "contacts")

    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    props = []
    after = None
    while True:
        url = f"https://api.hubapi.com/crm/v3/properties/{hs_object}?limit=500"
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
                    props.append({
                        "name": name, "label": label,
                        "type": prop_type, "fieldType": field_type,
                        "options": options
                    })
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        except Exception as e:
            _log(f"[HubSpot properties/{hs_object}] error: {e}")
            break
    _log(f"[HubSpot properties/{hs_object}] total={len(props)}")
    return sorted(props, key=lambda x: x["label"].lower())

def get_deal_pipelines():
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    try:
        resp = requests.get(
            "https://api.hubapi.com/crm/v3/pipelines/deals",
            headers=headers, timeout=10
        )
        resp.raise_for_status()
        pipelines = []
        for p in resp.json().get("results", []):
            stages = sorted(
                [{"id": s.get("id", ""), "label": s.get("label", "")}
                 for s in p.get("stages", [])],
                key=lambda s: s["label"].lower()
            )
            pipelines.append({
                "id":     p.get("id", ""),
                "label":  p.get("label", ""),
                "stages": stages,
            })
        _log(f"[HubSpot pipelines] total={len(pipelines)}")
        return sorted(pipelines, key=lambda x: x["label"].lower())
    except Exception as e:
        _log(f"[HubSpot pipelines] error: {e}")
        return []

def get_slack_channels():
    if not SLACK_BOT_TOKEN:
        return []
    headers  = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    channels = []
    cursor   = None
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
    url     = "https://api.instantly.ai/api/v2/campaigns?limit=100"
    headers = {"Authorization": f"Bearer {INSTANTLY_API_KEY}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            campaigns = data
        elif isinstance(data, dict):
            campaigns = data.get("items", data.get("campaigns", data.get("data", [])))
        else:
            campaigns = []
        return [{"id": c.get("id", ""), "name": c.get("name", "")} for c in campaigns]
    except requests.HTTPError as e:
        raise Exception(f"Instantly HTTP {e.response.status_code}: {e.response.text[:400]}")
    except Exception as e:
        raise Exception(f"Instantly request failed: {e}")

def get_service_account_email():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    try:
        return json.loads(sa_json).get("client_email")
    except Exception:
        return None

# ── Clay API helpers ──────────────────────────────────────────

def get_clay_table_columns(table_id, api_key=None):
    """Fetch column definitions for a Clay table."""
    key = api_key or CLAY_API_KEY
    if not key:
        raise ValueError("CLAY_API_KEY not configured")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json"
    }
    url  = f"https://api.clay.com/v1/sources/{table_id}/columns"
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code == 401:
        raise ValueError("Invalid Clay API key")
    if resp.status_code == 404:
        raise ValueError(f"Clay table '{table_id}' not found")
    resp.raise_for_status()
    data    = resp.json()
    columns = []
    for col in data.get("columns", data.get("data", [])):
        col_id   = col.get("id",   col.get("slug",  ""))
        col_name = col.get("name", col.get("label", col_id))
        if col_id:
            columns.append({"id": col_id, "name": col_name})
    return sorted(columns, key=lambda x: x["name"].lower())

def validate_clay_key(api_key):
    """Quick check that the Clay API key is valid."""
    headers = {"Authorization": f"Bearer {api_key}"}
    resp    = requests.get("https://api.clay.com/v1/me", headers=headers, timeout=10)
    return resp.status_code == 200

# ── Field helpers ─────────────────────────────────────────────

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
        target["alert_threshold"]          = int(body.get("alert_threshold", 0))
        target["alert_schedule"]           = body.get("alert_schedule", "daily")
        target["alert_day"]                = int(body.get("alert_day", 0))
        target["alert_time"]               = body.get("alert_time", "08:00")
        target["alert_slack_channel"]      = body.get("alert_slack_channel", "")
        target["alert_slack_channel_name"] = body.get("alert_slack_channel_name", "")
        target["alert_message"]            = body.get("alert_message", "")
    else:
        target["alert_threshold"]          = 0
        target["alert_schedule"]           = "daily"
        target["alert_day"]                = 0
        target["alert_time"]               = "08:00"
        target["alert_slack_channel"]      = ""
        target["alert_slack_channel_name"] = ""
        target["alert_message"]            = ""

def _apply_gsheet_fields(target, body):
    target["sheet_url"]          = body.get("sheet_url", "").strip()
    target["sheet_tab"]          = body.get("sheet_tab", "").strip()
    target["object_type"]        = body.get("object_type", "contact")
    target["primary_key_column"] = body.get("primary_key_column", "").strip()
    target["primary_key_type"]   = body.get("primary_key_type", "email")
    target["column_mappings"]    = [
        m for m in body.get("column_mappings", [])
        if isinstance(m, dict) and m.get("column") and m.get("property")
    ]
    target["gsheet_schedule_type"]    = body.get("gsheet_schedule_type", "interval")
    target["gsheet_interval_minutes"] = int(body.get("gsheet_interval_minutes", 60))
    target["gsheet_run_time"]         = body.get("gsheet_run_time", "08:00")
    target["gsheet_run_day"]          = int(body.get("gsheet_run_day", 0))
    target["default_pipeline"]        = body.get("default_pipeline", "").strip()
    target["default_pipeline_label"]  = body.get("default_pipeline_label", "").strip()
    target["default_stage"]           = body.get("default_stage", "").strip()
    target["default_stage_label"]     = body.get("default_stage_label", "").strip()

def _apply_clay_fields(target, body):
    target["clay_enabled"]         = bool(body.get("clay_enabled", False))
    target["clay_table_id"]        = body.get("clay_table_id", "").strip()
    target["clay_column_mappings"] = [
        m for m in body.get("clay_column_mappings", [])
        if isinstance(m, dict) and m.get("hs_property") and m.get("clay_column")
    ]

def _apply_enrichment_gsheet_fields(target, body):
    target["enrichment_gsheet_enabled"] = bool(body.get("enrichment_gsheet_enabled", False))
    target["sheet_url"]                 = body.get("sheet_url", "").strip()
    target["sheet_tab"]                 = body.get("sheet_tab", "").strip()
    target["object_type"]               = body.get("object_type", "contact")
    target["primary_key_column"]        = body.get("primary_key_column", "").strip()
    target["primary_key_type"]          = body.get("primary_key_type", "email")
    target["column_mappings"]           = [
        m for m in body.get("column_mappings", [])
        if isinstance(m, dict) and m.get("column") and m.get("property")
    ]
    target["gsheet_schedule_type"]    = body.get("gsheet_schedule_type", "interval")
    target["gsheet_interval_minutes"] = int(body.get("gsheet_interval_minutes", 60))
    target["gsheet_run_time"]         = body.get("gsheet_run_time", "08:00")
    target["gsheet_run_day"]          = int(body.get("gsheet_run_day", 0))


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
        path  = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""
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
            object_type = "contacts"
            for part in query.split("&"):
                if part.startswith("object="):
                    object_type = part.split("=", 1)[1]
            try:
                self._json(200, get_hs_properties(object_type))
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.endswith("/pipelines"):
            try:
                self._json(200, get_deal_pipelines())
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

        elif path.endswith("/google/service-account-email"):
            email = get_service_account_email()
            if email:
                self._json(200, {"email": email})
            else:
                self._json(404, {"error": "GOOGLE_SERVICE_ACCOUNT_JSON not configured"})

        elif "/contacts/" in path:
            list_id = path.split("/contacts/")[-1].strip("/")
            try:
                self._json(200, get_list_contacts(list_id))
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Clay endpoints ────────────────────────────────────
        elif path.endswith("/clay/columns"):
            # ?table_id=xxx
            table_id = ""
            for part in query.split("&"):
                if part.startswith("table_id="):
                    table_id = part.split("=", 1)[1]
            if not table_id:
                self._json(400, {"error": "Missing table_id"})
                return
            try:
                columns = get_clay_table_columns(table_id)
                self._json(200, columns)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.endswith("/clay/validate"):
            # ?api_key=xxx  — validate a Clay API key
            api_key = ""
            for part in query.split("&"):
                if part.startswith("api_key="):
                    api_key = part.split("=", 1)[1]
            if not api_key:
                self._json(400, {"error": "Missing api_key"})
                return
            try:
                valid = validate_clay_key(api_key)
                self._json(200, {"valid": valid})
            except Exception as e:
                self._json(500, {"error": str(e)})

        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        path   = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

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

        if not path.endswith("/automations"):
            self._json(404, {"error": "Not found"})
            return

        name          = body.get("name", "").strip()
        delivery_type = body.get("delivery_type", "instantly")

        if not name or not delivery_type:
            self._json(400, {"error": "Missing fields"})
            return

        existing = get_automations()

        # ── GSheet → HubSpot sync / Clay + GSheet ────────────
        if delivery_type == "gsheet_sync":
            clay_enabled   = bool(body.get("clay_enabled", False))
            gsheet_enabled = bool(body.get("gsheet_enabled", False))
            sheet_url      = body.get("sheet_url", "").strip()
            object_type    = body.get("object_type", "contact")
            pk_column      = body.get("primary_key_column", "").strip()
            column_mappings = [
                m for m in body.get("column_mappings", [])
                if isinstance(m, dict) and m.get("column") and m.get("property")
            ]

            if not clay_enabled and not gsheet_enabled:
                self._json(400, {"error": "Please enable at least one automation (Clay or GSheet)"})
                return

            if clay_enabled:
                if not body.get("clay_hubspot_list_id", "").strip():
                    self._json(400, {"error": "Clay: please select a HubSpot list"})
                    return
                if not body.get("clay_table_id", "").strip():
                    self._json(400, {"error": "Clay: please enter the Clay table ID"})
                    return
                clay_maps = [m for m in body.get("clay_column_mappings", []) if isinstance(m, dict) and m.get("hs_property") and m.get("clay_column")]
                if not clay_maps:
                    self._json(400, {"error": "Clay: please add at least one column mapping"})
                    return

            if gsheet_enabled:
                if not sheet_url:
                    self._json(400, {"error": "GSheet: please enter the Google Sheet URL"})
                    return
                if not pk_column:
                    self._json(400, {"error": "GSheet: please enter the primary key column"})
                    return
                if not column_mappings:
                    self._json(400, {"error": "GSheet: at least one column mapping is required"})
                    return
                match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url)
                if not match:
                    self._json(400, {"error": "GSheet: invalid Google Sheet URL"})
                    return

            # Use sheet_id for auto id if gsheet enabled, else use clay table id
            if gsheet_enabled:
                match   = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url)
                sheet_id = match.group(1) if match else "unknown"
            else:
                sheet_id = body.get("clay_table_id", "clay").strip()

            new_auto = {
                "id":            f"gs_{sheet_id}_{object_type}",
                "name":          name,
                "delivery_type": "gsheet_sync",
                "active":        True,
            }
            _apply_gsheet_fields(new_auto, body)
            _apply_slack_fields(new_auto, body)
            existing.append(new_auto)
            save_automations(existing)
            self._json(200, new_auto)
            return

        # ── Enrichment (HubSpot → Clay + GSheet → HubSpot) ───
        if delivery_type == "enrichment":
            list_id   = str(body.get("hubspot_list_id", "")).strip()
            list_name = body.get("hubspot_list_name", "").strip()

            if not list_id:
                self._json(400, {"error": "Missing HubSpot list"})
                return

            clay_enabled  = bool(body.get("clay_enabled", False))
            gsheet_enabled = bool(body.get("enrichment_gsheet_enabled", False))

            if not clay_enabled and not gsheet_enabled:
                self._json(400, {"error": "At least one of Clay push or GSheet pull must be enabled"})
                return

            if clay_enabled:
                clay_table_id      = body.get("clay_table_id", "").strip()
                clay_col_mappings  = [
                    m for m in body.get("clay_column_mappings", [])
                    if isinstance(m, dict) and m.get("hs_property") and m.get("clay_column")
                ]
                if not clay_table_id:
                    self._json(400, {"error": "Missing Clay table ID"})
                    return
                if not clay_col_mappings:
                    self._json(400, {"error": "At least one Clay column mapping is required"})
                    return

            if gsheet_enabled:
                sheet_url = body.get("sheet_url", "").strip()
                pk_column = body.get("primary_key_column", "").strip()
                col_maps  = [
                    m for m in body.get("column_mappings", [])
                    if isinstance(m, dict) and m.get("column") and m.get("property")
                ]
                if not sheet_url:
                    self._json(400, {"error": "Missing Google Sheet URL for GSheet pull"})
                    return
                if not pk_column:
                    self._json(400, {"error": "Missing primary key column for GSheet pull"})
                    return
                if not col_maps:
                    self._json(400, {"error": "At least one GSheet column mapping is required"})
                    return

            new_auto = {
                "id":                f"enrich_{list_id}",
                "name":              name,
                "delivery_type":     "enrichment",
                "hubspot_list_id":   list_id,
                "hubspot_list_name": list_name,
                "active":            True,
            }
            _apply_clay_fields(new_auto, body)
            _apply_enrichment_gsheet_fields(new_auto, body)
            _apply_slack_fields(new_auto, body)
            existing.append(new_auto)
            save_automations(existing)
            self._json(200, new_auto)
            return

        # ── Instantly + HS Form ───────────────────────────────
        list_id   = str(body.get("hubspot_list_id", "")).strip()
        list_name = body.get("hubspot_list_name", "").strip()

        if not list_id:
            self._json(400, {"error": "Missing HubSpot list"})
            return

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
            filters     = [f for f in body.get("filters", []) if isinstance(f, dict) and f.get("property")]
            new_auto = {
                "id":                      f"{list_id}_{camp_id}",
                "name":                    name,
                "delivery_type":           "instantly",
                "hubspot_list_id":         list_id,
                "hubspot_list_name":       list_name,
                "instantly_campaign_id":   camp_id,
                "instantly_campaign_name": camp_name,
                "action":                  action,
                "delay_hours":             delay_hours,
                "filters":                 filters,
                "active":                  True,
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
            filters  = [f for f in body.get("filters", []) if isinstance(f, dict) and f.get("property")]
            new_auto = {
                "id":                f"{list_id}_form_{form_id}",
                "name":              name,
                "delivery_type":     "hubspot_form",
                "hubspot_list_id":   list_id,
                "hubspot_list_name": list_name,
                "hubspot_form_id":   form_id,
                "hubspot_form_name": form_name,
                "filters":           filters,
                "active":            True,
            }
        else:
            self._json(400, {"error": "Invalid delivery_type"})
            return

        _apply_slack_fields(new_auto, body)
        _apply_alert_fields(new_auto, body)
        existing.append(new_auto)
        save_automations(existing)
        self._json(200, new_auto)

    def do_PATCH(self):
        token = self.headers.get("X-Auth-Token", "")
        if token != DASHBOARD_PASSWORD:
            self._json(401, {"error": "Unauthorized"})
            return

        parts   = self.path.strip("/").split("/")
        auto_id = parts[-1] if parts else ""
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length)) if length else {}

        existing = get_automations()
        found    = False
        for a in existing:
            if a.get("id") == auto_id:
                if "active" in body:
                    a["active"] = bool(body["active"])
                if "name" in body:
                    a["name"] = str(body["name"]).strip()

                dt = a.get("delivery_type")

                if dt == "gsheet_sync":
                    gsheet_keys = {
                        "sheet_url", "sheet_tab", "object_type", "primary_key_column",
                        "primary_key_type", "column_mappings", "gsheet_schedule_type",
                        "gsheet_interval_minutes", "gsheet_run_time", "gsheet_run_day",
                        "default_pipeline", "default_stage"
                    }
                    if gsheet_keys & body.keys():
                        _apply_gsheet_fields(a, body)
                    if "slack_enabled" in body:
                        _apply_slack_fields(a, body)

                elif dt == "enrichment":
                    if "clay_enabled" in body or "clay_table_id" in body or "clay_column_mappings" in body:
                        _apply_clay_fields(a, body)
                    enrichment_gsheet_keys = {
                        "enrichment_gsheet_enabled", "sheet_url", "sheet_tab",
                        "object_type", "primary_key_column", "primary_key_type",
                        "column_mappings", "gsheet_schedule_type",
                        "gsheet_interval_minutes", "gsheet_run_time", "gsheet_run_day"
                    }
                    if enrichment_gsheet_keys & body.keys():
                        _apply_enrichment_gsheet_fields(a, body)
                    if "hubspot_list_id" in body:
                        a["hubspot_list_id"]   = str(body["hubspot_list_id"]).strip()
                        a["hubspot_list_name"] = body.get("hubspot_list_name", "")
                    if "slack_enabled" in body:
                        _apply_slack_fields(a, body)

                else:
                    if "hubspot_list_id" in body:
                        a["hubspot_list_id"]   = str(body["hubspot_list_id"]).strip()
                        a["hubspot_list_name"] = body.get("hubspot_list_name", "")
                    if "instantly_campaign_id" in body:
                        a["instantly_campaign_id"]   = str(body["instantly_campaign_id"]).strip()
                        a["instantly_campaign_name"] = body.get("instantly_campaign_name", "")
                    if "hubspot_form_id" in body:
                        a["hubspot_form_id"]   = str(body["hubspot_form_id"]).strip()
                        a["hubspot_form_name"] = body.get("hubspot_form_name", "")
                    if "action" in body:
                        action    = body["action"]
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

        parts    = self.path.strip("/").split("/")
        auto_id  = parts[-1] if parts else ""
        existing = get_automations()
        updated  = [a for a in existing if a.get("id") != auto_id]
        if len(updated) == len(existing):
            self._json(404, {"error": "Not found"})
            return
        save_automations(updated)
        self._json(200, {"ok": True})

    def log_message(self, *args):
        pass
