import json
import datetime
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

def _redis_get_int(key):
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    val = data.get("result")
    return int(val) if val else 0

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
        url = f"https://api.hubapi.com/contacts/v1/lists/{list_id}/contacts/all?count=100&property=email&property=firstname&property=lastname&property=createdate"
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
                created_ms = props.get("createdate", {}).get("value", "")
                created_iso = ""
                if created_ms:
                    try:
                        created_iso = datetime.datetime.fromtimestamp(
                            int(created_ms)/1000, tz=datetime.timezone.utc
                        ).isoformat()
                    except Exception:
                        pass
                contacts.append({"email": email, "name": f"{first} {last}".strip(), "created": created_iso})
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

def get_service_account_email():
    """Extract client_email from GOOGLE_SERVICE_ACCOUNT_JSON env var."""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    try:
        return json.loads(sa_json).get("client_email")
    except Exception:
        return None

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
    """Copy all GSheet-specific fields from request body onto the automation record."""
    target["sheet_url"]          = body.get("sheet_url", "").strip()
    target["sheet_tab"]          = body.get("sheet_tab", "").strip()
    target["object_type"]        = body.get("object_type", "contact")
    target["primary_key_column"] = body.get("primary_key_column", "").strip()
    target["primary_key_type"]   = body.get("primary_key_type", "email")
    target["column_mappings"]    = [
        m for m in body.get("column_mappings", [])
        if isinstance(m, dict) and m.get("column") and m.get("property")
    ]
    # Schedule
    target["gsheet_schedule_type"]    = body.get("gsheet_schedule_type", "interval")
    target["gsheet_interval_minutes"] = int(body.get("gsheet_interval_minutes", 60))
    target["gsheet_run_time"]         = body.get("gsheet_run_time", "08:00")
    target["gsheet_run_day"]          = int(body.get("gsheet_run_day", 0))
    # Deal-specific defaults
    target["default_pipeline"] = body.get("default_pipeline", "").strip()
    target["default_stage"]    = body.get("default_stage", "").strip()


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
        elif path.endswith("/google/service-account-email"):
            email = get_service_account_email()
            if email:
                self._json(200, {"email": email})
            else:
                self._json(404, {"error": "GOOGLE_SERVICE_ACCOUNT_JSON not configured"})
        elif path.endswith('/activity'):
            try:
                automations_list = get_automations()
                est       = datetime.timezone(datetime.timedelta(hours=-5))
                est_now   = datetime.datetime.now(est)
                now_utc   = datetime.datetime.now(datetime.timezone.utc)
                # Cutoff = midnight today in Florida (EDT = UTC-4)
                edt = datetime.timezone(datetime.timedelta(hours=-4))
                today_edt = datetime.datetime.now(edt).replace(hour=0, minute=0, second=0, microsecond=0)
                cutoff_24h = today_edt.astimezone(datetime.timezone.utc)
                day_totals = {}
                daily      = {}

                for a in automations_list:
                    auto_id       = a.get('id','')
                    delivery_type = a.get('delivery_type','')
                    if not auto_id:
                        continue

                    # GSheet automations — read from Redis logs
                    if delivery_type == 'gsheet_sync':
                        try:
                            log_url = f"{UPSTASH_URL}/lrange/logs:{auto_id}/0/999"
                            log_req = Request(log_url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
                            with urlopen(log_req, timeout=8) as r:
                                log_data = json.loads(r.read())
                            entries   = log_data.get("result", [])
                            count_24h = 0
                            for entry_str in entries:
                                try:
                                    entry = json.loads(entry_str)
                                    ts    = entry.get("ts", 0)
                                    if not ts:
                                        continue
                                    try:
                                        dt = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
                                    except (ValueError, TypeError):
                                        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                                    day_key = dt.astimezone(est).date().isoformat()
                                    day_totals[day_key] = day_totals.get(day_key, 0) + 1
                                    if dt >= cutoff_24h:
                                        count_24h += 1
                                except Exception:
                                    continue
                            if count_24h > 0:
                                daily[auto_id] = count_24h
                        except Exception:
                            pass

                    # Instantly / Form automations — read createdate from HubSpot list
                    else:
                        list_id = a.get('hubspot_list_id','')
                        if not list_id:
                            continue
                        try:
                            contacts  = get_list_contacts(list_id)
                            count_24h = 0
                            for c in contacts:
                                created = c.get('created','')
                                if not created:
                                    continue
                                try:
                                    dt = datetime.datetime.fromisoformat(created.replace("Z","+00:00"))
                                    day_key = dt.astimezone(est).date().isoformat()
                                    day_totals[day_key] = day_totals.get(day_key, 0) + 1
                                    if dt >= cutoff_24h:
                                        count_24h += 1
                                except Exception:
                                    continue
                            if count_24h > 0:
                                daily[auto_id] = count_24h
                        except Exception:
                            pass

                monthly = []
                for i in range(29, -1, -1):
                    day = (est_now - datetime.timedelta(days=i)).date().isoformat()
                    monthly.append(day_totals.get(day, 0))
                self._json(200, {'daily': daily, 'monthly': monthly})
            except Exception as e:
                self._json(500, {'error': str(e)})

        elif "/logs/" in path:
            from urllib.parse import unquote
            auto_id = unquote(path.split("/logs/")[-1].strip("/"))
            try:
                # Find the automation
                automations_list = get_automations()
                auto = next((a for a in automations_list if a.get("id") == auto_id), None)
                if not auto:
                    self._json(404, {"error": "Automation not found"})
                    return

                sheet_url = auto.get("sheet_url", "")
                sheet_tab = auto.get("sheet_tab", "")
                pk_column = auto.get("primary_key_column", "Email")

                if not sheet_url:
                    self._json(200, [])
                    return

                # Get Google token
                import re as _re
                import google.oauth2.service_account
                import google.auth.transport.requests as google_requests
                from urllib.parse import quote

                sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
                creds_data = json.loads(sa_json)
                creds = google.oauth2.service_account.Credentials.from_service_account_info(
                    creds_data, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
                )
                creds.refresh(google_requests.Request())
                token = creds.token

                # Extract sheet ID
                match = _re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url)
                if not match:
                    self._json(400, {"error": "Invalid sheet URL"})
                    return
                sheet_id = match.group(1)

                # Fetch sheet data
                range_name = quote(f"'{sheet_tab}'", safe='') if sheet_tab else "A1:ZZ"
                sheets_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
                import requests as req_lib
                resp = req_lib.get(sheets_url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
                resp.raise_for_status()
                sheet_data = resp.json().get("values", [])

                if not sheet_data or len(sheet_data) < 2:
                    self._json(200, [])
                    return

                headers = [h.strip() for h in sheet_data[0]]
                pk_idx  = headers.index(pk_column) if pk_column in headers else 0

                # Build name from first/last name columns if available
                fn_idx = next((i for i, h in enumerate(headers) if h.lower() in ('first name','firstname','first')), None)
                ln_idx = next((i for i, h in enumerate(headers) if h.lower() in ('last name','lastname','last')), None)

                rows = []
                for row in sheet_data[1:]:
                    padded = list(row) + [''] * max(0, len(headers) - len(row))
                    email  = padded[pk_idx].strip() if pk_idx < len(padded) else ''
                    if not email:
                        continue
                    first = padded[fn_idx].strip() if fn_idx is not None and fn_idx < len(padded) else ''
                    last  = padded[ln_idx].strip() if ln_idx is not None and ln_idx < len(padded) else ''
                    name  = f"{first} {last}".strip() or email
                    rows.append({"email": email, "name": name, "created": ""})

                self._json(200, rows)
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

        if not path.endswith("/automations"):
            self._json(404, {"error": "Not found"})
            return

        name          = body.get("name", "").strip()
        delivery_type = body.get("delivery_type", "instantly")

        if not name or not delivery_type:
            self._json(400, {"error": "Missing fields"})
            return

        existing = get_automations()

        # ── GSheet → HubSpot sync ─────────────────────────────
        if delivery_type == "gsheet_sync":
            sheet_url       = body.get("sheet_url", "").strip()
            object_type     = body.get("object_type", "contact")
            pk_column       = body.get("primary_key_column", "").strip()
            column_mappings = [
                m for m in body.get("column_mappings", [])
                if isinstance(m, dict) and m.get("column") and m.get("property")
            ]

            if not sheet_url:
                self._json(400, {"error": "Missing Google Sheet URL"})
                return
            if not pk_column:
                self._json(400, {"error": "Missing primary key column"})
                return
            if not column_mappings:
                self._json(400, {"error": "At least one column mapping is required"})
                return

            match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url)
            if not match:
                self._json(400, {"error": "Invalid Google Sheet URL"})
                return
            sheet_id = match.group(1)

            # Duplicate check: same sheet URL + object type
            for a in existing:
                if a.get("delivery_type") == "gsheet_sync" and \
                   a.get("sheet_url") == sheet_url and \
                   a.get("object_type") == object_type:
                    self._json(409, {"error": "A GSheet sync for this sheet and object type already exists"})
                    return

            new_auto = {
                "id":            f"gs_{sheet_id}_{object_type}",
                "name":          name,
                "delivery_type": "gsheet_sync",
                "active":        True,
            }
            _apply_gsheet_fields(new_auto, body)
            _apply_slack_fields(new_auto, body)
            # GSheet syncs don't use the enrollment alert
            existing.append(new_auto)
            save_automations(existing)
            self._json(200, new_auto)
            return

        # ── Calendly ─────────────────────────────────────────
        if delivery_type == "calendly":
            event_url = body.get("calendly_event_url", "").strip()
            if not event_url:
                self._json(400, {"error": "Please enter your Calendly event URL"})
                return
            import hashlib as _hl
            new_auto = {
                "id":                 f"calendly_{_hl.md5(event_url.encode()).hexdigest()[:8]}",
                "name":               name,
                "delivery_type":      "calendly",
                "calendly_event_url": event_url,
                "property_mappings":  body.get("property_mappings", []),
                "active":             True,
            }
            _apply_slack_fields(new_auto, body)
            existing.append(new_auto)
            save_automations(existing)
            self._json(200, new_auto)
            return

        # ── Instantly + HS Form: both require a HubSpot list ─
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
            filters = [f for f in body.get("filters", []) if isinstance(f, dict) and f.get("property")]
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
            filters = [f for f in body.get("filters", []) if isinstance(f, dict) and f.get("property")]
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

        parts = self.path.strip("/").split("/")
        auto_id = parts[-1] if parts else ""
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        existing = get_automations()
        found = False
        for a in existing:
            if a.get("id") == auto_id:
                # Fields common to all types
                if "active" in body:
                    a["active"] = bool(body["active"])
                if "name" in body:
                    a["name"] = str(body["name"]).strip()

                if a.get("delivery_type") == "calendly":
                    if "calendly_event_url" in body: a["calendly_event_url"] = body["calendly_event_url"]
                    if "property_mappings"  in body: a["property_mappings"]  = body["property_mappings"]
                    if "slack_enabled" in body: _apply_slack_fields(a, body)

                elif a.get("delivery_type") == "gsheet_sync":
                    # Re-apply all gsheet fields if any gsheet key is present
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
