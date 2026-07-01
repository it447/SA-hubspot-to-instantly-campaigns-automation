import json
import os
import sys
import time
import datetime
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import quote, urlencode

UPSTASH_URL     = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN   = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SYNC_SECRET     = os.environ.get("SYNC_SECRET", "")
GOOGLE_OAUTH_CLIENT_ID     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

# ── Redis ─────────────────────────────────────────────────────

def _redis_get(key):
    # Use pipeline GET so key encoding matches pipeline SET
    from urllib.parse import quote as _quote
    pipeline = [["GET", key]]
    body = json.dumps(pipeline).encode()
    req = Request(f"{UPSTASH_URL}/pipeline", data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type":  "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        results = json.loads(r.read())
    val = results[0].get("result") if results else None
    return json.loads(val) if val else None

def _redis_set_raw(key, value):
    # Use pipeline to guarantee SET works and keep gcal_connected_emails in sync
    email = key.replace("gcal_token:", "") if key.startswith("gcal_token:") else None
    pipeline = [["SET", key, value]]
    if email:
        pipeline.append(["SADD", "gcal_connected_emails", email])
    body = json.dumps(pipeline).encode()
    req  = Request(f"{UPSTASH_URL}/pipeline", data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type":  "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()

def _redis_set_json(key, value):
    url  = f"{UPSTASH_URL}/set/{key}"
    body = json.dumps(value).encode()
    req  = Request(url, data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type":  "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()

def get_automations():
    data = _redis_get("automations_config")
    return data if isinstance(data, list) else []

def update_automation_last_run(auto_id):
    try:
        data = _redis_get("automations_config")
        automations = data if isinstance(data, list) else []
        for a in automations:
            if a.get("id") == auto_id:
                a["last_run"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _redis_set_json("automations_config", automations)
    except Exception as e:
        _log(f"[gcal_sync] update_automation_last_run error: {e}")

# ── Dedup & logs ──────────────────────────────────────────────

def load_sent_cache(auto_id):
    sent   = set()
    cursor = 0
    pattern = f"gcal:{auto_id}:*"
    try:
        while True:
            url = f"{UPSTASH_URL}/scan/{cursor}?match={pattern}&count=500"
            req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
            with urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            result = data.get("result", [0, []])
            cursor = int(result[0])
            for key in (result[1] if len(result) > 1 else []):
                sent.add(key)
            if cursor == 0:
                break
    except Exception as e:
        _log(f"[gcal_sync] sent cache load error: {e}")
        return None
    _log(f"[gcal_sync] sent cache loaded: {len(sent)} keys for {auto_id}")
    return sent

def already_sent(auto_id, email, sent_cache=None):
    key = f"gcal:{auto_id}:{email.lower()}"
    if sent_cache is not None:
        return key in sent_cache
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    return result.get("result") is not None

def mark_sent(auto_id, email, sent_cache=None):
    _redis_set_raw(f"gcal:{auto_id}:{email.lower()}", 1)
    if sent_cache is not None:
        sent_cache.add(f"gcal:{auto_id}:{email.lower()}")

def log_enrollment(auto_id, email):
    key     = f"logs:{auto_id}"
    entry   = json.dumps({"email": email, "ts": time.time(), "type": "gcal"})
    encoded = quote(entry, safe="")
    url     = f"{UPSTASH_URL}/lpush/{key}/{encoded}"
    req     = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        r.read()

# ── OAuth token management ────────────────────────────────────

def get_token(email):
    return _redis_get(f"gcal_token:{email}")

def refresh_token(token_record):
    refresh = token_record.get("refresh_token", "")
    if not refresh:
        raise Exception("No refresh token stored — re-connect the Google account")
    data = urlencode({
        "client_id":     GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "refresh_token": refresh,
        "grant_type":    "refresh_token",
    }).encode()
    req = Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    with urlopen(req, timeout=10) as r:
        tokens = json.loads(r.read())
    if "error" in tokens:
        raise Exception(f"Token refresh failed: {tokens.get('error_description', tokens['error'])}")
    token_record["access_token"] = tokens["access_token"]
    token_record["expiry"]       = time.time() + tokens.get("expires_in", 3600)
    email = token_record.get("email", "")
    if email:
        key     = f"gcal_token:{email}"
        encoded = quote(json.dumps(token_record), safe="")
        _redis_set_raw(key, encoded)
    return token_record

def get_valid_access_token(email):
    record = get_token(email)
    if not record:
        raise Exception(f"No OAuth token found for {email} — connect the account first")
    if time.time() >= record.get("expiry", 0) - 60:
        record = refresh_token(record)
    return record["access_token"]

# ── HubSpot ───────────────────────────────────────────────────

def get_list_contacts(list_id, extra_properties=None):
    headers    = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    contacts   = []
    vid_offset = None
    base_props = ["email", "firstname", "lastname", "company", "phone"]
    all_props  = base_props + [p for p in (extra_properties or []) if p not in base_props]
    prop_str   = "&".join(f"property={p}" for p in all_props)

    while True:
        url = f"https://api.hubapi.com/contacts/v1/lists/{list_id}/contacts/all?count=100&{prop_str}"
        if vid_offset:
            url += f"&vidOffset={vid_offset}"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            _log(f"[gcal_sync] HubSpot list {list_id} fetch error: {e}")
            break

        for c in body.get("contacts", []):
            props = c.get("properties", {})
            email = props.get("email", {}).get("value", "").strip().lower()
            if not email:
                continue
            contact = {"email": email}
            for p in all_props:
                contact[p] = props.get(p, {}).get("value", "")
            contacts.append(contact)

        if not body.get("has-more", False):
            break
        vid_offset = body.get("vid-offset")

    _log(f"[gcal_sync] list {list_id}: {len(contacts)} contacts")
    return contacts

# ── Placeholder substitution ──────────────────────────────────

def fill_placeholders(text, contact):
    replacements = {
        "{{firstname}}":  contact.get("firstname", ""),
        "{{lastname}}":   contact.get("lastname",  ""),
        "{{email}}":      contact.get("email",     ""),
        "{{company}}":    contact.get("company",   ""),
        "{{phone}}":      contact.get("phone",     ""),
        "{{name}}":       f"{contact.get('firstname','')} {contact.get('lastname','')}".strip(),
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text

# ── Event timing ──────────────────────────────────────────────

def compute_event_times(automation, contact):
    timing_type = automation.get("timing_type", "relative")
    duration    = int(automation.get("duration_minutes", 30))
    tz_offset   = 0  # always UTC for now

    if timing_type == "fixed":
        dt_str = automation.get("timing_fixed_datetime", "")
        if not dt_str:
            raise Exception("Fixed datetime not set")
        start_dt = datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))

    elif timing_type == "relative":
        days    = int(automation.get("timing_relative_days", 1))
        t_str   = automation.get("timing_relative_time", "09:00")
        hour, minute = map(int, t_str.split(":"))
        now     = datetime.datetime.utcnow()
        start_dt = (now + datetime.timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0,
            tzinfo=datetime.timezone.utc
        )

    elif timing_type == "hs_property":
        prop    = automation.get("timing_hs_property", "")
        val     = contact.get(prop, "")
        t_str   = automation.get("timing_relative_time", "09:00")
        hour, minute = map(int, t_str.split(":"))
        if not val:
            raise Exception(f"HubSpot property '{prop}' is empty for {contact['email']}")
        try:
            # HubSpot stores date properties as milliseconds epoch or YYYY-MM-DD
            if val.isdigit():
                start_dt = datetime.datetime.fromtimestamp(int(val)/1000, tz=datetime.timezone.utc)
            else:
                start_dt = datetime.datetime.fromisoformat(val).replace(tzinfo=datetime.timezone.utc)
            start_dt = start_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except Exception:
            raise Exception(f"Could not parse date property '{prop}' value: {val}")
    else:
        raise Exception(f"Unknown timing_type: {timing_type}")

    end_dt = start_dt + datetime.timedelta(minutes=duration)
    fmt    = "%Y-%m-%dT%H:%M:%S"
    return start_dt.strftime(fmt), end_dt.strftime(fmt)

# ── Google Calendar ───────────────────────────────────────────

def create_calendar_event(access_token, organizer_email, contact_email, title, description, start_dt, end_dt, google_meet=False):
    event = {
        "summary":     title,
        "description": description,
        "start":       {"dateTime": start_dt, "timeZone": "UTC"},
        "end":         {"dateTime": end_dt,   "timeZone": "UTC"},
        "attendees":   [{"email": contact_email}],
        "sendUpdates": "all",
    }
    if google_meet:
        event["conferenceData"] = {
            "createRequest": {
                "requestId":             f"{contact_email}_{int(time.time())}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    url = f"https://www.googleapis.com/calendar/v3/calendars/{organizer_email}/events"
    if google_meet:
        url += "?conferenceDataVersion=1"

    resp = requests.post(url, json=event, headers={
        "Authorization":  f"Bearer {access_token}",
        "Content-Type":   "application/json",
    }, timeout=15)

    if resp.status_code not in (200, 201):
        raise Exception(f"Calendar API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()

# ── Slack ─────────────────────────────────────────────────────

def send_slack_message(channel_id, text):
    if not SLACK_BOT_TOKEN or not channel_id:
        return
    payload = json.dumps({"channel": channel_id, "text": text}).encode()
    req = Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        if not result.get("ok"):
            _log(f"[slack] error: {result.get('error')}")
    except Exception as e:
        _log(f"[slack] send error: {e}")

# ── Main sync ─────────────────────────────────────────────────

def run_gcal_sync(automation):
    auto_id       = automation.get("id", "")
    auto_name     = automation.get("name", auto_id)
    list_id       = automation.get("hubspot_list_id", "")
    send_from     = automation.get("send_from_email", "").strip()
    title_tpl     = automation.get("meeting_title", "Meeting")
    desc_tpl      = automation.get("meeting_description", "")
    google_meet   = automation.get("google_meet", False)
    slack_channel = automation.get("slack_channel", "")
    timing_type   = automation.get("timing_type", "relative")

    if not send_from:
        _log(f"[gcal_sync] {auto_name}: no send_from_email, skipping")
        return

    # Extra HubSpot properties needed for hs_property timing
    extra_props = []
    if timing_type == "hs_property":
        prop = automation.get("timing_hs_property", "")
        if prop:
            extra_props = [prop]

    try:
        access_token = get_valid_access_token(send_from)
    except Exception as e:
        _log(f"[gcal_sync] {auto_name}: token error: {e}")
        return

    contacts   = get_list_contacts(list_id, extra_properties=extra_props)
    sent_cache = load_sent_cache(auto_id)
    sent = skipped = errors = 0

    for contact in contacts:
        email = contact["email"]

        if already_sent(auto_id, email, sent_cache):
            skipped += 1
            continue

        title       = fill_placeholders(title_tpl, contact)
        description = fill_placeholders(desc_tpl, contact)

        try:
            start_dt, end_dt = compute_event_times(automation, contact)
        except Exception as e:
            _log(f"[gcal_sync] {auto_name}: timing error for {email}: {e}")
            skipped += 1
            continue

        try:
            create_calendar_event(access_token, send_from, email, title, description, start_dt, end_dt, google_meet)
            mark_sent(auto_id, email, sent_cache)
            sent += 1
            _log(f"[gcal_sync] {auto_name}: invite sent to {email}")
        except Exception as e:
            _log(f"[gcal_sync] {auto_name}: error for {email}: {e}")
            errors += 1
            continue

        try:
            log_enrollment(auto_id, email)
        except Exception as e:
            _log(f"[gcal_sync] {auto_name}: log_enrollment error for {email}: {e}")

    _log(f"[gcal_sync] {auto_name}: done. sent={sent} skipped={skipped} errors={errors}")
    update_automation_last_run(auto_id)

    if errors > 0 and slack_channel and automation.get("slack_enabled"):
        send_slack_message(slack_channel,
            f"⚠️ *{auto_name}* — Calendar sync had {errors} error(s). Check Vercel logs.")

    if sent > 0 and slack_channel and automation.get("slack_enabled"):
        msg = automation.get("slack_message", f"✅ *{{name}}* — sent {{pushed}} calendar invite(s).")
        msg = msg.replace("{{name}}", auto_name).replace("{{pushed}}", str(sent))
        send_slack_message(slack_channel, msg)

# ── Handler ───────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        secret = self.headers.get("X-Sync-Secret", "")
        if SYNC_SECRET and secret != SYNC_SECRET:
            self._json(401, {"error": "Unauthorized"})
            return
        self._run_sync()

    def do_POST(self):
        self.do_GET()

    def _run_sync(self):
        all_automations = get_automations()
        gcal_automations = [
            a for a in all_automations
            if a.get("active") and a.get("delivery_type") == "gcal"
        ]

        _log(f"[gcal_sync] found {len(gcal_automations)} active GCal automations")

        if not gcal_automations:
            self._json(200, {"ok": True, "processed": 0})
            return

        processed = 0
        for automation in gcal_automations:
            auto_id   = automation.get("id", "")
            auto_name = automation.get("name", auto_id)
            _log(f"[gcal_sync] running: {auto_name} ({auto_id})")
            try:
                run_gcal_sync(automation)
                processed += 1
            except Exception as e:
                _log(f"[gcal_sync] error in {auto_name}: {e}")

        self._json(200, {"ok": True, "processed": processed})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
