import json
import os
import sys
import datetime
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request

UPSTASH_URL     = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN   = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
SYNC_SECRET     = os.environ.get("SYNC_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

EST = datetime.timezone(datetime.timedelta(hours=-5))

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

# ── Redis helpers ─────────────────────────────────────────────

def _redis_get(key):
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    val = data.get("result")
    return json.loads(val) if val else None

def _redis_set_json(key, value):
    url = f"{UPSTASH_URL}/set/{key}"
    body = json.dumps(value).encode()
    req = Request(url, data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()

def _redis_set_raw(key, value):
    url = f"{UPSTASH_URL}/set/{key}/{value}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        r.read()

def get_automations():
    data = _redis_get("automations_config")
    return data if isinstance(data, list) else []

def save_automations(automations):
    _redis_set_json("automations_config", automations)

# ── Google auth token cache ───────────────────────────────────

def get_google_token():
    """Get Google auth token, using Redis cache to avoid refreshing every run."""
    # Check cache first
    cached = _redis_get("gsheet_token_cache")
    if cached and cached.get("token") and cached.get("expires_at"):
        try:
            expires_at = datetime.datetime.fromisoformat(cached["expires_at"])
            now = datetime.datetime.now(datetime.timezone.utc)
            # Use cached token if it has more than 5 minutes left
            if (expires_at - now).total_seconds() > 300:
                _log("[gsheet] using cached Google token")
                return cached["token"]
        except Exception:
            pass

    # Refresh token
    _log("[gsheet] refreshing Google token")
    import google.oauth2.service_account
    import google.auth.transport.requests as google_requests

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    if not sa_json or sa_json == "{}":
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not configured")

    creds_data = json.loads(sa_json)
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        creds_data, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    creds.refresh(google_requests.Request())

    # Cache token in Redis with expiry info (Google tokens last 60 minutes)
    expires_at = (datetime.datetime.now(datetime.timezone.utc) +
                  datetime.timedelta(minutes=55)).isoformat()
    try:
        _redis_set_json("gsheet_token_cache", {
            "token": creds.token,
            "expires_at": expires_at
        })
    except Exception as e:
        _log(f"[gsheet] token cache save failed (non-fatal): {e}")

    return creds.token

# ── Google Sheet helpers ──────────────────────────────────────

def extract_sheet_id(url):
    import re
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
    if not match:
        raise ValueError(f"Invalid Google Sheet URL: {url}")
    return match.group(1)

def get_sheet_data(sheet_id, tab_name, token):
    from urllib.parse import quote
    if tab_name:
        range_name = quote(f"'{tab_name}'", safe='')
    else:
        range_name = "A1:ZZ"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("values", [])

# ── HubSpot batch helpers ─────────────────────────────────────

def hs_batch_upsert(hs_object, inputs):
    hs_headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }
    errors = []
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        url = f"https://api.hubapi.com/crm/v3/objects/{hs_object}/batch/upsert"
        try:
            resp = requests.post(url, headers=hs_headers,
                                 json={"inputs": batch}, timeout=20)
            if resp.status_code not in (200, 201, 207):
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                for err in resp.json().get("errors", []):
                    errors.append(err.get("message", "Unknown error"))
        except Exception as e:
            errors.append(str(e))
    return errors

def hs_batch_update(hs_object, inputs):
    hs_headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }
    errors = []
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        url = f"https://api.hubapi.com/crm/v3/objects/{hs_object}/batch/update"
        try:
            resp = requests.post(url, headers=hs_headers,
                                 json={"inputs": batch}, timeout=20)
            if resp.status_code not in (200, 201, 207):
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                for err in resp.json().get("errors", []):
                    errors.append(err.get("message", "Unknown error"))
        except Exception as e:
            errors.append(str(e))
    return errors

def hs_batch_create(hs_object, inputs):
    hs_headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }
    errors = []
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        url = f"https://api.hubapi.com/crm/v3/objects/{hs_object}/batch/create"
        try:
            resp = requests.post(url, headers=hs_headers,
                                 json={"inputs": batch}, timeout=20)
            if resp.status_code not in (200, 201, 207):
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            errors.append(str(e))
    return errors

