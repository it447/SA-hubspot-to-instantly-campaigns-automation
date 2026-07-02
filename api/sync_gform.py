import json
import os
import sys
import time
import datetime
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request

UPSTASH_URL     = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN   = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SYNC_SECRET     = os.environ.get("SYNC_SECRET", "")

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

# ── Redis ─────────────────────────────────────────────────────

def _redis_get(key):
    from urllib.parse import quote as _q
    url = f"{UPSTASH_URL}/get/{_q(key, safe='')}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    val = data.get("result")
    return json.loads(val) if val else None

def _redis_set(key, value):
    from urllib.parse import quote as _q
    pipeline = [["SET", key, json.dumps(value)]]
    body = json.dumps(pipeline).encode()
    req = Request(f"{UPSTASH_URL}/pipeline", data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()

def log_enrollment(auto_id, label, ts):
    try:
        entry = json.dumps({"email": label, "type": "gform", "ts": ts})
        pipeline = [
            ["LPUSH", f"logs:{auto_id}", entry],
            ["LTRIM", f"logs:{auto_id}", 0, 9999],
        ]
        body = json.dumps(pipeline).encode()
        req = Request(f"{UPSTASH_URL}/pipeline", data=body, headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type": "application/json"
        }, method="POST")
        with urlopen(req, timeout=5) as r:
            r.read()
    except Exception as e:
        _log(f"[gform] log_enrollment error: {e}")

def get_automations():
    data = _redis_get("automations_config")
    return data if isinstance(data, list) else []

# ── Google Forms API ──────────────────────────────────────────

def _get_access_token(scopes):
    import google.oauth2.service_account
    import google.auth.transport.requests as google_requests
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        json.loads(sa_json), scopes=scopes
    )
    creds.refresh(google_requests.Request())
    return creds.token

def _extract_form_id(form_url):
    """Extract form ID from a Google Form URL."""
    import re
    # Patterns: /forms/d/<id>/viewform  or  /forms/d/<id>  or just the ID
    m = re.search(r'/forms/d/([a-zA-Z0-9-_]+)', form_url)
    if m:
        return m.group(1)
    # If it looks like a raw ID already
    if re.match(r'^[a-zA-Z0-9-_]{20,}$', form_url.strip()):
        return form_url.strip()
    return None

def get_form_metadata(form_id, token):
    """Fetch form structure (title + question map)."""
    import requests as req_lib
    resp = req_lib.get(
        f"https://forms.googleapis.com/v1/forms/{form_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    title = data.get("info", {}).get("title", "Google Form")
    # Build question_id → question_title map
    questions = {}
    for item in data.get("items", []):
        q = item.get("questionItem", {}).get("question", {})
        qid = q.get("questionId")
        label = item.get("title", "")
        if qid and label:
            questions[qid] = label
    return title, questions

def get_form_responses(form_id, token, after_ts=None):
    """Fetch all form responses, optionally filtered to after a timestamp."""
    import requests as req_lib
    params = {"pageSize": 200}
    if after_ts:
        # RFC3339 format
        dt = datetime.datetime.fromtimestamp(after_ts, tz=datetime.timezone.utc)
        params["filter"] = f"timestamp > {dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    resp = req_lib.get(
        f"https://forms.googleapis.com/v1/forms/{form_id}/responses",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20
    )
    resp.raise_for_status()
    return resp.json().get("responses", [])

# ── Slack ─────────────────────────────────────────────────────

def send_slack_message(channel, text):
    body = json.dumps({"channel": channel, "text": text}).encode()
    req = Request("https://slack.com/api/chat.postMessage", data=body, headers={
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }, method="POST")
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def format_response_message(form_title, questions, response):
    """Format a single form response as a Slack message."""
    ts_str = response.get("lastSubmittedTime", "")
    try:
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        time_label = dt.strftime("%b %d, %Y at %I:%M %p UTC")
    except Exception:
        time_label = ts_str

    lines = [f"📋 *New response — {form_title}*", f"_Submitted: {time_label}_", ""]

    answers = response.get("answers", {})
    for qid, answer_data in answers.items():
        q_label = questions.get(qid, qid)
        text_answers = answer_data.get("textAnswers", {}).get("answers", [])
        if text_answers:
            value = ", ".join(a.get("value", "") for a in text_answers)
        else:
            value = "—"
        if value.strip():
            lines.append(f"• *{q_label}:* {value}")

    return "\n".join(lines)

# ── Main sync ─────────────────────────────────────────────────

def run_gform_sync(auto):
    auto_id    = auto.get("id", "")
    auto_name  = auto.get("name", "")
    form_url   = auto.get("form_url", "")
    channel    = auto.get("slack_channel", "")

    if not form_url or not channel:
        _log(f"[gform] {auto_name}: missing form_url or slack_channel — skipping")
        return

    form_id = _extract_form_id(form_url)
    if not form_id:
        _log(f"[gform] {auto_name}: could not extract form ID from {form_url}")
        return

    _log(f"[gform] running {auto_name} (form={form_id})")

    try:
        token = _get_access_token([
            "https://www.googleapis.com/auth/forms.responses.readonly",
            "https://www.googleapis.com/auth/forms.body.readonly",
        ])
    except Exception as e:
        _log(f"[gform] {auto_name}: auth error: {e}")
        return

    # Load last-seen timestamp from Redis
    state_key  = f"gform_state:{auto_id}"
    state      = _redis_get(state_key) or {}
    last_ts    = state.get("last_response_ts", None)

    try:
        form_title, questions = get_form_metadata(form_id, token)
    except Exception as e:
        _log(f"[gform] {auto_name}: metadata error: {e}")
        return

    try:
        responses = get_form_responses(form_id, token, after_ts=last_ts)
    except Exception as e:
        _log(f"[gform] {auto_name}: responses error: {e}")
        return

    if not responses:
        _log(f"[gform] {auto_name}: no new responses")
        return

    # Sort oldest first so we process in order
    def _parse_ts(r):
        try:
            return datetime.datetime.fromisoformat(r.get("lastSubmittedTime","").replace("Z","+00:00")).timestamp()
        except Exception:
            return 0

    responses.sort(key=_parse_ts)

    newest_ts = last_ts or 0
    sent = 0
    for resp in responses:
        resp_ts = _parse_ts(resp)
        if last_ts and resp_ts <= last_ts:
            continue  # already processed
        msg = format_response_message(form_title, questions, resp)
        try:
            result = send_slack_message(channel, msg)
            if result.get("ok"):
                sent += 1
                log_enrollment(auto_id, resp.get("responseId", "response"), resp_ts)
                if resp_ts > newest_ts:
                    newest_ts = resp_ts
            else:
                _log(f"[gform] slack error: {result.get('error')}")
        except Exception as e:
            _log(f"[gform] send error: {e}")

    if newest_ts > (last_ts or 0):
        _redis_set(state_key, {"last_response_ts": newest_ts})

    _log(f"[gform] {auto_name}: sent {sent} response(s) to Slack")


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        secret = self.headers.get("X-Sync-Secret", "")
        if secret != SYNC_SECRET:
            self.send_response(401)
            self.end_headers()
            return

        automations = get_automations()
        ran = 0
        for a in automations:
            if a.get("delivery_type") == "gform" and a.get("active"):
                run_gform_sync(a)
                ran += 1

        body = json.dumps({"ok": True, "ran": ran}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
