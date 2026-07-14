#!/usr/bin/env python3
"""One-time Google authorization — mints YOUTUBE_REFRESH_TOKEN.

Run this on your own laptop (it opens a browser):

    python -m tools.gdrive_auth

It prints YOUTUBE_REFRESH_TOKEN — paste that into your host's environment. The
one token covers Drive uploads, Gmail notifications, and YouTube uploads.

Prerequisite — create a free OAuth client once (5 min, no Microsoft involved):
  1. https://console.cloud.google.com  -> create/select any project.
  2. APIs & Services -> Library -> enable "Google Drive API", "Gmail API",
     and "YouTube Data API v3".
  3. APIs & Services -> OAuth consent screen -> External -> add yourself as a
     "Test user" (so it works without verification).
  4. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID ->
     Application type: "Desktop app". Copy the Client ID + Client secret.
  5. Put them in .env as YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET, then run this.
"""
from __future__ import annotations

import http.server
import sys
import urllib.parse
import webbrowser

import requests

from config import load_config

SCOPE = ("https://www.googleapis.com/auth/drive.file "
         "https://www.googleapis.com/auth/gmail.send "
         "https://www.googleapis.com/auth/youtube.upload")
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
PORT = 8765


def _exchange(cfg, code: str, redirect_uri: str) -> int:
    tok = requests.post(
        TOKEN_URI,
        data={
            "code": code,
            "client_id": cfg.google_client_id,
            "client_secret": cfg.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not tok.ok:
        print(f"\n❌ Token exchange failed: {tok.text}", file=sys.stderr)
        return 1
    refresh = tok.json().get("refresh_token")
    if not refresh:
        print("\n❌ No refresh token returned. Revoke prior access at "
              "https://myaccount.google.com/permissions and re-run.", file=sys.stderr)
        return 1

    print("\n✅ Success. Set this in your host (and your local .env):\n")
    print(f"YOUTUBE_REFRESH_TOKEN={refresh}\n")
    print("(Keep it secret — it grants upload access to your Drive folder.)")
    return 0


def _auth_url(cfg, redirect_uri: str) -> str:
    return f"{AUTH_URI}?" + urllib.parse.urlencode(
        {
            "client_id": cfg.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )


def main() -> int:
    cfg = load_config()
    if not (cfg.google_client_id and cfg.google_client_secret):
        print("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first "
              "(see this file's header).", file=sys.stderr)
        return 2

    redirect_uri = f"http://127.0.0.1:{PORT}/"

    # Headless mode: no local web server. Print the URL, let the user approve in
    # any browser, then paste back the resulting code (or the redirect URL).
    if "--manual" in sys.argv:
        print("\n1) Open this URL in your browser and approve access:\n")
        print(_auth_url(cfg, redirect_uri))
        print("\n2) Your browser will try to load a 'site can't be reached' page at")
        print("   http://127.0.0.1:8765/?code=... — that's expected. Copy the FULL")
        print("   address from the URL bar (or just the code) and paste it here.\n")
        pasted = input("Paste code or redirect URL: ").strip()
        if "code=" in pasted:
            pasted = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)["code"][0]
        return _exchange(cfg, pasted, redirect_uri)

    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params:
                captured["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Faceless YouTube Kit: authorized. You can close this tab.</h2>")
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *_args):  # silence the default logging
            pass

    auth_url = _auth_url(cfg, redirect_uri)

    print("\nOpening your browser to authorize Google Drive access...")
    print(f"If it doesn't open, paste this into your browser:\n{auth_url}\n")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    while "code" not in captured:
        server.handle_request()

    return _exchange(cfg, captured["code"], redirect_uri)


if __name__ == "__main__":
    raise SystemExit(main())
