import json
import os
import sys
import time
import requests
from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import quote

UPSTASH_URL     = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN   = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

def _log(msg):
    print(msg, file=sys.stderr, flush=True)

def _redis_get(key):
    url = f"{UPSTASH_URL}/get/{key}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    val = data.get("result")
    return json.loads(val) if val else None

def get_automations():
    data = _redis_get("automations_config")
    return data if isinstance(data, list) else []

def log_event(auto_id, email, event_type):
    key = f"logs:{auto_id}"
    entry = json.dumps({"email": email, "ts": time.time(), "type": f"instantly_{event_type}"})
    encoded = quote(entry, safe="")
    url = f"{UPSTASH_URL}/lpush/{key}/{encoded}"
    req = Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urlopen(req, timeout=5) as r:
        r.read()

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

def update_hubspot_contact(email, property_name, property_value):
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    search_url = f"https://api.hubapi.com/contacts/v1/contact/email/{quote(email, safe='')}/profile"
    resp = requests.get(search_url, headers=headers, timeout=10)
    if resp.status_code == 404:
        _log(f"[instantly_webhook] contact not found in HubSpot: {email}")
        return False
    resp.raise_for_status()
    vid = resp.json().get("vid")
    if not vid:
        return False
    update_url = f"https://api.hubapi.com/contacts/v1/contact/vid/{vid}/profile"
    payload = {"properties": [{"property": property_name, "value": property_value}]}
    resp2 = requests.post(update_url, headers=headers, json=payload, timeout=10)
    resp2.raise_for_status()
    return True

_EVENT_NORMALIZE = {
    "reply_received":       "reply_received",
    "contact_replied":      "reply_received",
    "replied":              "reply_received",
    "lead_unsubscribed":    "unsubscribed",
    "contact_unsubscribed": "unsubscribed",
    "unsubscribe":          "unsubscribed",
    "opt_out":              "unsubscribed",
    "opted_out":            "unsubscribed",
    "lead_bounced":         "bounced",
    "contact_bounced":      "bounced",
    "bounced":              "bounced",
    "lead_interested":      "interested",
    "contact_interested":   "interested",
    "interested":           "interested",
    "lead_not_interested":  "not_interested",
    "not_interested":       "not_interested",
    "out_of_office":        "out_of_office",
    "lead_out_of_office":   "out_of_office",
}

def parse_webhook_payload(body):
    event_type  = (body.get("event_type") or body.get("type") or body.get("event") or "").lower()
    data        = body.get("data", body)
    email       = (data.get("lead_email") or data.get("email") or data.get("from_email") or
                   body.get("lead_email") or body.get("email") or "").strip().lower()
    campaign_id = (data.get("campaign_id") or body.get("campaign_id") or "").strip()
    normalized  = _EVENT_NORMALIZE.get(event_type, event_type)
    return email, campaign_id, normalized


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json(400, {"error": "Invalid JSON"})
            return

        _log(f"[instantly_webhook] received: {json.dumps(body)[:500]}")
        email, campaign_id, event_type = parse_webhook_payload(body)

        if not email:
            _log("[instantly_webhook] no email in payload, ignoring")
            self._json(200, {"ok": True, "skipped": "no email"})
            return

        automations = get_automations()
        inbound = [a for a in automations if a.get("active") and a.get("delivery_type") == "instantly_inbound"]
        _log(f"[instantly_webhook] email={email} event={event_type} campaign={campaign_id} automations={len(inbound)}")

        processed = 0
        for auto in inbound:
            auto_id       = auto.get("id", "")
            auto_name     = auto.get("name", auto_id)
            # Support both legacy single trigger_event and new trigger_events list
            trigger_events = auto.get("trigger_events") or ([auto["trigger_event"]] if auto.get("trigger_event") else [])
            auto_campaign  = auto.get("instantly_campaign_id", "")
            hs_property    = auto.get("hs_property", "")
            hs_value       = auto.get("hs_value", "")
            slack_channel  = auto.get("slack_channel", "")

            if trigger_events and event_type not in trigger_events:
                continue
            if auto_campaign and auto_campaign != campaign_id:
                continue
            if not hs_property:
                continue

            _log(f"[instantly_webhook] matched: {auto_name}")
            try:
                updated = update_hubspot_contact(email, hs_property, hs_value)
                if updated:
                    processed += 1
                    try:
                        log_event(auto_id, email, event_type)
                    except Exception as e:
                        _log(f"[instantly_webhook] log error: {e}")
                    if slack_channel and auto.get("slack_enabled"):
                        msg = auto.get("slack_message", f"✅ *{{name}}* — updated {{email}} ({{event}}).")
                        msg = (msg.replace("{{name}}", auto_name)
                                  .replace("{{email}}", email)
                                  .replace("{{event}}", event_type))
                        send_slack_message(slack_channel, msg)
            except Exception as e:
                _log(f"[instantly_webhook] error updating {email}: {e}")

        self._json(200, {"ok": True, "processed": processed})

    def do_GET(self):
        self._json(200, {"ok": True, "service": "instantly_webhook"})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
