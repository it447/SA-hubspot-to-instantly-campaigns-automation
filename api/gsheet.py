import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
import requests

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
HUBSPOT_API_KEY    = os.environ.get("HUBSPOT_API_KEY", "")
GOOGLE_SA_JSON     = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

def get_google_token():
    import google.oauth2.service_account
    import google.auth.transport.requests as google_requests
    if not GOOGLE_SA_JSON or GOOGLE_SA_JSON == "{}":
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds_data = json.loads(GOOGLE_SA_JSON)
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        creds_data,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    creds.refresh(google_requests.Request())
    return creds.token

def get_service_account_email():
    if not GOOGLE_SA_JSON or GOOGLE_SA_JSON == "{}":
        return ""
    try:
        return json.loads(GOOGLE_SA_JSON).get("client_email", "")
    except Exception:
        return ""

def extract_sheet_id(url):
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
    if not match:
        raise ValueError("Invalid Google Sheet URL — copy it directly from your browser address bar")
    return match.group(1)

def get_sheet_tabs(sheet_id, token):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}?fields=sheets.properties.title"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if resp.status_code == 403:
        raise PermissionError(f"Access denied. Share the sheet with: {get_service_account_email()}")
    if resp.status_code == 404:
        raise ValueError("Sheet not found. Check the URL is correct.")
    resp.raise_for_status()
    return [s["properties"]["title"] for s in resp.json().get("sheets", [])]

def get_sheet_headers(sheet_id, tab_name, token):
    range_name = quote(f"'{tab_name}'!1:1", safe='')
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if resp.status_code == 403:
        raise PermissionError(f"Access denied. Share the sheet with: {get_service_account_email()}")
    resp.raise_for_status()
    values = resp.json().get("values", [[]])
    return [h.strip() for h in (values[0] if values else [])]

def get_hs_properties(object_type):
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    props = []
    after = None
    hs_type = {"contact": "contacts", "company": "companies", "deal": "deals"}.get(object_type, "contacts")
    while True:
        url = f"https://api.hubapi.com/crm/v3/properties/{hs_type}?limit=500"
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
                    options = [{"label": o.get("label", ""), "value": o.get("value", "")}
                               for o in p.get("options", []) if o.get("value")]
                if name:
                    props.append({"name": name, "label": label, "type": prop_type,
                                  "fieldType": field_type, "options": options})
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        except Exception as e:
            _log(f"[gsheet] HubSpot properties error: {e}")
            break
    return sorted(props, key=lambda x: x["label"].lower())

def get_hs_pipelines():
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    try:
        resp = requests.get("https://api.hubapi.com/crm/v3/pipelines/deals", headers=headers, timeout=10)
        resp.raise_for_status()
        pipelines = []
        for p in resp.json().get("results", []):
            stages = sorted(
                [{"id": s.get("id", ""), "label": s.get("label", "")}
                 for s in p.get("stages", [])],
                key=lambda s: s["label"]
            )
            pipelines.append({"id": p.get("id", ""), "label": p.get("label", ""), "stages": stages})
        return pipelines
    except Exception as e:
        _log(f"[gsheet] pipelines error: {e}")
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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()

    def do_GET(self):
        token = self.headers.get("X-Auth-Token", "")
        if token != DASHBOARD_PASSWORD:
            self._json(401, {"error": "Unauthorized"})
            return

        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        def p(key): return params.get(key, [""])[0]

        if path.endswith("/gsheet/email"):
            email = get_service_account_email()
            if not email:
                self._json(500, {"error": "GOOGLE_SERVICE_ACCOUNT_JSON not configured"})
            else:
                self._json(200, {"email": email})

        elif path.endswith("/gsheet/tabs"):
            sheet_url = p("url")
            if not sheet_url:
                self._json(400, {"error": "Missing url parameter"})
                return
            try:
                gtoken   = get_google_token()
                sheet_id = extract_sheet_id(sheet_url)
                tabs     = get_sheet_tabs(sheet_id, gtoken)
                self._json(200, tabs)
            except PermissionError as e:
                self._json(403, {"error": str(e)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.endswith("/gsheet/headers"):
            sheet_url = p("url")
            tab       = p("tab")
            if not sheet_url or not tab:
                self._json(400, {"error": "Missing url or tab parameter"})
                return
            try:
                gtoken   = get_google_token()
                sheet_id = extract_sheet_id(sheet_url)
                headers  = get_sheet_headers(sheet_id, tab, gtoken)
                self._json(200, headers)
            except PermissionError as e:
                self._json(403, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.endswith("/gsheet/hs-properties"):
            obj_type = p("type") or "contact"
            try:
                self._json(200, get_hs_properties(obj_type))
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.endswith("/gsheet/pipelines"):
            try:
                self._json(200, get_hs_pipelines())
            except Exception as e:
                self._json(500, {"error": str(e)})

        else:
            self._json(404, {"error": "Not found"})

    def log_message(self, *args):
        pass
