import json
import os
import sys
import hmac
import hashlib
import requests
from http.server import BaseHTTPRequestHandler

HUBSPOT_API_KEY         = os.environ.get("HUBSPOT_API_KEY", "")
CALENDLY_WEBHOOK_SECRET = os.environ.get("CALENDLY_WEBHOOK_SECRET", "")

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

def create_or_update_hs_contact(email, first_name, last_name, phone):
    url = f"https://api.hubapi.com/contacts/v1/contact/createOrUpdate/email/{email}"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }
    properties = []
    if first_name:
        properties.append({"property": "firstname", "value": first_name})
    if last_name:
        properties.append({"property": "lastname",  "value": last_name})
    if phone:
        properties.append({"property": "phone", "value": phone})

    resp = requests.post(url, headers=headers, json={"properties": properties}, timeout=10)
    _log(f"[calendly] HubSpot upsert {email} status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()

class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        if CALENDLY_WEBHOOK_SECRET:
            sig_header = self.headers.get("Calendly-Webhook-Signature", "")
            try:
                parts = dict(p.split("=", 1) for p in sig_header.split(","))
                timestamp  = parts.get("t", "")
                signature  = parts.get("v1", "")
                to_sign    = f"{timestamp}.{raw_body.decode()}"
                expected   = hmac.new(
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
        last_name  = payload.get("last_name",  "")

        if not first_name:
            full = payload.get("name", "")
            parts = full.split(" ", 1)
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

        _log(f"[calendly] upserting contact email={email} first={first_name} last={last_name} phone={phone}")

        try:
            create_or_update_hs_contact(email, first_name, last_name, phone)
            self._json(200, {"ok": True, "email": email})
        except Exception as e:
            _log(f"[calendly] HubSpot error: {e}")
            self._json(500, {"error": str(e)})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
