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
CLAY_API_KEY    = os.environ.get("CLAY_API_KEY", "")

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
    url  = f"{UPSTASH_URL}/set/{key}"
    body = json.dumps(value).encode()
    req  = Request(url, data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type":  "application/json"
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

# ── Sent cache (for Clay deduplication) ──────────────────────

def load_sent_cache():
    """Scan all sent: keys into memory to avoid per-contact Redis reads."""
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
        _log(f"[sync_gsheet] sent cache load error: {e}")
        return None
    _log(f"[sync_gsheet] sent cache loaded: {len(sent)} keys")
    return sent

def already_sent_cached(email, target_key, sent_cache):
    key = f"sent:{email.lower()}:{target_key}"
    if sent_cache is None:
        url = f"{UPSTASH_URL}/get/{key}"
        req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
        with urlopen(req, timeout=5) as r:
            result = json.loads(r.read())
        return result.get("result") is not None
    return key in sent_cache

def mark_as_sent(email, target_key, sent_cache=None):
    key = f"sent:{email.lower()}:{target_key}"
    _redis_set_raw(key, 1)
    if sent_cache is not None:
        sent_cache.add(key)

# ── Google auth token cache ───────────────────────────────────

def get_google_token():
    cached = _redis_get("gsheet_token_cache")
    if cached and cached.get("token") and cached.get("expires_at"):
        try:
            expires_at = datetime.datetime.fromisoformat(cached["expires_at"])
            now        = datetime.datetime.now(datetime.timezone.utc)
            if (expires_at - now).total_seconds() > 300:
                _log("[gsheet] using cached Google token")
                return cached["token"]
        except Exception:
            pass

    _log("[gsheet] refreshing Google token")
    import google.oauth2.service_account
    import google.auth.transport.requests as google_requests

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
        _redis_set_json("gsheet_token_cache", {
            "token":      creds.token,
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
    range_name = quote(f"'{tab_name}'", safe='') if tab_name else "A1:ZZ"
    url  = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("values", [])

# ── HubSpot batch helpers ─────────────────────────────────────

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

def hs_batch_create(hs_object, inputs):
    hs_headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    errors = []
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        url   = f"https://api.hubapi.com/crm/v3/objects/{hs_object}/batch/create"
        try:
            resp = requests.post(url, headers=hs_headers, json={"inputs": batch}, timeout=20)
            if resp.status_code not in (200, 201, 207):
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            errors.append(str(e))
    return errors

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
            _log(f"[sync_gsheet] HubSpot list {list_id} fetch error: {e}")
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

    _log(f"[sync_gsheet] list {list_id} has {len(contacts)} contacts")
    return contacts

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
            "Content-Type":  "application/json"
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

def should_run_gsheet(automation, last_run_key="last_run"):
    schedule_type = automation.get("gsheet_schedule_type", "interval")
    last_run      = automation.get(last_run_key)
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
                lr      = datetime.datetime.fromisoformat(
                    last_run.replace("Z", "+00:00")).astimezone(EST)
                cal_lr  = lr.isocalendar()
                cal_now = est_now.isocalendar()
                if cal_lr[0] == cal_now[0] and cal_lr[1] == cal_now[1]:
                    return False
            except Exception:
                pass
        return True

    return True

# ── GSheet sync (existing gsheet_sync type) ───────────────────

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

    pk_idx        = headers.index(pk_column)
    upsert_inputs = []
    create_inputs = []

    for row in data_rows:
        row_padded = list(row) + [''] * max(0, len(headers) - len(row))
        pk_value   = row_padded[pk_idx].strip() if pk_idx < len(row_padded) else ''

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
            upsert_inputs.append({"id": pk_value, "idProperty": pk_type, "properties": properties})
        else:
            upsert_inputs.append({"id": pk_value, "properties": properties})

    _log(f"[gsheet] {auto_name}: {len(upsert_inputs)} upserts, {len(create_inputs)} creates for {hs_object}")

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

# ── Clay helpers (enrichment type) ───────────────────────────

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

def run_clay_push(automation, sent_cache):
    """Push unsent HubSpot contacts to Clay table."""
    auto_name     = automation.get("name", "?")
    auto_id       = automation.get("id", "")
    list_id       = automation.get("clay_hubspot_list_id", "") or automation.get("hubspot_list_id", "")
    table_id      = automation.get("clay_table_id", "")
    col_mappings  = automation.get("clay_column_mappings", [])
    slack_channel = automation.get("slack_channel", "")

    if not table_id:
        _log(f"[clay] {auto_name}: no table_id configured, skipping")
        return

    if not CLAY_API_KEY:
        _log(f"[clay] {auto_name}: CLAY_API_KEY not set, skipping")
        return

    contacts = get_list_contacts(list_id)
    clay_key = f"clay:{auto_id}"  # separate namespace so it doesn't conflict with other automations

    pushed  = 0
    skipped = 0
    errors  = 0

    for contact in contacts:
        email = contact["email"]

        if already_sent_cached(email, clay_key, sent_cache):
            skipped += 1
            continue

        # Build row from column mappings
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
    elif pushed > 0 and slack_channel and automation.get("slack_enabled"):
        send_slack_message(slack_channel,
            f"✅ *{auto_name}* — Clay push complete. {pushed} contacts sent.")

def run_enrichment_gsheet_pull(automation):
    """Pull enriched data from GSheet and upsert into HubSpot (enrichment type)."""
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
            upsert_inputs.append({"id": pk_value, "idProperty": pk_type, "properties": properties})
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
                f"✅ *{auto_name}* — GSheet pull complete. {len(upsert_inputs)} records updated.")

def run_enrichment(automation, sent_cache):
    """Run Clay push and/or GSheet pull depending on which toggles are on."""
    auto_name      = automation.get("name", "?")
    clay_enabled   = automation.get("clay_enabled", False)
    gsheet_enabled = automation.get("enrichment_gsheet_enabled", False)

    # ── Step 1: Clay push ─────────────────────────────────────
    if clay_enabled:
        _log(f"[enrich] {auto_name}: running Clay push")
        try:
            run_clay_push(automation, sent_cache)
        except Exception as e:
            _log(f"[enrich] {auto_name}: Clay push error: {e}")
    else:
        _log(f"[enrich] {auto_name}: Clay push disabled, skipping")

    # ── Step 2: GSheet pull ───────────────────────────────────
    if gsheet_enabled:
        # GSheet pull uses its own last_gsheet_run timestamp so it runs
        # on its own schedule independently of the Clay push
        if should_run_gsheet(automation, last_run_key="last_gsheet_run"):
            _log(f"[enrich] {auto_name}: running GSheet pull")
            try:
                run_enrichment_gsheet_pull(automation)
                automation["last_gsheet_run"] = datetime.datetime.utcnow().strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            except Exception as e:
                _log(f"[enrich] {auto_name}: GSheet pull error: {e}")
        else:
            _log(f"[enrich] {auto_name}: GSheet pull not scheduled yet, skipping")
    else:
        _log(f"[enrich] {auto_name}: GSheet pull disabled, skipping")

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

        # Pick up both gsheet_sync and enrichment automations
        active = [
            a for a in all_automations
            if a.get("active") and a.get("delivery_type") in ("gsheet_sync", "enrichment")
        ]

        _log(f"[sync_gsheet] running for {len(active)} active automations")

        # Load sent cache once for Clay deduplication
        sent_cache = load_sent_cache()

        ran     = 0
        skipped = 0
        errors  = 0

        for automation in active:
            auto_id       = automation.get("id", "")
            delivery_type = automation.get("delivery_type")

            # ── GSheet sync (may also include Clay push) ─────
            if delivery_type == "gsheet_sync":
                clay_enabled   = automation.get("clay_enabled", False)
                gsheet_enabled = automation.get("gsheet_enabled", False)
                did_something  = False

                # Clay push runs every cycle (no schedule, just dedup)
                if clay_enabled:
                    _log(f"[sync_gsheet] running clay push: {auto_id} — {automation.get('name', '')}")
                    try:
                        run_clay_push(automation, sent_cache)
                        did_something = True
                    except Exception as e:
                        _log(f"[sync_gsheet] {auto_id} clay error: {e}")
                        errors += 1

                # GSheet pull respects its own schedule
                if gsheet_enabled:
                    if not should_run_gsheet(automation):
                        _log(f"[sync_gsheet] {auto_id}: gsheet not scheduled yet, skip")
                    else:
                        _log(f"[sync_gsheet] running gsheet pull: {auto_id} — {automation.get('name', '')}")
                        try:
                            run_gsheet_sync(automation)
                            did_something = True
                        except Exception as e:
                            _log(f"[sync_gsheet] {auto_id} gsheet error: {e}")
                            errors += 1

                if not clay_enabled and not gsheet_enabled:
                    _log(f"[sync_gsheet] {auto_id}: neither clay nor gsheet enabled, skipping")
                    skipped += 1
                    continue

                if did_something:
                    ran += 1
                else:
                    skipped += 1

                automation["last_run"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

            # ── Enrichment ────────────────────────────────────
            elif delivery_type == "enrichment":
                _log(f"[sync_gsheet] running enrichment: {auto_id} — {automation.get('name', '')}")
                try:
                    run_enrichment(automation, sent_cache)
                    ran += 1
                except Exception as e:
                    _log(f"[sync_gsheet] {auto_id} error: {e}")
                    errors += 1
                automation["last_run"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        save_automations(all_automations)

        result = {"ran": ran, "skipped": skipped, "errors": errors}
        _log(f"[sync_gsheet] done: {result}")
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
