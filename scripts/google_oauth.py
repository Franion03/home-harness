#!/usr/bin/env python3
"""One-time Google OAuth consent -> a refresh token for the harness.

A service account cannot read your personal calendar, so the harness uses a
user refresh token instead. Run this once on a machine with a browser:

    python3 scripts/google_oauth.py --client-id ... --client-secret ...

Setup in Google Cloud Console beforehand:
  1. Create or pick a project.
  2. Enable the Google Calendar API.
  3. APIs & Services -> Credentials -> Create credentials -> OAuth client ID
     -> Application type: Web application
     -> Authorised redirect URI: http://localhost:8765/callback
  4. OAuth consent screen -> External -> add your own address under
     "Test users" (a personal project stays in testing, which is fine).

Only the standard library is used, so there is nothing to install.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/callback"

received: dict[str, str] = {}
done = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        received["code"] = (params.get("code") or [""])[0]
        received["state"] = (params.get("state") or [""])[0]
        received["error"] = (params.get("error") or [""])[0]

        body = (
            b"<h2>Authorised.</h2><p>You can close this tab and return to the terminal.</p>"
            if received["code"]
            else b"<h2>Authorisation failed.</h2><p>Check the terminal.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        done.set()

    def log_message(self, *_args) -> None:
        pass  # keep the console clean


def exchange(client_id: str, client_secret: str, code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    args = ap.parse_args()

    state = secrets.token_urlsafe(16)
    auth_url = f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            # Google only returns a refresh token with these two together.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        print("\nOpen this URL and approve access:\n")
        print(f"  {auth_url}\n")
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001 - headless machines have no browser
            pass

        print(f"Waiting for the redirect on {REDIRECT_URI} ...")
        if not done.wait(timeout=300):
            print("Timed out after 5 minutes.", file=sys.stderr)
            return 1
        httpd.shutdown()

    if received.get("error"):
        print(f"Google returned an error: {received['error']}", file=sys.stderr)
        return 1
    if received.get("state") != state:
        print("State mismatch — aborting.", file=sys.stderr)
        return 1
    if not received.get("code"):
        print("No authorisation code received.", file=sys.stderr)
        return 1

    tokens = exchange(args.client_id, args.client_secret, received["code"])
    refresh = tokens.get("refresh_token")
    if not refresh:
        print(
            "No refresh_token in the response. Revoke the app's access at\n"
            "  https://myaccount.google.com/permissions\n"
            "and run this again — Google only issues one on first consent.",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 68)
    print("Success. Put these into the harness secret:\n")
    print(f"  GOOGLE_CLIENT_ID={args.client_id}")
    print(f"  GOOGLE_CLIENT_SECRET={args.client_secret}")
    print(f"  GOOGLE_REFRESH_TOKEN={refresh}")
    print("\nThen:  scripts/create-secrets.sh   (or seal-secrets.sh for GitOps)")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
