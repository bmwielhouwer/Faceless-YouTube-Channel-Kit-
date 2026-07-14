#!/usr/bin/env python3
"""One-time OneDrive authorization — mints MS_REFRESH_TOKEN.

Uses the Microsoft "device code" flow, so there's nothing to configure beyond an
app registration (no redirect URLs, no web server). Run it on your own laptop:

    python -m tools.onedrive_auth

It prints a code + URL, you sign in once, and it prints the refresh token to
paste into your host's environment as MS_REFRESH_TOKEN.

Prerequisite — register a free app once at https://portal.azure.com :
  Azure Active Directory -> App registrations -> New registration
    * Supported account types: "personal Microsoft accounts" (or "any org + personal")
    * Authentication -> Advanced -> "Allow public client flows" = Yes
  Copy the "Application (client) ID" -> set MS_CLIENT_ID in your .env.
  (No client secret is needed for the device-code public-client flow, but if you
   created one, set MS_CLIENT_SECRET too — the worker's refresh call accepts it.)
"""
from __future__ import annotations

import sys
import time

import requests

from config import load_config

SCOPE = "offline_access Files.ReadWrite.All User.Read"


def main() -> int:
    cfg = load_config()
    if not cfg.ms_client_id:
        print("Set MS_CLIENT_ID in .env first (see this file's header).", file=sys.stderr)
        return 2

    tenant = cfg.ms_tenant or "common"
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"

    dc = requests.post(
        f"{base}/devicecode",
        data={"client_id": cfg.ms_client_id, "scope": SCOPE},
        timeout=30,
    )
    dc.raise_for_status()
    flow = dc.json()

    print("\n" + "=" * 60)
    print(f"  Go to:  {flow['verification_uri']}")
    print(f"  Enter code:  {flow['user_code']}")
    print("=" * 60 + "\n")
    print("Waiting for you to sign in...")

    interval = flow.get("interval", 5)
    device_code = flow["device_code"]
    deadline = time.time() + flow.get("expires_in", 900)

    while time.time() < deadline:
        time.sleep(interval)
        tok = requests.post(
            f"{base}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": cfg.ms_client_id,
                "device_code": device_code,
            },
            timeout=30,
        )
        data = tok.json()
        if tok.ok and "refresh_token" in data:
            print("\n✅ Success. Set this in your host environment:\n")
            print(f"MS_REFRESH_TOKEN={data['refresh_token']}\n")
            print("(Keep it secret — it grants access to your OneDrive.)")
            return 0
        err = data.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print(f"\n❌ Auth failed: {data.get('error_description', err)}", file=sys.stderr)
        return 1

    print("\n❌ Timed out waiting for sign-in.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
