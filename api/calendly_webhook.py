# v2
import json
import os
import sys
import hmac
import hashlib
import time
import datetime
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request

HUBSPOT_API_KEY         = os.environ.get("HUBSPOT_API_KEY", "")
CALENDLY_WEBHOOK_SECRET = os.environ.get("CALENDLY_WEBHOOK_SECRET", "")
UPSTASH_URL             = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN           = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
SLACK_BOT_TOKEN         = os.environ.get("SLACK_BOT_TOKEN", "")

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

def _redis_get(key):
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    val = data.get("result")
    return json.loads(val) if val else None

def log_booking(auto_id, email, name):
    entry = json.dumps({"email": email, "name": name, "ts": time.time(), "type": "calendly"})
    pipeline = [
        ["LPUSH", f"logs:{auto_id}", entry],
        ["LTRIM", f"logs:{auto_id}", 0, 999]
    ]
    body = json.dumps(pipeline).encode()
    req  = Request(f"{UPSTASH_URL}/pipeline", data=body, headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type":  "application/json"
    }, method="POST")
    with urlopen(req, timeout=5) as r:
        r.read()

def send_slack(channel_id, text):
    if not SLACK_BOT_TOKEN or not channel_id:
        return
    payload = json.dumps({"channel": channel_id, "text": text}).encode()
    req = Request("https://slack.com/api/chat.postMessage", data=payload, headers={
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type":  "application/json"
    }, method="POST")
    with urlopen(req, timeout=10) as r:
        r.read()

def create_or_update_hs_contact(email, first_name, last_name, phone):
    url = f"https://api.hubapi.com/contacts/v1/contact/createOrUpdate/email/{email}"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    properties = []
    if first_name: properties.append({"property": "firstname", "value": first_name})
    if last_name:  properties.append({"property": "lastname",  "value": last_name})
    if phone:      properties.append({"property": "phone",     "value": phone})
    resp = requests.post(url, headers=headers, json={"properties": properties}, timeout=10)
    _log(f"[calendly] HubSpot upsert {email} status={resp.status_code}")
    resp.raise_for_status()
    return resp.json()

def get_calendly_automations():
    try:
        data = _redis_get("automations_config")
        if isinstance(data, list):
            return [a for a in data if a.get("delivery_type") == "calendly" and a.get("active")]
    except Exception:
        pass
    return []

class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length   = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        # Verify signature if secret is set
        if CALENDLY_WEBHOOK_SECRET:
            sig_header = self.headers.get("Calendly-Webhook-Signature", "")
            try:
                parts     = dict(p.split("=", 1) for p in sig_header.split(","))
                timestamp = parts.get("t", "")
                signature = parts.get("v1", "")
                to_sign   = f"{timestamp}.{raw_body.decode()}"
                expected  = hmac.new(
                    CALENDLY_WEBHOOK_SECRET.encode(),
                    to_sign.encode(),
                    hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    self._json(401, {"error": "Invalid signature"})
                    return
            except Exception as e:
                _log(f"[calendly] signature error: {e}")
                self._json(401, {"error": "Signature verification failed"})
                return

        try:
            body = json.loads(raw_body) if raw_body else {}
        except Exception:
            self._json(400, {"error": "Invalid JSON"})
            return

        event = body.get("event", "")
        _log(f"[calendly] received event={event}")

        if event != "invitee.created":
            self._json(200, {"ok": True, "skipped": True})
            return

        payload    = body.get("payload", {})
        email      = payload.get("email", "").strip().lower()
        first_name = payload.get("first_name", "")
        last_name  = payload.get("last_name", "")

        if not first_name:
            full       = payload.get("name", "")
            parts      = full.split(" ", 1)
            first_name = parts[0]
            last_name  = parts[1] if len(parts) > 1 else ""

        phone = ""
        for qa in payload.get("questions_and_answers", []):
            q = qa.get("question", "").lower()
            if "phone" in q or "mobile" in q or "number" in q:
                phone = qa.get("answer", "").strip()
                break

        if not email:
            self._json(400, {"error": "No email in payload"})
            return

        _log(f"[calendly] booking: email={email} name={first_name} {last_name}")

        # Create/update HubSpot contact
        try:
            create_or_update_hs_contact(email, first_name, last_name, phone)
        except Exception as e:
            _log(f"[calendly] HubSpot error: {e}")
            self._json(500, {"error": str(e)})
            return

        # Log to Redis + send Slack for each matching Calendly automation
        event_uri = payload.get("event_type", {}).get("uri", "") if isinstance(payload.get("event_type"), dict) else ""
        full_name = f"{first_name} {last_name}".strip()

        automations = get_calendly_automations()
        for auto in automations:
            auto_id = auto.get("id", "")
            try:
                log_booking(auto_id, email, full_name)
            except Exception as e:
                _log(f"[calendly] log error: {e}")

            if auto.get("slack_enabled") and auto.get("slack_channel"):
                msg = auto.get("slack_message", "📅 New Calendly booking from {{email}}")
                msg = msg.replace("{{email}}", email).replace("{{name}}", full_name)
                try:
                    send_slack(auto["slack_channel"], msg)
                except Exception as e:
                    _log(f"[calendly] slack error: {e}")

        self._json(200, {"ok": True, "email": email})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
