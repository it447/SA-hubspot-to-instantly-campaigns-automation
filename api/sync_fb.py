import json
import os
import sys
import hashlib
import time
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import quote

UPSTASH_URL     = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN   = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SYNC_SECRET     = os.environ.get("SYNC_SECRET", "")

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

def _redis_set_raw(key, value):
    url = f"{UPSTASH_URL}/set/{key}/{value}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        r.read()

def get_automations():
    data = _redis_get("automations_config")
    return data if isinstance(data, list) else []

# ── Dedup ─────────────────────────────────────────────────────

def load_sent_cache(auto_id):
    """Load all sent keys for this automation into a set in one scan."""
    sent   = set()
    cursor = 0
    pattern = f"fb:{auto_id}:*"
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
        _log(f"[fb_sync] sent cache load error: {e}")
        return None
    _log(f"[fb_sync] sent cache loaded: {len(sent)} keys for {auto_id}")
    return sent

def already_sent(auto_id, email, sent_cache=None):
    key = f"fb:{auto_id}:{email.lower()}"
    if sent_cache is not None:
        return key in sent_cache
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    return result.get("result") is not None

def mark_sent(auto_id, email, sent_cache=None):
    key = f"fb:{auto_id}:{email.lower()}"
    _redis_set_raw(key, 1)
    if sent_cache is not None:
        sent_cache.add(key)

def log_enrollment(auto_id, email):
    key   = f"logs:{auto_id}"
    entry = json.dumps({"email": email, "ts": time.time(), "type": "fb_conversions"})
    encoded = quote(entry, safe="")
    url = f"{UPSTASH_URL}/lpush/{key}/{encoded}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        resp_body = r.read()
    _log(f"[fb_sync] log_enrollment redis response: {resp_body}")

# ── Last run timestamp ────────────────────────────────────────

def get_last_run(auto_id):
    key = f"fb_last_run:{auto_id}"
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    val = result.get("result")
    return float(val) if val else None

def set_last_run(auto_id, ts):
    key = f"fb_last_run:{auto_id}"
    _redis_set_raw(key, ts)

def update_automation_last_run(auto_id):
    """Update last_run on the automation config so the dashboard shows it."""
    try:
        data = _redis_get("automations_config")
        automations = data if isinstance(data, list) else []
        for a in automations:
            if a.get("id") == auto_id:
                import datetime
                a["last_run"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        url  = f"{UPSTASH_URL}/set/automations_config"
        body = json.dumps(automations).encode()
        req  = Request(url, data=body, headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type":  "application/json"
        }, method="POST")
        with urlopen(req, timeout=5) as r:
            r.read()
    except Exception as e:
        _log(f"[fb_sync] update_automation_last_run error: {e}")

# ── HubSpot ───────────────────────────────────────────────────

def get_new_list_contacts(list_id, since_ts, extra_properties=None):
    """Fetch all contacts in the list — dedup keys handle skipping already-sent ones."""
    headers    = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    contacts   = []
    vid_offset = None

    base_props = ["email", "firstname", "lastname"]
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
            _log(f"[fb_sync] HubSpot list {list_id} fetch error: {e}")
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

    _log(f"[fb_sync] list {list_id} has {len(contacts)} contacts")
    return contacts

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

# ── Facebook Conversions API ──────────────────────────────────

# Fields that should be SHA256 hashed (lowercase, stripped)
_HASH_FIELDS = {"em", "ph", "fn", "ln", "ct", "st", "country", "zp"}

# Facebook field name → user_data key mapping
_FB_FIELD_MAP = {
    "email":       "em",
    "phone":       "ph",
    "first_name":  "fn",
    "last_name":   "ln",
    "city":        "ct",
    "state":       "st",
    "country":     "country",
    "zip":         "zp",
    "external_id": "external_id",
    "fbc":         "fbc",
}

def _sha256(val):
    return hashlib.sha256(val.strip().lower().encode()).hexdigest()

def build_user_data(contact, fb_field_mappings, ts):
    user_data = {}
    for mapping in fb_field_mappings:
        fb_field  = mapping.get("fb_field", "")
        hs_prop   = mapping.get("hs_property", "")
        if not fb_field or not hs_prop:
            continue
        raw_val = contact.get(hs_prop, "").strip()
        if not raw_val:
            continue
        ud_key = _FB_FIELD_MAP.get(fb_field)
        if not ud_key:
            continue

        if fb_field == "fbc":
            # Extract fbclid from hs_analytics_first_url or raw value
            fbclid = ""
            if "fbclid=" in raw_val:
                fbclid = raw_val.split("fbclid=")[1].split("&")[0]
            else:
                fbclid = raw_val
            if fbclid:
                user_data["fbc"] = f"fb.1.{int(ts)}.{fbclid}"
        elif ud_key in _HASH_FIELDS:
            user_data[ud_key] = _sha256(raw_val)
        else:
            user_data[ud_key] = raw_val

    return user_data

