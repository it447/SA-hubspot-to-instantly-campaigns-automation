import os
import json
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, urlencode, quote
from urllib.request import urlopen, Request

GOOGLE_OAUTH_CLIENT_ID     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
UPSTASH_URL                = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN              = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIRECT_URI               = "https://sa-hubspot-to-instantly-campaigns-a.vercel.app/api/google/oauth/callback"
APP_URL                    = "https://sa-hubspot-to-instantly-campaigns-a.vercel.app"

def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code   = params.get("code",  [""])[0]
        state  = params.get("state", [""])[0]   # email hint passed in state
        error  = params.get("error", [""])[0]

        if error:
            self._redirect("error", f"Google auth failed: {error}")
            return
        if not code:
            self._redirect("error", "No authorisation code received")
            return

        # Exchange code for tokens
        token_data = urlencode({
            "code":          code,
            "client_id":     GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri":  REDIRECT_URI,
            "grant_type":    "authorization_code",
        }).encode()

        req = Request(
            "https://oauth2.googleapis.com/token",
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        try:
            with urlopen(req, timeout=10) as r:
                tokens = json.loads(r.read())
        except Exception as e:
            _log(f"[oauth_callback] token exchange error: {e}")
            self._redirect("error", "Token exchange failed")
            return

        if "error" in tokens:
            self._redirect("error", tokens.get("error_description", tokens["error"]))
            return

        access_token = tokens.get("access_token", "")

        # Get real email from Google userinfo
        try:
            ui_req = Request(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            with urlopen(ui_req, timeout=10) as r:
                userinfo = json.loads(r.read())
            email = userinfo.get("email", state)
        except Exception:
            email = state

        # Store token record in Redis
        token_record = {
            "access_token":  access_token,
            "refresh_token": tokens.get("refresh_token", ""),
            "expiry":        time.time() + tokens.get("expires_in", 3600),
            "email":         email,
        }
        key     = f"gcal_token:{email}"
        encoded = json.dumps(token_record)
        url     = f"{UPSTASH_URL}/set/{quote(key, safe='')}"
        req2    = Request(url, data=json.dumps([encoded]).encode(), headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type":  "application/json"
        }, method="POST")
        try:
            with urlopen(req2, timeout=5) as r:
                r.read()
            _log(f"[oauth_callback] stored token for {email}")
        except Exception as e:
            _log(f"[oauth_callback] redis store error: {e}")

        self._redirect("success", email)

    def _redirect(self, status, msg):
        safe_msg = msg.replace("\\", "\\\\").replace("`", "\\`")
        html = f"""<!DOCTYPE html><html><head><title>Google OAuth</title></head><body>
<script>
(function(){{
  var payload = {{oauth:'{status}',email:`{safe_msg}`}};
  if(window.opener){{
    window.opener.postMessage(payload,'*');
    setTimeout(function(){{window.close();}},300);
  }} else {{
    window.location.href='{APP_URL}/?oauth={status}&email={quote(msg)}';
  }}
}})();
</script>
<p style="font-family:sans-serif;text-align:center;margin-top:60px">
{'Connected! You may close this window.' if status=='success' else 'Auth error: '+msg}
</p></body></html>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
