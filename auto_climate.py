#!/usr/bin/env python3
"""
Toyota bZ auto climate control.

Logic:
- Get car's current GPS location from Toyota API
- If car is within HOME_RADIUS_M of home → do nothing (in garage/driveway)
- Get outdoor temp at car's location via Open-Meteo
- If temp >= TEMP_THRESHOLD_F and AC is off → start remote AC
- If temp < TEMP_THRESHOLD_F and AC is on (auto-started by us) → stop it

Handles token expiry automatically via IMAP OTP re-auth.
Exits 0 always. Prints a brief status line.
"""

from __future__ import annotations

import asyncio
import email as emaillib
import imaplib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

# ── Telegram notification ─────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = "8629480213:AAGoHm6UXRhgfmo-UdIt6Kcdpzi1dE8HU5I"
TELEGRAM_CHAT_ID = "8623402151"

def telegram_notify(message: str) -> None:
    """Send a Telegram message directly via Bot API — no LLM involved."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10):
            pass
    except Exception as e:
        print(f"[notify] Telegram error: {e}", file=sys.stderr)

import aiohttp
import jwt
from toyota_na.auth import ToyotaOneAuth

# ── Config ────────────────────────────────────────────────────────────────────

HOME_LAT = 32.7716810
HOME_LON = -117.1690550
HOME_RADIUS_M = 150          # ~500ft — covers driveway + garage
TEMP_THRESHOLD_F = 85.0      # Start AC above this outdoor temp
VIN = "JTMBDAFB2TA003072"
USERNAME = "djpadz@padz.net"
DEVICE_ID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

API_GATEWAY = "https://onecdn.telematicsct.com/oneapi/"
API_KEY = "pypIHG015k4ABHWbcI4G0a94F7cC0JDo1OynpAsG"
GRAPHQL_ENDPOINT = "https://oa-api.telematicsct.com/graphql"
APPSYNC_API_KEY = "da2-zgeayo2qh5eo7cj6pmdwhwugze"
USER_AGENT = "ToyotaOneApp/3.10.0 (com.toyota.oneapp; build:3100; Android 14) okhttp/4.12.0"
TOKEN_CACHE = os.path.expanduser("~/.openclaw-dj/projects/toyota/.toyota_tokens.json")
STATE_FILE = os.path.expanduser("~/.openclaw-dj/projects/toyota/.auto_climate_state.json")
OP_TOKEN_FILE = os.path.expanduser("~/.openclaw-dj/projects/email-automation/.op-service-account")


# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p)
         * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def get_outdoor_temp_f(lat: float, lon: float) -> Optional[float]:
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&current_weather=true&temperature_unit=fahrenheit")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())["current_weather"]["temperature"]
    except Exception as e:
        print(f"[weather] Error: {e}", file=sys.stderr)
        return None


def load_state() -> dict:
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"auto_started": False}


def save_state(state: dict) -> None:
    json.dump(state, open(STATE_FILE, "w"))


def _op(item: str, field: str) -> str:
    token = open(OP_TOKEN_FILE).read().strip()
    result = subprocess.run(
        ["op", "item", "get", item, "--vault", "OpenClaw", "--reveal", "--fields", f"label={field}"],
        capture_output=True, text=True,
        env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token}
    )
    if result.returncode != 0:
        raise RuntimeError(f"1Password lookup failed: {result.stderr}")
    return result.stdout.strip()


# ── OTP polling ───────────────────────────────────────────────────────────────

def poll_toyota_otp(after_ts: float, timeout: int = 90) -> str:
    imap_pass = _op("email: djpadz@padz.net", "password")
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

async def do_full_login() -> dict:
    """Full login flow with OTP. Returns raw token dict."""
    print("[auth] Full login required...", file=sys.stderr)
    password = _op("Toyota bZ", "password")
    auth = ToyotaOneAuth()

    async with aiohttp.ClientSession() as session:
        headers = {"Accept-API-Version": "resource=2.1, protocol=1.0", "X-Device-Id": DEVICE_ID}
        data: dict = {}
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
                            cb["input"][0]["value"] = otp
                        else:
                            cb["input"][0]["value"] = password
                    elif cb_type == "TextOutputCallback":
                        if "OTP" in cb["output"][0]["value"]:
                            otp_step = True
                            sent_at = time.time()
                    elif cb_type in ("ChoiceCallback", "ConfirmationCallback"):
                        if cb.get("input"):
                            cb["input"][0]["value"] = 0
        else:
            raise RuntimeError("Authentication failed — no tokenId received")

        headers["Cookie"] = f"iPlanetDirectoryPro={data['tokenId']}"
        auth_params = {
            "client_id": "oneappsdkclient", "scope": "openid profile write",
            "response_type": "code", "redirect_uri": "com.toyota.oneapp:/oauth2Callback",
            "code_challenge": "plain", "code_challenge_method": "plain",
        }
        async with session.get(
            f"{auth.AUTHORIZE_URL}?{urlencode(auth_params)}",
            headers=headers, allow_redirects=False
        ) as resp:
            code = parse_qs(urlparse(resp.headers["Location"]).query)["code"][0]

    await auth.request_tokens(code)
    tokens = auth.get_tokens()
    json.dump(tokens, open(TOKEN_CACHE, "w"))
    print("[auth] Login successful — tokens cached.", file=sys.stderr)
    return tokens


async def refresh_access_token(refresh_token: str) -> Optional[dict]:
    """Try to refresh using the refresh token."""
    data = {
        "client_id": "oneappsdkclient",
        "redirect_uri": "com.toyota.oneapp:/oauth2Callback",
        "grant_type": "refresh_token",
        "code_verifier": "plain",
        "refresh_token": refresh_token,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(ToyotaOneAuth.ACCESS_TOKEN_URL, data=data) as resp:
                if resp.status == 200:
                    token_resp = await resp.json()
                    # Rebuild token dict
                    id_token = token_resp["id_token"]
                    guid = jwt.decode(
                        id_token, algorithms=["RS256"],
                        options={"verify_signature": False}, audience="oneappsdkclient"
                    )["sub"]
                    tokens = {
                        "access_token": token_resp["access_token"],
                        "refresh_token": token_resp["refresh_token"],
                        "id_token": id_token,
                        "expires_at": time.time() + token_resp["expires_in"],
                        "updated_at": time.time(),
                        "guid": guid,
                    }
                    json.dump(tokens, open(TOKEN_CACHE, "w"))
                    print("[auth] Token refreshed.", file=sys.stderr)
                    return tokens
    except Exception as e:
        print(f"[auth] Refresh failed: {e}", file=sys.stderr)
    return None


async def get_valid_tokens() -> dict:
    """Load tokens, refresh if needed, full re-login if refresh fails."""
    tokens: dict = {}
    if os.path.exists(TOKEN_CACHE):
        try:
            tokens = json.load(open(TOKEN_CACHE))
        except Exception:
            pass

    expires_at = tokens.get("expires_at", 0)

    # Still valid
    if expires_at > time.time() + 60:
        return tokens

    # Try refresh first
    if tokens.get("refresh_token"):
        refreshed = await refresh_access_token(tokens["refresh_token"])
        if refreshed:
            return refreshed

    # Full login with OTP
    return await do_full_login()


# ── Toyota API ────────────────────────────────────────────────────────────────

def build_headers(access_token: str, guid: str) -> dict:
    return {
        "AUTHORIZATION": f"Bearer {access_token}",
        "X-API-KEY": API_KEY,
        "X-GUID": guid,
        "X-CHANNEL": "ONEAPP",
        "X-BRAND": "T",
        "x-region": "US",
        "X-APPVERSION": "3.1.0",
        "X-LOCALE": "en-US",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "VIN": VIN,
    }


async def api_get(session: aiohttp.ClientSession, headers: dict, endpoint: str) -> Optional[dict]:
    from urllib.parse import urljoin
    async with session.get(urljoin(API_GATEWAY, endpoint), headers=headers) as resp:
        if resp.status == 200:
            data = await resp.json()
            return data.get("payload", data)
        return None


async def api_post(session: aiohttp.ClientSession, headers: dict, endpoint: str, body: dict) -> Optional[dict]:
    from urllib.parse import urljoin
    async with session.post(urljoin(API_GATEWAY, endpoint), headers=headers, json=body) as resp:
        data = await resp.json()
        return data.get("payload", data)


async def pre_wake(session: aiohttp.ClientSession, access_token: str, guid: str) -> None:
    """Send pre-wake command to rouse the telematics module."""
    gql = """mutation SendPreWakeCommand($guid: String!) {
      postPreWake(guid: $guid) {
        timestamp
        status { messages { responseCode } }
      }
    }"""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": APPSYNC_API_KEY,
        "x-resolver-api-key": API_KEY,
        "Authorization": f"Bearer {access_token}",
        "x-guid": guid,
        "X-APPBRAND": "T",
        "x-channel": "ONEAPP",
        "X-APPVERSION": "3.1.0",
        "User-Agent": USER_AGENT,
    }
    payload = json.dumps({"operationName": "SendPreWakeCommand", "query": gql, "variables": {"guid": guid}})
    try:
        async with session.post(GRAPHQL_ENDPOINT, headers=headers, data=payload) as resp:
            result = await resp.json()
            code = result.get("data", {}).get("postPreWake", {}).get("status", {}).get("messages", [{}])[0].get("responseCode", "")
            print(f"[pre-wake] {code}", file=sys.stderr)
    except Exception as e:
        print(f"[pre-wake] Error: {e}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    tokens = await get_valid_tokens()
    access_token = tokens["access_token"]
    guid = jwt.decode(
        tokens["id_token"], algorithms=["RS256"],
        options={"verify_signature": False}, audience="oneappsdkclient"
    )["sub"]
    headers = build_headers(access_token, guid)
    state = load_state()

    async with aiohttp.ClientSession() as session:
        ev_data = await api_get(session, headers, "v2/electric/status")
        if not ev_data:
            print("ERROR: Could not fetch vehicle status.")
            sys.exit(1)

        pos = ev_data.get("positionInfo", {})
        car_lat = pos.get("latitude")
        car_lon = pos.get("longitude")

        if car_lat is None or car_lon is None:
            print("No GPS position available — skipping.")
            return

        dist_m = haversine_m(HOME_LAT, HOME_LON, car_lat, car_lon)

        if dist_m <= HOME_RADIUS_M:
            print(f"Car is at home ({dist_m:.0f}m from home) — no action.")
            if state.get("auto_started"):
                print("Auto-stopping AC (car returned home).")
                await api_post(session, headers, "v1/global/remote/command", {"command": "engine-stop"})
                state["auto_started"] = False
                save_state(state)
            return

        temp_f = get_outdoor_temp_f(car_lat, car_lon)
        if temp_f is None:
            print("Could not get outdoor temp — skipping.")
            return

        vehicle_info = ev_data.get("vehicleInfo", {})
        hvac = vehicle_info.get("remoteHvacInfo", {})
        ac_on = hvac.get("remoteHvacMode", 0) != 0
        charge_pct = vehicle_info.get("chargeInfo", {}).get("chargeRemainingAmount")

        print(f"Car at ({car_lat:.4f}, {car_lon:.4f}), {dist_m:.0f}m from home")
        print(f"Outdoor temp: {temp_f:.1f}°F | AC: {'on' if ac_on else 'off'} | Battery: {charge_pct}% | Threshold: {TEMP_THRESHOLD_F}°F")

        # Don't run AC if battery is below 25%
        if charge_pct is not None and charge_pct < 25:
            print(f"Battery at {charge_pct}% — skipping AC to preserve charge.")
            return

        if temp_f >= TEMP_THRESHOLD_F and not ac_on:
            print(f"Temp {temp_f:.1f}°F ≥ {TEMP_THRESHOLD_F}°F — starting remote AC.")
            # Pre-wake the telematics module, then retry AC command up to 3 times
            print("[ac] Sending pre-wake...", file=sys.stderr)
            await pre_wake(session, access_token, guid)
            await asyncio.sleep(8)

            started = False
            for attempt in range(3):
                # 21MM EVs use engine-start for climate preconditioning (ac-settings-on is 17CY gas only)
            result = await api_post(session, headers, "v1/global/remote/command", {"command": "engine-start"})
                response_code = result.get("status", {}).get("messages", [{}])[0].get("responseCode", "") if result else ""
                print(f"[ac] Attempt {attempt+1}: {response_code}", file=sys.stderr)
                if response_code and "40009" not in response_code:
                    started = True
                    break
                if attempt < 2:
                    await asyncio.sleep(10)

            if started:
                state["auto_started"] = True
                save_state(state)
                telegram_notify(f"🌡️ It's {temp_f:.0f}°F outside your car — I've started the AC remotely.")
            else:
                print(f"[ac] Failed to start AC after 3 attempts.", file=sys.stderr)

        elif temp_f < TEMP_THRESHOLD_F and state.get("auto_started"):
            print(f"Temp {temp_f:.1f}°F < {TEMP_THRESHOLD_F}°F — stopping auto-started AC.")
            await api_post(session, headers, "v1/global/remote/command", {"command": "engine-stop"})
            state["auto_started"] = False
            save_state(state)

        else:
            print("No action needed.")


if __name__ == "__main__":
    asyncio.run(main())