def push_fb_event(pixel_id, access_token, event_name, email, user_data, ts, event_source_url=""):
    event_id = f"{email}_{int(ts)}"
    event = {
        "event_name":    event_name,
        "event_time":    int(ts),
        "event_id":      event_id,
        "action_source": "website",
        "user_data":     user_data,
    }
    if event_source_url:
        event["event_source_url"] = event_source_url
    payload = {"data": [event]}
    url = f"https://graph.facebook.com/v19.0/{pixel_id}/events?access_token={access_token}"
    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
    if resp.status_code not in (200, 201):
        raise Exception(f"FB Conversions API error {resp.status_code}: {resp.text[:200]}")

def run_fb_sync(automation):
    auto_id       = automation.get("id", "")
    auto_name     = automation.get("name", auto_id)
    list_id       = automation.get("hubspot_list_id", "")
    pixel_id      = automation.get("fb_pixel_id", "")
    access_token  = automation.get("fb_access_token", "")
    event_name    = automation.get("fb_event_name", "Lead")
    fb_mappings   = automation.get("fb_field_mappings", [])
    slack_channel = automation.get("slack_channel", "")

    if not pixel_id or not access_token:
        _log(f"[fb_sync] {auto_name}: missing pixel_id or access_token, skipping")
        return

    extra_props = list({m.get("hs_property", "") for m in fb_mappings if m.get("hs_property")} | {"hs_analytics_first_url", "phone"})
    last_run    = get_last_run(auto_id)
    contacts    = get_new_list_contacts(list_id, since_ts=last_run, extra_properties=extra_props)

    sent_cache = load_sent_cache(auto_id)
    sent = skipped = errors = 0
    ts = time.time()

    for contact in contacts:
        email = contact["email"]

        if already_sent(auto_id, email, sent_cache):
            skipped += 1
            continue

        user_data = build_user_data(contact, fb_mappings, ts)
        if not user_data:
            _log(f"[fb_sync] {auto_name}: no user_data for {email}, skipping")
            skipped += 1
            continue

        # Extract event_source_url from first landing page property if present
        event_source_url = ""
        first_url = contact.get("hs_analytics_first_url", "").strip()
        if first_url:
            # Strip fbclid and other tracking params for cleaner URL
            event_source_url = first_url.split("?fbclid=")[0].split("&fbclid=")[0]

        try:
            push_fb_event(pixel_id, access_token, event_name, email, user_data, ts, event_source_url)
            mark_sent(auto_id, email, sent_cache)
            sent += 1
            _log(f"[fb_sync] {auto_name}: sent event for {email}")
        except Exception as e:
            _log(f"[fb_sync] {auto_name}: error sending {email}: {e}")
            errors += 1
            continue

        try:
            log_enrollment(auto_id, email)
        except Exception as e:
            _log(f"[fb_sync] {auto_name}: log_enrollment error for {email}: {e}")

    _log(f"[fb_sync] {auto_name}: done. sent={sent} skipped={skipped} errors={errors}")

    # Always update last_run so next run only checks new contacts
    set_last_run(auto_id, ts)
    update_automation_last_run(auto_id)

    if errors > 0 and slack_channel and automation.get("slack_enabled"):
        send_slack_message(slack_channel,
            f"⚠️ *{auto_name}* — FB Conversions sync had {errors} error(s). Check Vercel logs.")

    if sent > 0 and slack_channel and automation.get("slack_enabled"):
        msg = automation.get("slack_message", f"✅ *{{name}}* — sent {{pushed}} FB Conversion event(s).")
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
        fb_automations = [
            a for a in all_automations
            if a.get("active")
            and a.get("delivery_type") == "fb_conversions"
        ]

        _log(f"[fb_sync] found {len(fb_automations)} active FB Conversions automations")

        if not fb_automations:
            self._json(200, {"ok": True, "processed": 0})
            return

        processed = 0

        for automation in fb_automations:
            auto_id   = automation.get("id", "")
            auto_name = automation.get("name", auto_id)
            _log(f"[fb_sync] running: {auto_name} ({auto_id})")
            try:
                run_fb_sync(automation)
                processed += 1
            except Exception as e:
                _log(f"[fb_sync] error in {auto_name}: {e}")

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