# ── Slack ─────────────────────────────────────────────────────

def send_slack_message(channel_id, text):
    if not SLACK_BOT_TOKEN or not channel_id:
        return
    payload = json.dumps({"channel": channel_id, "text": text}).encode()
    req = Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        if not result.get("ok"):
            _log(f"[slack] error: {result.get('error')}")
    except Exception as e:
        _log(f"[slack] send error: {e}")

# ── Schedule check ────────────────────────────────────────────

def should_run_gsheet(automation):
    schedule_type = automation.get("gsheet_schedule_type", "interval")
    last_run      = automation.get("last_run")
    now_utc       = datetime.datetime.now(datetime.timezone.utc)
    est_now       = datetime.datetime.now(EST)

    if schedule_type == "interval":
        interval_min = int(automation.get("gsheet_interval_minutes", 60))
        if not last_run:
            return True
        try:
            lr = datetime.datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            return (now_utc - lr).total_seconds() / 60 >= interval_min
        except Exception:
            return True

    elif schedule_type == "daily":
        run_time = automation.get("gsheet_run_time", "08:00")
        try:
            h, m = [int(x) for x in run_time.split(":")]
        except Exception:
            h, m = 8, 0
        if (est_now.hour, est_now.minute) < (h, m):
            return False
        if last_run:
            try:
                lr = datetime.datetime.fromisoformat(
                    last_run.replace("Z", "+00:00")).astimezone(EST)
                if lr.date() == est_now.date():
                    return False
            except Exception:
                pass
        return True

    elif schedule_type == "weekly":
        run_day  = int(automation.get("gsheet_run_day", 0))
        run_time = automation.get("gsheet_run_time", "08:00")
        if est_now.weekday() != run_day:
            return False
        try:
            h, m = [int(x) for x in run_time.split(":")]
        except Exception:
            h, m = 8, 0
        if (est_now.hour, est_now.minute) < (h, m):
            return False
        if last_run:
            try:
                lr = datetime.datetime.fromisoformat(
                    last_run.replace("Z", "+00:00")).astimezone(EST)
                cal_lr  = lr.isocalendar()
                cal_now = est_now.isocalendar()
                if cal_lr[0] == cal_now[0] and cal_lr[1] == cal_now[1]:
                    return False
            except Exception:
                pass
        return True

    return True

# ── Main sync logic ───────────────────────────────────────────

