import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlparse

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
REDIRECT_URI = "https://sa-hubspot-to-instantly-campaigns-a.vercel.app/api/google/oauth/callback"
SCOPE = "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/userinfo.email"


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        email  = params.get("email", [""])[0]

        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id":     GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri":  REDIRECT_URI,
            "response_type": "code",
            "scope":         SCOPE,
            "access_type":   "offline",
            "prompt":        "consent",
            "state":         email,
            "login_hint":    email,
        })

        self.send_response(302)
        self.send_header("Location", auth_url)
        self.end_headers()

    def log_message(self, *args):
        pass
