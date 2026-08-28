"""
Claude Usage Bar - core logic: credentials, token refresh, usage fetch.

Talks to the same endpoint the Claude Code CLI uses:
    GET https://api.anthropic.com/api/oauth/usage
authenticated with the OAuth access token stored in ~/.claude/.credentials.json.

Credentials are only ever sent to Anthropic's own OAuth/API hosts, and the
token is never logged or written anywhere other than the credentials file it
came from.
"""

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-usage-bar/1.0"

CRED_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")

# Refresh the access token this many seconds before it actually expires.
EXPIRY_SKEW = 120


class UsageError(Exception):
    """Raised with a short, display-ready message."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind  # "auth" | "network" | "error"


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def load_credentials():
    if not os.path.exists(CRED_PATH):
        raise UsageError("No Claude credentials found", kind="auth")
    try:
        with open(CRED_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise UsageError("Cannot read credentials: %s" % exc, kind="auth")


def save_credentials(data):
    """Write credentials back atomically, keeping a pristine + rolling backup."""
    pristine = CRED_PATH + ".orig"
    if not os.path.exists(pristine):
        try:
            shutil.copy2(CRED_PATH, pristine)
        except OSError:
            pass
    try:
        shutil.copy2(CRED_PATH, CRED_PATH + ".bak")
    except OSError:
        pass

    tmp = CRED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CRED_PATH)


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)


def refresh_access_token(creds):
    """Exchange the refresh token for a fresh access token; persist the result."""
    oauth = creds.get("claudeAiOauth") or {}
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise UsageError("Sign in to Claude Code", kind="auth")

    try:
        data = _post_json(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
        )
    except urllib.error.HTTPError:
        # Refresh token rejected - usually means it was already rotated by
        # another client, so the CLI has to be signed in again.
        raise UsageError("Sign in to Claude Code", kind="auth")
    except urllib.error.URLError as exc:
        raise UsageError("Offline (%s)" % exc.reason, kind="network")

    access = data.get("access_token")
    if not access:
        raise UsageError("Sign in to Claude Code", kind="auth")

    oauth["accessToken"] = access
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    if data.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(data["expires_in"]) * 1000
    creds["claudeAiOauth"] = oauth

    try:
        save_credentials(creds)
    except OSError:
        pass  # still usable in memory for this run
    return access


def get_access_token(force_refresh=False):
    creds = load_credentials()
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt") or 0

    expired = (expires_at / 1000.0) - EXPIRY_SKEW <= time.time()
    if force_refresh or expired or not token:
        return refresh_access_token(creds)
    return token


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------

def _get_usage_raw(token):
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)


def fetch_usage():
    """Return normalised usage, refreshing the token once if the API says it is stale."""
    token = get_access_token()
    try:
        raw = _get_usage_raw(token)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            token = get_access_token(force_refresh=True)
            try:
                raw = _get_usage_raw(token)
            except urllib.error.HTTPError:
                raise UsageError("Sign in to Claude Code", kind="auth")
            except urllib.error.URLError as err:
                raise UsageError("Offline (%s)" % err.reason, kind="network")
        elif exc.code == 429:
            raise UsageError("Rate limited", kind="network")
        else:
            raise UsageError("API error %s" % exc.code, kind="error")
    except urllib.error.URLError as exc:
        raise UsageError("Offline (%s)" % exc.reason, kind="network")
    except ValueError as exc:
        raise UsageError("Bad response (%s)" % type(exc).__name__, kind="error")

    return normalise(raw)


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def _first(mapping, *keys):
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping[key]
    return None


def _as_percent(value):
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    # Some payloads express utilisation as a 0-1 fraction, others as 0-100.
    if isinstance(value, float) and 0.0 < num <= 1.0:
        num *= 100.0
    return max(0.0, min(100.0, num))


def _parse_reset(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _window(raw, *names):
    """Pull one rate-limit window out of the payload, tolerating shape drift."""
    node = _first(raw, *names)
    if node is None:
        return {"percent": None, "resets_at": None}
    if isinstance(node, (int, float)):
        return {"percent": _as_percent(node), "resets_at": None}
    return {
        "percent": _as_percent(
            _first(node, "utilization", "utilisation", "percent", "percent_used", "used")
        ),
        "resets_at": _parse_reset(_first(node, "resets_at", "resetsAt", "reset_at")),
    }


def normalise(raw):
    return {
        "session": _window(raw, "five_hour", "5h", "session"),
        "weekly": _window(raw, "seven_day", "7d", "week", "weekly"),
        "weekly_opus": _window(raw, "seven_day_opus"),
        "fetched_at": time.time(),
        "raw": raw,
    }


def humanise_reset(when):
    if when is None:
        return "unknown"
    delta = when - datetime.now(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    return "%dm" % max(1, minutes)


if __name__ == "__main__":
    # Diagnostic mode: print the raw payload so the display can be adapted if
    # the shape of this endpoint ever changes.
    try:
        result = fetch_usage()
    except UsageError as err:
        print("[%s] %s" % (err.kind, err))
        raise SystemExit(1)
    print(json.dumps(result["raw"], indent=2))
    print("-" * 40)
    for key in ("session", "weekly", "weekly_opus"):
        win = result[key]
        pct = "--" if win["percent"] is None else round(win["percent"])
        print("%-12s %5s%%  resets in %s" % (key, pct, humanise_reset(win["resets_at"])))