def run_gsheet_sync(automation):
    auto_name        = automation.get("name", "?")
    sheet_url        = automation.get("sheet_url", "")
    sheet_tab        = automation.get("sheet_tab", "")
    object_type      = automation.get("object_type", "contact")
    pk_column        = automation.get("primary_key_column", "")
    pk_type          = automation.get("primary_key_type", "email")
    column_mappings  = automation.get("column_mappings", [])
    slack_channel    = automation.get("slack_channel", "")
    default_pipeline = automation.get("default_pipeline", "")
    default_stage    = automation.get("default_stage", "")

    hs_object = {
        "contact": "contacts",
        "company": "companies",
        "deal":    "deals"
    }.get(object_type, "contacts")

    # Read Google Sheet
    try:
        gtoken   = get_google_token()
        sheet_id = extract_sheet_id(sheet_url)
        rows     = get_sheet_data(sheet_id, sheet_tab, gtoken)
    except Exception as e:
        msg = f"⚠️ *{auto_name}* — Could not read Google Sheet: {e}"
        _log(f"[gsheet] {msg}")
        if slack_channel and automation.get("slack_enabled"):
            send_slack_message(slack_channel, msg)
        return

    if not rows or len(rows) < 2:
        _log(f"[gsheet] {auto_name}: sheet empty or header-only, skipping")
        return

    headers   = [h.strip() for h in rows[0]]
    data_rows = rows[1:]

    if pk_column not in headers:
        msg = f"⚠️ *{auto_name}* — Primary key column '{pk_column}' not found. Headers: {headers}"
        _log(f"[gsheet] {msg}")
        if slack_channel and automation.get("slack_enabled"):
            send_slack_message(slack_channel, msg)
        return

    pk_idx = headers.index(pk_column)

    upsert_inputs = []
    create_inputs = []

    for row in data_rows:
        row_padded = list(row) + [''] * max(0, len(headers) - len(row))
        pk_value   = row_padded[pk_idx].strip() if pk_idx < len(row_padded) else ''

        # Build property dict from column mappings
        properties = {}
        for mapping in column_mappings:
            col  = mapping.get("column", "").strip()
            prop = mapping.get("property", "").strip()
            if not col or not prop or col not in headers:
                continue
            cidx = headers.index(col)
            val  = row_padded[cidx].strip() if cidx < len(row_padded) else ''
            if val != '':
                properties[prop] = val

        if not properties:
            continue

        # Deals with no ID → create
        if object_type == "deal" and not pk_value:
            props = dict(properties)
            if "pipeline" not in props and default_pipeline:
                props["pipeline"] = default_pipeline
            if "dealstage" not in props and default_stage:
                props["dealstage"] = default_stage
            create_inputs.append({"properties": props})
            continue

        if not pk_value:
            continue

        if pk_type in ("email", "domain"):
            upsert_inputs.append({
                "id":         pk_value,
                "idProperty": pk_type,
                "properties": properties
            })
        else:
            upsert_inputs.append({
                "id":         pk_value,
                "properties": properties
            })

    _log(f"[gsheet] {auto_name}: {len(upsert_inputs)} upserts, "
         f"{len(create_inputs)} creates for {hs_object}")

    all_errors = []

    if upsert_inputs:
        if pk_type in ("email", "domain"):
            all_errors += hs_batch_upsert(hs_object, upsert_inputs)
        else:
            all_errors += hs_batch_update(hs_object, upsert_inputs)

    if create_inputs:
        all_errors += hs_batch_create(hs_object, create_inputs)

    if all_errors:
        _log(f"[gsheet] {auto_name}: {len(all_errors)} error(s): {all_errors[:3]}")
        if slack_channel and automation.get("slack_enabled"):
            snippet = "\n".join(f"• {e}" for e in all_errors[:10])
            send_slack_message(slack_channel,
                f"⚠️ *{auto_name}* — Sheet sync had {len(all_errors)} error(s):\n{snippet}")
    else:
        if slack_channel and automation.get("slack_enabled"):
            send_slack_message(slack_channel,
                f"✅ *{auto_name}* — Sheet sync complete. "
                f"{len(upsert_inputs)} upserted, {len(create_inputs)} created.")

    _log(f"[gsheet] {auto_name}: done. errors={len(all_errors)}")


# ── Handler ───────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        secret = self.headers.get("X-Sync-Secret", "")
        if SYNC_SECRET and secret != SYNC_SECRET:
            self._json(401, {"error": "Unauthorized"})
            return
        self._run_sync()

    def _run_sync(self):
        all_automations = get_automations()
        gsheet_automations = [
            a for a in all_automations
            if a.get("active") and a.get("delivery_type") == "gsheet_sync"
        ]

        _log(f"[gsheet_sync] running for {len(gsheet_automations)} active GSheet automations")

        ran = 0
        skipped = 0
        errors = 0

        for automation in gsheet_automations:
            auto_id = automation.get("id", "")

            if not should_run_gsheet(automation):
                _log(f"[gsheet_sync] {auto_id}: not scheduled yet, skip")
                skipped += 1
                continue

            _log(f"[gsheet_sync] running: {auto_id} — {automation.get('name', '')}")
            try:
                run_gsheet_sync(automation)
                ran += 1
            except Exception as e:
                _log(f"[gsheet_sync] {auto_id} error: {e}")
                errors += 1

            automation["last_run"] = datetime.datetime.utcnow().strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        save_automations(all_automations)

        result = {"ran": ran, "skipped": skipped, "errors": errors}
        _log(f"[gsheet_sync] done: {result}")
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
