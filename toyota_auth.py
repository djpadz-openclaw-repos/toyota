#!/usr/bin/env python3
"""
Toyota Connected Services (North America) auth + API client.

Handles the full login flow including:
- ui_locales callback
- Username/password
- OTP via email (polled from djpadz@padz.net IMAP)

Usage:
    python3 toyota_auth.py status        # Get vehicle status
    python3 toyota_auth.py climate on    # Start climate control
    python3 toyota_auth.py climate off   # Stop climate control
    python3 toyota_auth.py lock          # Lock doors
    python3 toyota_auth.py unlock        # Unlock doors
"""

from __future__ import annotations

import asyncio
import email as emaillib
import imaplib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from toyota_na.auth import ToyotaOneAuth
from toyota_na.client import ToyotaOneClient

# ── Config ────────────────────────────────────────────────────────────────────

USERNAME = "djpadz@padz.net"
VIN = "JTMBDAFB2TA003072"
DEVICE_ID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
TOKEN_CACHE = os.path.expanduser("~/.openclaw-dj/projects/toyota/.toyota_tokens.json")
OP_ACCOUNT_TOKEN_FILE = os.path.expanduser("~/.openclaw-dj/projects/email-automation/.op-service-account")


# ── Credentials ───────────────────────────────────────────────────────────────

def _op(item: str, field: str) -> str:
    token = open(OP_ACCOUNT_TOKEN_FILE).read().strip()
    result = subprocess.run(
        ["op", "item", "get", item, "--vault", "OpenClaw", "--reveal", "--fields", f"label={field}"],
        capture_output=True, text=True,
        env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token}
    )
    if result.returncode != 0:
        raise RuntimeError(f"1Password lookup failed: {result.stderr}")
    return result.stdout.strip()


def get_toyota_password() -> str:
    return _op("Toyota bZ", "password")


def get_imap_password() -> str:
    return _op("email: djpadz@padz.net", "password")


# ── OTP polling ───────────────────────────────────────────────────────────────

def poll_toyota_otp(after_ts: float, timeout: int = 90) -> str:
    """Poll djpadz@padz.net IMAP for Toyota OTP code."""
    imap_pass = get_imap_password()
    deadline = time.time() + timeout
    print("[auth] Polling padz.net for Toyota OTP...", file=sys.stderr)

    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("mail.padz.net", 993)
            mail.login("djpadz", imap_pass)
            mail.select("INBOX")
            _, data = mail.search(None, "FROM", '"toyotaconnectedservices.com"')
            for uid in reversed(data[0].split()):
                _, msg_data = mail.fetch(uid, "(RFC822)")
                msg = emaillib.message_from_bytes(msg_data[0][1])
                try:
                    from email.utils import parsedate_to_datetime
                    recv_ts = parsedate_to_datetime(msg["Date"]).timestamp()
                except Exception:
                    recv_ts = 0
                if recv_ts < after_ts - 30:
                    continue
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                match = re.search(r"\b(\d{6})\b", body)
                if match:
                    print("[auth] Got Toyota OTP — deleting email.", file=sys.stderr)
                    mail.store(uid, "+FLAGS", "\\Deleted")
                    mail.expunge()
                    mail.logout()
                    return match.group(1)
            mail.logout()
        except Exception as e:
            print(f"[auth] IMAP error: {e}", file=sys.stderr)
        time.sleep(5)

    raise RuntimeError("Timed out waiting for Toyota OTP email")


# ── Auth ──────────────────────────────────────────────────────────────────────

async def authenticate() -> ToyotaOneAuth:
    """Full login flow with OTP support. Returns authenticated ToyotaOneAuth."""
    # Try cached tokens first
    if os.path.exists(TOKEN_CACHE):
        try:
            tokens = json.load(open(TOKEN_CACHE))
            auth = ToyotaOneAuth(initial_tokens=tokens)
            if auth.logged_in():
                print("[auth] Using cached tokens.", file=sys.stderr)
                return auth
        except Exception:
            pass

    print("[auth] Logging in...", file=sys.stderr)
    password = get_toyota_password()
    auth = ToyotaOneAuth(callback=lambda t: json.dump(t, open(TOKEN_CACHE, "w")))

    async with aiohttp.ClientSession() as session:
        headers = {
            "Accept-API-Version": "resource=2.1, protocol=1.0",
            "X-Device-Id": DEVICE_ID,
        }
        data: dict[str, Any] = {}
        sent_at = time.time()

        for _ in range(12):
            async with session.post(auth.AUTHENTICATE_URL, json=data, headers=headers) as resp:
                data = await resp.json()
                if "tokenId" in data:
                    break
                otp_step = False
                for cb in data.get("callbacks", []):
                    cb_type = cb["type"]
                    if cb_type == "NameCallback":
                        prompt = cb["output"][0]["value"]
                        cb["input"][0]["value"] = "en-US" if prompt == "ui_locales" else USERNAME
                    elif cb_type == "PasswordCallback":
                        if otp_step:
                            otp = poll_toyota_otp(sent_at)
                            print(f"[auth] Submitting OTP.", file=sys.stderr)
                            cb["input"][0]["value"] = otp
                        else:
                            cb["input"][0]["value"] = password
                    elif cb_type == "TextOutputCallback":
                        msg = cb["output"][0]["value"]
                        if "OTP" in msg:
                            otp_step = True
                            sent_at = time.time()
                    elif cb_type in ("ChoiceCallback", "ConfirmationCallback"):
                        if cb.get("input"):
                            cb["input"][0]["value"] = 0
        else:
            raise RuntimeError("Authentication failed — no tokenId received")

        # Exchange tokenId for auth code
        headers["Cookie"] = f"iPlanetDirectoryPro={data['tokenId']}"
        auth_params = {
            "client_id": "oneappsdkclient",
            "scope": "openid profile write",
            "response_type": "code",
            "redirect_uri": "com.toyota.oneapp:/oauth2Callback",
            "code_challenge": "plain",
            "code_challenge_method": "plain",
        }
        async with session.get(
            f"{auth.AUTHORIZE_URL}?{urlencode(auth_params)}",
            headers=headers, allow_redirects=False
        ) as resp:
            location = resp.headers["Location"]
            code = parse_qs(urlparse(location).query)["code"][0]

    await auth.request_tokens(code)
    # Cache tokens
    json.dump(auth.get_tokens(), open(TOKEN_CACHE, "w"))
    print("[auth] Login successful.", file=sys.stderr)
    return auth


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_status() -> None:
    auth = await authenticate()
    client = ToyotaOneClient(auth)
    status = await client.get_vehicle_status(VIN)
    print(json.dumps(status, indent=2))


async def cmd_climate(state: str) -> None:
    """state: 'on' or 'off'"""
    auth = await authenticate()
    client = ToyotaOneClient(auth)
    command = "ac-settings-on" if state == "on" else "engine-stop"
    result = await client.remote_request(VIN, command)
    print(json.dumps(result, indent=2))


async def cmd_lock(state: str) -> None:
    """state: 'lock' or 'unlock'"""
    auth = await authenticate()
    client = ToyotaOneClient(auth)
    command = "doorLock" if state == "lock" else "doorUnlock"
    result = await client.remote_request(VIN, command)
    print(json.dumps(result, indent=2))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0]
    try:
        if cmd == "status":
            asyncio.run(cmd_status())
        elif cmd == "climate" and len(args) > 1:
            asyncio.run(cmd_climate(args[1]))
        elif cmd == "lock":
            asyncio.run(cmd_lock("lock"))
        elif cmd == "unlock":
            asyncio.run(cmd_lock("unlock"))
        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)
            sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
