import json
import os
import sys
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

# ── Sent cache ────────────────────────────────────────────────

def load_sent_cache():
    sent   = set()
    cursor = 0
    try:
        while True:
            url = f"{UPSTASH_URL}/scan/{cursor}?match=sent:*&count=500"
            req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
            with urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            result = data.get("result", [0, []])
            cursor = int(result[0])
            keys   = result[1] if len(result) > 1 else []
            for k in keys:
                sent.add(k)
            if cursor == 0:
                break
    except Exception as e:
        _log(f"[clay_sync] sent cache load error (falling back to per-key reads): {e}")
        return None
    _log(f"[clay_sync] sent cache loaded: {len(sent)} keys")
    return sent

def already_sent_cached(email, target_id, sent_cache):
    key = f"sent:{email.lower()}:{target_id}"
    if sent_cache is None:
        url = f"{UPSTASH_URL}/get/{key}"
        req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
        with urlopen(req, timeout=5) as r:
            result = json.loads(r.read())
        return result.get("result") is not None
    return key in sent_cache

def mark_as_sent(email, target_id, sent_cache=None):
    key = f"sent:{email.lower()}:{target_id}"
    _redis_set_raw(key, 1)
    if sent_cache is not None:
        sent_cache.add(key)

def log_enrollment(auto_id, email):
    key   = f"logs:{auto_id}"
    entry = json.dumps({"email": email, "ts": __import__("time").time(), "type": "enrichment"})
    encoded = quote(entry, safe="")
    url = f"{UPSTASH_URL}/lpush/{key}/{encoded}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        r.read()

# ── HubSpot ───────────────────────────────────────────────────

def get_list_contacts(list_id, extra_properties=None):
    headers    = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    contacts   = []
    vid_offset = None

    base_props = ["email", "firstname", "lastname", "company", "company_domain"]
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
            _log(f"[clay_sync] HubSpot list {list_id} fetch error: {e}")
            break

        for c in body.get("contacts", []):
            props = c.get("properties", {})
            email = props.get("email", {}).get("value", "").strip().lower()
            if email:
                contact = {
                    "email":          email,
                    "firstname":      props.get("firstname",      {}).get("value", ""),
                    "lastname":       props.get("lastname",       {}).get("value", ""),
                    "company":        props.get("company",        {}).get("value", ""),
                    "company_domain": props.get("company_domain", {}).get("value", ""),
                }
                for p in (extra_properties or []):
                    contact[p] = props.get(p, {}).get("value", "")
                contacts.append(contact)

        if not body.get("has-more", False):
            break
        vid_offset = body.get("vid-offset")

    _log(f"[clay_sync] list {list_id} has {len(contacts)} contacts")
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

# ── Clay push ─────────────────────────────────────────────────

def push_to_clay(webhook_url, row_data):
    resp = requests.post(webhook_url, json=row_data, headers={"Content-Type": "application/json"}, timeout=15)
    if resp.status_code not in (200, 201, 202):
        raise Exception(f"Clay webhook error {resp.status_code}: {resp.text[:200]}")

def run_clay_push(automation, sent_cache):
    auto_id       = automation.get("id", "")
    auto_name     = automation.get("name", auto_id)
    list_id       = automation.get("hubspot_list_id", "")
    webhook_url   = automation.get("clay_webhook_url", "") or automation.get("clay_table_id", "")
    col_mappings  = automation.get("clay_column_mappings", [])
    slack_channel = automation.get("slack_channel", "")

    if not webhook_url:
        _log(f"[clay] {auto_name}: no webhook URL, skipping")
        return

    extra_props = [m.get("hs_property", "") for m in col_mappings if m.get("hs_property")]
    contacts    = get_list_contacts(list_id, extra_properties=extra_props)

    clay_key = f"clay:{auto_id}"
    pushed = skipped = errors = 0

    for contact in contacts:
        email = contact["email"]

        if already_sent_cached(email, clay_key, sent_cache):
            skipped += 1
            continue

        row_data = {}
        for mapping in col_mappings:
            hs_prop  = mapping.get("hs_property", "")
            clay_col = mapping.get("clay_column", "")
            if not hs_prop or not clay_col:
                continue
            val = contact.get(hs_prop, "")
            if val:
                row_data[clay_col] = val

        if not row_data:
            _log(f"[clay] {auto_name}: no data for {email}, skipping")
            skipped += 1
            continue

        try:
            push_to_clay(webhook_url, row_data)
            mark_as_sent(email, clay_key, sent_cache)
            log_enrollment(auto_id, email)
            pushed += 1
            _log(f"[clay] {auto_name}: pushed {email}")
        except Exception as e:
            _log(f"[clay] {auto_name}: error pushing {email}: {e}")
            errors += 1

    _log(f"[clay] {auto_name}: done. pushed={pushed} skipped={skipped} errors={errors}")

    if errors > 0 and slack_channel and automation.get("slack_enabled"):
        send_slack_message(slack_channel,
            f"⚠️ *{auto_name}* — Clay push had {errors} error(s). Check Vercel logs.")

    if pushed > 0 and slack_channel and automation.get("slack_enabled"):
        msg = automation.get("slack_message", f"✅ *{{name}}* — pushed {{pushed}} contact(s) to Clay.")
        msg = msg.replace("{{name}}", auto_name).replace("{{pushed}}", str(pushed))
        send_slack_message(slack_channel, msg)

# ── Handler ───────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        secret = self.headers.get("X-Sync-Secret", "")
        if SYNC_SECRET and secret != SYNC_SECRET:
            self._json(401, {"error": "Unauthorized"})
            return
        self._json(200, {"ok": True, "status": "sync started"})
        self._run_sync()

    def do_POST(self):
        self.do_GET()

    def _run_sync(self):
        all_automations = get_automations()
        clay_automations = [
            a for a in all_automations
            if a.get("active")
            and a.get("delivery_type") == "enrichment"
            and a.get("clay_enabled")
        ]

        _log(f"[clay_sync] found {len(clay_automations)} active Clay automations")

        if not clay_automations:
            return

        sent_cache = load_sent_cache()
        processed  = 0

        for automation in clay_automations:
            auto_id   = automation.get("id", "")
            auto_name = automation.get("name", auto_id)
            _log(f"[clay_sync] running: {auto_name} ({auto_id})")
            try:
                run_clay_push(automation, sent_cache)
                processed += 1
            except Exception as e:
                _log(f"[clay_sync] error in {auto_name}: {e}")

        _log(f"[clay_sync] done. processed={processed}")

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
