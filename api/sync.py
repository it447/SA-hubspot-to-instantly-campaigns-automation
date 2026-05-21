import json
import os
import sys
import time
import datetime
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import quote

UPSTASH_URL       = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN     = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
INSTANTLY_API_KEY = os.environ.get("INSTANTLY_API_KEY", "")
HUBSPOT_API_KEY   = os.environ.get("HUBSPOT_API_KEY", "")
HUBSPOT_PORTAL_ID = os.environ.get("HUBSPOT_PORTAL_ID", "22650739")
SYNC_SECRET       = os.environ.get("SYNC_SECRET", "")
SLACK_BOT_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "")
CLAY_API_KEY      = os.environ.get("CLAY_API_KEY", "")

EST = datetime.timezone(datetime.timedelta(hours=-5))

INSTANTLY_TERMINAL_STATUSES = {"completed", "unsubscribed", "bounced", "finished", "out_of_sequence"}

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

def _redis_set_json(key, value):
    url = f"{UPSTASH_URL}/set/{key}"
    body = json.dumps(value).encode()
    req = Request(url, data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()

def _redis_incr(key):
    url = f"{UPSTASH_URL}/incr/{key}"
    req = Request(url, data=b'', headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, method="POST")
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    return data.get("result", 0)

def _redis_expire(key, seconds):
    url = f"{UPSTASH_URL}/expire/{key}/{seconds}"
    req = Request(url, data=b'', headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()

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
    _redis_set_json("automations_config", automations)

# ── Sent cache ────────────────────────────────────────────────

def load_sent_cache():
    """Scan all sent: keys from Redis into a local set. One scan per run."""
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
        _log(f"[sync] sent cache load error (falling back to per-key reads): {e}")
        return None
    _log(f"[sync] sent cache loaded: {len(sent)} keys")
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

# ── First seen helpers ────────────────────────────────────────

def get_first_seen(email, target_id):
    key = f"first_seen:{email.lower()}:{target_id}"
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    val = result.get("result")
    return float(val) if val else None

def set_first_seen(email, target_id):
    _redis_set_raw(f"first_seen:{email.lower()}:{target_id}", time.time())

def log_enrollment(auto_id, email, delivery_type, ts):
    key     = f"logs:{auto_id}"
    entry   = json.dumps({"email": email, "ts": ts, "type": delivery_type})
    encoded = quote(entry, safe='')
    url     = f"{UPSTASH_URL}/lpush/{key}/{encoded}"
    req     = Request(url, data=b'', headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()
    trim_req = Request(f"{UPSTASH_URL}/ltrim/{key}/0/499", data=b'',
                       headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, method="POST")
    with urlopen(trim_req, timeout=5) as r:
        r.read()

def increment_enroll_count(auto_id, est_date):
    key   = f"enroll_count:{auto_id}:{est_date}"
    count = _redis_incr(key)
    if count == 1:
        _redis_expire(key, 30 * 86400)
    return count

def get_enroll_count(auto_id, est_date):
    return _redis_get_int(f"enroll_count:{auto_id}:{est_date}")

def get_weekly_enroll_count(auto_id, est_now):
    total = 0
    for i in range(7):
        day = est_now.date() - datetime.timedelta(days=i)
        total += get_enroll_count(auto_id, day.isoformat())
    return total

def alert_already_sent_today(auto_id, est_date):
    key = f"alert_sent:{auto_id}:{est_date}"
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    return data.get("result") is not None

def mark_alert_sent_today(auto_id, est_date):
    key = f"alert_sent:{auto_id}:{est_date}"
    _redis_set_raw(key, 1)
    _redis_expire(key, 2 * 86400)

def alert_already_sent_week(auto_id, iso_week):
    key = f"alert_sent_week:{auto_id}:{iso_week}"
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    return data.get("result") is not None

def mark_alert_sent_week(auto_id, iso_week):
    key = f"alert_sent_week:{auto_id}:{iso_week}"
    _redis_set_raw(key, 1)
    _redis_expire(key, 8 * 86400)

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

def send_enrollment_notification(automation, email, first_name, last_name, company):
    if not automation.get("slack_enabled"):
        return
    channel_id  = automation.get("slack_channel", "")
    message_tpl = automation.get("slack_message", "")
    if not channel_id or not message_tpl:
        return
    msg = message_tpl
    msg = msg.replace("{{email}}", email)
    msg = msg.replace("{{first_name}}", first_name)
    msg = msg.replace("{{last_name}}", last_name)
    msg = msg.replace("{{company}}", company)
    try:
        send_slack_message(channel_id, msg)
    except Exception as e:
        _log(f"[slack] enrollment notification error: {e}")

def check_and_send_alert(automation, auto_id, est_now, est_date):
    if not automation.get("alert_enabled"):
        return
    threshold   = int(automation.get("alert_threshold", 0))
    schedule    = automation.get("alert_schedule", "daily")
    alert_time  = automation.get("alert_time", "08:00")
    channel_id  = automation.get("alert_slack_channel", "")
    message_tpl = automation.get("alert_message", "")

    if not channel_id or not message_tpl:
        return

    try:
        alert_hour, alert_min = [int(x) for x in alert_time.split(":")]
    except Exception:
        alert_hour, alert_min = 8, 0

    if (est_now.hour, est_now.minute) < (alert_hour, alert_min):
        return

    if schedule == "daily":
        if alert_already_sent_today(auto_id, est_date):
            return
        count = get_enroll_count(auto_id, est_date)
        if count < threshold:
            msg = message_tpl
            msg = msg.replace("{{count}}", str(count))
            msg = msg.replace("{{automation_name}}", automation.get("name", auto_id))
            msg = msg.replace("{{threshold}}", str(threshold))
            msg = msg.replace("{{date}}", est_date)
            send_slack_message(channel_id, msg)
            mark_alert_sent_today(auto_id, est_date)
            _log(f"[alert] daily alert sent for {auto_id}: {count} < {threshold}")

    elif schedule == "weekly":
        alert_day = int(automation.get("alert_day", 0))
        if est_now.weekday() != alert_day:
            return
        iso_week = est_now.strftime("%Y-W%W")
        if alert_already_sent_week(auto_id, iso_week):
            return
        count = get_weekly_enroll_count(auto_id, est_now)
        if count < threshold:
            msg = message_tpl
            msg = msg.replace("{{count}}", str(count))
            msg = msg.replace("{{automation_name}}", automation.get("name", auto_id))
            msg = msg.replace("{{threshold}}", str(threshold))
            msg = msg.replace("{{date}}", est_date)
            send_slack_message(channel_id, msg)
            mark_alert_sent_week(auto_id, iso_week)
            _log(f"[alert] weekly alert sent for {auto_id}: {count} < {threshold}")

# ── HubSpot list contacts ─────────────────────────────────────

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
            _log(f"[sync] HubSpot list {list_id} fetch error: {e}")
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

    _log(f"[sync] list {list_id} has {len(contacts)} contacts")
    return contacts

def check_filters(contact, filters):
    if not filters:
        return True
    for f in filters:
        prop        = f.get("property", "")
        operator    = f.get("operator", "equals")
        value       = str(f.get("value", "")).strip().lower()
        contact_val = str(contact.get(prop, "") or "").strip().lower()
        if operator == "exists":
            if not contact_val:
                return False
        elif operator == "equals":
            if contact_val != value:
                return False
        elif operator == "not_equals":
            if contact_val == value:
                return False
        elif operator == "contains":
            if value not in contact_val:
                return False
        elif operator == "not_contains":
            if value in contact_val:
                return False
    return True

# ── Instantly helpers ─────────────────────────────────────────

def add_to_instantly(email, first_name, last_name, company, campaign_id):
    headers = {
        "Authorization": f"Bearer {INSTANTLY_API_KEY}",
        "Content-Type":  "application/json"
    }
    resp = requests.post("https://api.instantly.ai/api/v2/leads/add", headers=headers, json={
        "campaign_id": campaign_id,
        "leads": [{"email": email, "first_name": first_name, "last_name": last_name, "company_name": company}],
    }, timeout=10)
    _log(f"[sync] Instantly add {email} status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()

def get_instantly_lead(email, campaign_id):
    url     = f"https://api.instantly.ai/api/v2/leads?campaign_id={campaign_id}&email={email}&limit=1"
    headers = {"Authorization": f"Bearer {INSTANTLY_API_KEY}"}
    try:
        resp  = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data  = resp.json()
        items = data if isinstance(data, list) else data.get("items", data.get("leads", []))
        for lead in items:
            if lead.get("email", "").lower() == email.lower():
                return lead
        return None
    except Exception as e:
        _log(f"[sync] get_instantly_lead error for {email}: {e}")
        return None

def unenroll_from_instantly(lead_id):
    url     = f"https://api.instantly.ai/api/v2/leads/{lead_id}"
    headers = {"Authorization": f"Bearer {INSTANTLY_API_KEY}"}
    resp    = requests.delete(url, headers=headers, timeout=10)
    _log(f"[sync] unenroll lead {lead_id} status={resp.status_code}")
    resp.raise_for_status()

def submit_hs_form(email, first_name, last_name, company, form_id):
    url  = f"https://api.hsforms.com/submissions/v3/integration/submit/{HUBSPOT_PORTAL_ID}/{form_id}"
    resp = requests.post(url, json={
        "fields": [
            {"name": "email",     "value": email},
            {"name": "firstname", "value": first_name},
            {"name": "lastname",  "value": last_name},
            {"name": "company",   "value": company},
        ]
    }, timeout=10)
    _log(f"[sync] HS form {form_id} submit {email} status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()

# ── Clay helpers ──────────────────────────────────────────────

def push_to_clay(table_id, row_data):
    """Push a single row to a Clay table."""
    headers = {
        "Authorization": f"Bearer {CLAY_API_KEY}",
        "Content-Type":  "application/json"
    }
    url  = f"https://api.clay.com/v1/sources/{table_id}/rows"
    resp = requests.post(url, headers=headers, json={"data": row_data}, timeout=15)
    if resp.status_code not in (200, 201):
        raise Exception(f"Clay API error {resp.status_code}: {resp.text[:200]}")
    return resp.json()

def run_clay_push(automation, contacts, sent_cache, auto_id):
    """Push unsent contacts from HubSpot list to Clay table."""
    table_id       = automation.get("clay_table_id", "")
    col_mappings   = automation.get("clay_column_mappings", [])
    slack_channel  = automation.get("slack_channel", "")
    auto_name      = automation.get("name", auto_id)

    if not table_id:
        _log(f"[clay] {auto_name}: no table_id configured, skipping")
        return 0, 0

    pushed   = 0
    skipped  = 0
    errors   = 0
    clay_key = f"clay:{auto_id}"  # separate sent key namespace for Clay

    for contact in contacts:
        email = contact["email"]

        # Check if already pushed to this Clay table
        if already_sent_cached(email, clay_key, sent_cache):
            skipped += 1
            continue

        # Build row data from column mappings
        row_data = {}
        for mapping in col_mappings:
            hs_prop    = mapping.get("hs_property", "")
            clay_col   = mapping.get("clay_column", "")
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
            push_to_clay(table_id, row_data)
            mark_as_sent(email, clay_key, sent_cache)
            pushed += 1
            _log(f"[clay] {auto_name}: pushed {email}")
        except Exception as e:
            _log(f"[clay] {auto_name}: error pushing {email}: {e}")
            errors += 1

    _log(f"[clay] {auto_name}: done. pushed={pushed} skipped={skipped} errors={errors}")

    if errors > 0 and slack_channel and automation.get("slack_enabled"):
        send_slack_message(slack_channel,
            f"⚠️ *{auto_name}* — Clay push had {errors} error(s). Check Vercel logs.")

    return pushed, errors

# ── GSheet pull helpers ───────────────────────────────────────

def get_google_token():
    import google.oauth2.service_account
    import google.auth.transport.requests as google_requests

    # Check Redis cache first
    cached = _redis_get("gsheet_token_cache")
    if cached and cached.get("token") and cached.get("expires_at"):
        try:
            expires_at = datetime.datetime.fromisoformat(cached["expires_at"])
            now        = datetime.datetime.now(datetime.timezone.utc)
            if (expires_at - now).total_seconds() > 300:
                return cached["token"]
        except Exception:
            pass

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    if not sa_json or sa_json == "{}":
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not configured")

    creds_data = json.loads(sa_json)
    creds      = google.oauth2.service_account.Credentials.from_service_account_info(
        creds_data, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    creds.refresh(google_requests.Request())

    expires_at = (datetime.datetime.now(datetime.timezone.utc) +
                  datetime.timedelta(minutes=55)).isoformat()
    try:
        _redis_set_json("gsheet_token_cache", {"token": creds.token, "expires_at": expires_at})
    except Exception:
        pass

    return creds.token

def extract_sheet_id(url):
    import re
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
    if not match:
        raise ValueError(f"Invalid Google Sheet URL: {url}")
    return match.group(1)

def get_sheet_data(sheet_id, tab_name, token):
    from urllib.parse import quote as url_quote
    range_name = url_quote(f"'{tab_name}'", safe='') if tab_name else "A1:ZZ"
    url  = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("values", [])

def hs_batch_upsert(hs_object, inputs):
    hs_headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    errors = []
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        url   = f"https://api.hubapi.com/crm/v3/objects/{hs_object}/batch/upsert"
        try:
            resp = requests.post(url, headers=hs_headers, json={"inputs": batch}, timeout=20)
            if resp.status_code not in (200, 201, 207):
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                for err in resp.json().get("errors", []):
                    errors.append(err.get("message", "Unknown error"))
        except Exception as e:
            errors.append(str(e))
    return errors

def hs_batch_update(hs_object, inputs):
    hs_headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    errors = []
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        url   = f"https://api.hubapi.com/crm/v3/objects/{hs_object}/batch/update"
        try:
            resp = requests.post(url, headers=hs_headers, json={"inputs": batch}, timeout=20)
            if resp.status_code not in (200, 201, 207):
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                for err in resp.json().get("errors", []):
                    errors.append(err.get("message", "Unknown error"))
        except Exception as e:
            errors.append(str(e))
    return errors

def should_run_gsheet(automation):
    """Check if the GSheet pull is due to run based on its schedule."""
    schedule_type    = automation.get("gsheet_schedule_type", "interval")
    last_gsheet_run  = automation.get("last_gsheet_run")  # separate from last_run
    now_utc          = datetime.datetime.now(datetime.timezone.utc)
    est_now          = datetime.datetime.now(EST)

    if schedule_type == "interval":
        interval_min = int(automation.get("gsheet_interval_minutes", 60))
        if not last_gsheet_run:
            return True
        try:
            lr = datetime.datetime.fromisoformat(last_gsheet_run.replace("Z", "+00:00"))
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
        if last_gsheet_run:
            try:
                lr = datetime.datetime.fromisoformat(
                    last_gsheet_run.replace("Z", "+00:00")).astimezone(EST)
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
        if last_gsheet_run:
            try:
                lr      = datetime.datetime.fromisoformat(
                    last_gsheet_run.replace("Z", "+00:00")).astimezone(EST)
                cal_lr  = lr.isocalendar()
                cal_now = est_now.isocalendar()
                if cal_lr[0] == cal_now[0] and cal_lr[1] == cal_now[1]:
                    return False
            except Exception:
                pass
        return True

    return True

def run_gsheet_pull(automation):
    """Pull enriched data from GSheet and upsert into HubSpot."""
    auto_name       = automation.get("name", "?")
    sheet_url       = automation.get("sheet_url", "")
    sheet_tab       = automation.get("sheet_tab", "")
    object_type     = automation.get("object_type", "contact")
    pk_column       = automation.get("primary_key_column", "")
    pk_type         = automation.get("primary_key_type", "email")
    column_mappings = automation.get("column_mappings", [])
    slack_channel   = automation.get("slack_channel", "")

    hs_object = {
        "contact": "contacts",
        "company": "companies",
        "deal":    "deals"
    }.get(object_type, "contacts")

    try:
        gtoken   = get_google_token()
        sheet_id = extract_sheet_id(sheet_url)
        rows     = get_sheet_data(sheet_id, sheet_tab, gtoken)
    except Exception as e:
        msg = f"⚠️ *{auto_name}* — Could not read Google Sheet: {e}"
        _log(f"[enrich/gsheet] {msg}")
        if slack_channel and automation.get("slack_enabled"):
            send_slack_message(slack_channel, msg)
        return

    if not rows or len(rows) < 2:
        _log(f"[enrich/gsheet] {auto_name}: sheet empty or header-only, skipping")
        return

    headers   = [h.strip() for h in rows[0]]
    data_rows = rows[1:]

    if pk_column not in headers:
        msg = f"⚠️ *{auto_name}* — Primary key column '{pk_column}' not found. Headers: {headers}"
        _log(f"[enrich/gsheet] {msg}")
        if slack_channel and automation.get("slack_enabled"):
            send_slack_message(slack_channel, msg)
        return

    pk_idx        = headers.index(pk_column)
    upsert_inputs = []

    for row in data_rows:
        row_padded = list(row) + [''] * max(0, len(headers) - len(row))
        pk_value   = row_padded[pk_idx].strip() if pk_idx < len(row_padded) else ''
        if not pk_value:
            continue

        properties = {}
        for mapping in column_mappings:
            col  = mapping.get("column", "").strip()
            prop = mapping.get("property", "").strip()
            if not col or not prop or col not in headers:
                continue
            cidx = headers.index(col)
            val  = row_padded[cidx].strip() if cidx < len(row_padded) else ''
            if val:
                properties[prop] = val

        if not properties:
            continue

        if pk_type in ("email", "domain"):
            upsert_inputs.append({
                "id":         pk_value,
                "idProperty": pk_type,
                "properties": properties
            })
        else:
            upsert_inputs.append({"id": pk_value, "properties": properties})

    _log(f"[enrich/gsheet] {auto_name}: {len(upsert_inputs)} upserts for {hs_object}")

    all_errors = []
    if upsert_inputs:
        if pk_type in ("email", "domain"):
            all_errors += hs_batch_upsert(hs_object, upsert_inputs)
        else:
            all_errors += hs_batch_update(hs_object, upsert_inputs)

    if all_errors:
        _log(f"[enrich/gsheet] {auto_name}: {len(all_errors)} error(s): {all_errors[:3]}")
        if slack_channel and automation.get("slack_enabled"):
            snippet = "\n".join(f"• {e}" for e in all_errors[:10])
            send_slack_message(slack_channel,
                f"⚠️ *{auto_name}* — GSheet pull had {len(all_errors)} error(s):\n{snippet}")
    else:
        _log(f"[enrich/gsheet] {auto_name}: done. errors=0")
        if slack_channel and automation.get("slack_enabled"):
            send_slack_message(slack_channel,
                f"✅ *{auto_name}* — GSheet pull complete. {len(upsert_inputs)} records updated in HubSpot.")

# ── Enrichment automation runner ──────────────────────────────

def run_enrichment(automation, sent_cache):
    """Run Clay push and/or GSheet pull depending on which toggles are enabled."""
    auto_id   = automation.get("id", "")
    auto_name = automation.get("name", auto_id)
    list_id   = automation.get("hubspot_list_id", "")

    clay_enabled   = automation.get("clay_enabled", False)
    gsheet_enabled = automation.get("enrichment_gsheet_enabled", False)

    # ── Step 1: Clay push ─────────────────────────────────────
    if clay_enabled:
        _log(f"[enrich] {auto_name}: running Clay push for list {list_id}")
        try:
            contacts = get_list_contacts(list_id)
            run_clay_push(automation, contacts, sent_cache, auto_id)
        except Exception as e:
            _log(f"[enrich] {auto_name}: Clay push error: {e}")
    else:
        _log(f"[enrich] {auto_name}: Clay push disabled, skipping")

    # ── Step 2: GSheet pull ───────────────────────────────────
    if gsheet_enabled:
        if should_run_gsheet(automation):
            _log(f"[enrich] {auto_name}: running GSheet pull")
            try:
                run_gsheet_pull(automation)
                automation["last_gsheet_run"] = datetime.datetime.utcnow().strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            except Exception as e:
                _log(f"[enrich] {auto_name}: GSheet pull error: {e}")
        else:
            _log(f"[enrich] {auto_name}: GSheet pull not scheduled yet, skipping")
    else:
        _log(f"[enrich] {auto_name}: GSheet pull disabled, skipping")


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        secret = self.headers.get("X-Sync-Secret", "")
        if SYNC_SECRET and secret != SYNC_SECRET:
            self._json(401, {"error": "Unauthorized"})
            return
        self._run_sync()

    def _run_sync(self):
        all_automations = get_automations()
        # Exclude gsheet_sync — handled by /api/sync_gsheet
        active = [
            a for a in all_automations
            if a.get("active") and a.get("delivery_type") != "gsheet_sync"
        ]
        _log(f"[sync] running for {len(active)} active automations")

        # Load all sent: keys into memory once
        sent_cache = load_sent_cache()

        total_processed = total_duplicates = total_errors = total_waiting = total_filtered = 0

        est_now  = datetime.datetime.now(EST)
        est_date = est_now.date().isoformat()

        for automation in active:
            auto_id       = automation.get("id", "")
            delivery_type = automation.get("delivery_type", "instantly")

            # ── Enrichment automation ─────────────────────────
            if delivery_type == "enrichment":
                _log(f"[sync] enrichment {auto_id}: {automation.get('name', '')}")
                try:
                    run_enrichment(automation, sent_cache)
                except Exception as e:
                    _log(f"[sync] enrichment {auto_id} error: {e}")
                automation["last_run"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                continue

            # ── Instantly + HubSpot form ──────────────────────
            list_id     = automation.get("hubspot_list_id", "")
            target_id   = automation.get("instantly_campaign_id") if delivery_type == "instantly" else automation.get("hubspot_form_id")
            action      = automation.get("action", "enroll")
            delay_hours = float(automation.get("delay_hours", 0))
            filters     = automation.get("filters", [])

            _log(f"[sync] automation={auto_id} list={list_id} delivery={delivery_type} target={target_id} action={action} delay={delay_hours}h filters={len(filters)}")

            if not target_id:
                _log(f"[sync] skip: no target_id for automation {auto_id}")
                continue

            extra_props = [f["property"] for f in filters if f.get("property")]
            contacts    = get_list_contacts(list_id, extra_properties=extra_props)

            for c in contacts:
                email      = c["email"]
                first_name = c.get("firstname", "")
                last_name  = c.get("lastname", "")
                company    = c.get("company", "")
                try:
                    if already_sent_cached(email, target_id, sent_cache):
                        total_duplicates += 1
                        continue

                    if delivery_type == "instantly" and delay_hours > 0:
                        first_seen = get_first_seen(email, target_id)
                        if first_seen is None:
                            set_first_seen(email, target_id)
                            _log(f"[sync] delay: first seen {email}, waiting {delay_hours}h")
                            total_waiting += 1
                            continue
                        elapsed_hours = (time.time() - first_seen) / 3600
                        if elapsed_hours < delay_hours:
                            remaining = round(delay_hours - elapsed_hours, 1)
                            _log(f"[sync] delay: {email} waiting {remaining}h more")
                            total_waiting += 1
                            continue

                    if not check_filters(c, filters):
                        _log(f"[sync] filtered out {email}: failed conditions")
                        mark_as_sent(email, target_id, sent_cache)
                        total_filtered += 1
                        continue

                    if delivery_type == "instantly":
                        if action == "unenroll":
                            lead = get_instantly_lead(email, target_id)
                            if lead is None:
                                _log(f"[sync] unenroll: {email} not in campaign, skip")
                                mark_as_sent(email, target_id, sent_cache)
                                total_duplicates += 1
                                continue
                            lead_status = lead.get("status", "")
                            if lead_status in INSTANTLY_TERMINAL_STATUSES:
                                _log(f"[sync] unenroll: {email} already terminal ({lead_status}), skip")
                                mark_as_sent(email, target_id, sent_cache)
                                total_duplicates += 1
                                continue
                            lead_id = lead.get("id", "")
                            if not lead_id:
                                _log(f"[sync] unenroll: no lead_id for {email}, skip")
                                continue
                            unenroll_from_instantly(lead_id)
                        else:
                            add_to_instantly(email, first_name, last_name, company, target_id)
                            try:
                                increment_enroll_count(auto_id, est_date)
                            except Exception:
                                pass
                            try:
                                send_enrollment_notification(automation, email, first_name, last_name, company)
                            except Exception:
                                pass
                    else:
                        submit_hs_form(email, first_name, last_name, company, target_id)
                        try:
                            increment_enroll_count(auto_id, est_date)
                        except Exception:
                            pass
                        try:
                            send_enrollment_notification(automation, email, first_name, last_name, company)
                        except Exception:
                            pass

                    mark_as_sent(email, target_id, sent_cache)
                    try:
                        log_enrollment(auto_id, email,
                                       f"{delivery_type}_{action}" if delivery_type == "instantly" else delivery_type,
                                       time.time())
                    except Exception:
                        pass
                    _log(f"[sync] processed {email} -> {delivery_type} {action} {target_id}")
                    total_processed += 1

                except Exception as e:
                    _log(f"[sync] error for {email}: {e}")
                    total_errors += 1

            try:
                check_and_send_alert(automation, auto_id, est_now, est_date)
            except Exception as e:
                _log(f"[sync] alert check error for {auto_id}: {e}")

            automation["last_run"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        save_automations(all_automations)

        result = {
            "processed":  total_processed,
            "duplicates": total_duplicates,
            "waiting":    total_waiting,
            "filtered":   total_filtered,
            "errors":     total_errors
        }
        _log(f"[sync] done: {result}")
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
