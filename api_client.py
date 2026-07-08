"""Gofile API client — guest account, website token, content listing."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests

API_BASE = "https://api.gofile.io"
WEBSITE_ORIGIN = "https://gofile.io"
TOKEN_SALT = "9844d94d963d30"

TOKEN_CACHE_PATH = Path.home() / ".gofile_dl_token.json"
TOKEN_MAX_AGE = 24 * 3600  # seconds

# Walking a folder tree fires one API call per subfolder. Folders with 20+
# subfolders will trip gofile's rate limit if fired back-to-back, so space the
# calls out and retry (with backoff) any that still come back rate-limited —
# a dropped subfolder silently omits all its files from the sync.
SUBFOLDER_FETCH_DELAY = 0.6  # seconds paused before each subfolder API call
SUBFOLDER_RETRY_DELAYS = (5, 10, 20, 40)  # backoff before each rate-limit retry


class GofileUnavailable(Exception):
    """Gofile can't be used right now (network down, rate-limited, outage,
    or auth broken in a way a retry won't fix). Callers should stop and
    retry later rather than continue hammering the API."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind  # network | rate_limited | unavailable | wrong_token


@dataclass
class ApiError:
    """A classified API failure. `kind` is the contract callers branch on;
    `message` is for display only."""

    kind: str  # network | rate_limited | unavailable | not_found | not_authorized | wrong_token | premium | other
    message: str

    def __str__(self) -> str:
        return self.message


def _classify_status(gf_status: str) -> str:
    """Map a gofile status string (e.g. 'error-notFound') to an ApiError kind."""
    s = gf_status.lower().replace("-", "")
    if "notfound" in s:
        return "not_found"
    if "wrongtoken" in s:
        return "wrong_token"
    if "notauthorized" in s or "password" in s:
        return "not_authorized"
    if "ratelimit" in s:
        return "rate_limited"
    if "premium" in s:
        return "premium"
    return "other"


def load_cached_token() -> tuple[str, float] | None:
    """Return (token, age_in_seconds) if a recent cached token exists, else None."""
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text())
        age = time.time() - data["created_at"]
        if age < TOKEN_MAX_AGE:
            return data["token"], age
    except Exception:
        pass
    return None


def save_token(token: str) -> None:
    """Cache a guest token with the current timestamp."""
    try:
        TOKEN_CACHE_PATH.write_text(
            json.dumps({"token": token, "created_at": time.time()})
        )
    except Exception:
        pass  # non-fatal


def clear_token_cache() -> None:
    """Delete the cached token (call when it proves invalid)."""
    try:
        TOKEN_CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def extract_firefox_token() -> str | None:
    """
    Extract the gofile account token from Firefox's localStorage.

    Gofile stores the token in localStorage (not cookies) under the key
    'accountsObject', as JSON that may be LZ4-compressed by Firefox.  The
    token value (20-50 alphanumeric chars) survives compression as a literal
    run, so a regex on the raw bytes is sufficient — no decompression needed.

    Copies the SQLite DB to a temp file first so it works while Firefox is running.
    """
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    profiles_dir = Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
    if not profiles_dir.is_dir():
        return None

    ls_glob = "*.default*/storage/default/https+++gofile.io/ls/data.sqlite"
    for ls_db in sorted(profiles_dir.glob(ls_glob)):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
                tmp_path = tmp.name
            shutil.copy2(ls_db, tmp_path)
            conn = sqlite3.connect(tmp_path)
            # Search both keys; appdataAccount tends to have the most recent token
            rows = conn.execute(
                "SELECT value FROM data WHERE key IN ('appdataAccount','accountsObject')"
                " ORDER BY CASE key WHEN 'appdataAccount' THEN 0 ELSE 1 END"
            ).fetchall()
            conn.close()
            for (raw,) in rows:
                if not isinstance(raw, bytes):
                    raw = str(raw).encode()
                # The token value is always a literal run in the LZ4 output.
                # The key "token" may be partially back-referenced so we try
                # progressively shorter suffixes of the key name.
                for pattern in (
                    rb'"token"\s*:\s*"([A-Za-z0-9]{20,60})"',
                    rb'token"\s*:\s*"([A-Za-z0-9]{20,60})"',
                    rb'oken"\s*:\s*"([A-Za-z0-9]{20,60})"',
                    rb'ken"\s*:\s*"([A-Za-z0-9]{20,60})"',
                ):
                    m = re.search(pattern, raw)
                    if m:
                        return m.group(1).decode()
        except Exception:
            continue
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
    return None


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0"
)


def _make_session(account_token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Origin": WEBSITE_ORIGIN,
            "Referer": f"{WEBSITE_ORIGIN}/",
            "X-BL": "en-US",
            "Authorization": f"Bearer {account_token}",
            "X-Website-Token": _website_token(account_token),
        }
    )
    s.cookies.set("accountToken", account_token, domain=".gofile.io")
    return s


def _website_token(account_token: str) -> str:
    time_slot = int(time.time() / 14400)
    raw = f"{DEFAULT_UA}::en-US::{account_token}::{time_slot}::{TOKEN_SALT}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _gofile_status(response) -> str | None:
    """Read gofile's status string from the response body (works on error responses too)."""
    try:
        return response.json().get("status") or None
    except Exception:
        return None


def _service_down_hint(response) -> str | None:
    """Return a clean message if the response looks like a service outage, else None."""
    if response.status_code in (502, 503, 504):
        return "Gofile API is unavailable — please retry later"
    try:
        text = response.text.lower()
        if "api is currently unavailable" in text or "cannot be used right now" in text:
            return "Gofile API is currently unavailable — please retry later"
    except Exception:
        pass
    return None


def create_guest_account(max_retries: int = 5) -> str:
    """Create a guest account and return its token. Retries with backoff on rate limit.

    Raises GofileUnavailable when gofile can't be used at all right now
    (network down, rate-limited after retries, service outage); RuntimeError
    for unexpected responses.
    """
    delay = 30
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"{API_BASE}/accounts", headers={"User-Agent": DEFAULT_UA}, timeout=30
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == max_retries - 1:
                raise GofileUnavailable(
                    f"cannot reach gofile ({type(e).__name__})", "network"
                ) from e
            print(f"  Connection problem ({type(e).__name__}), retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        except requests.exceptions.RequestException as e:
            raise RuntimeError(str(e)) from e
        if r.status_code == 429:
            if attempt == max_retries - 1:
                break
            print(f"  Rate-limited creating account, retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        hint = _service_down_hint(r)
        if hint:
            raise GofileUnavailable(hint, "unavailable")
        if not r.ok:
            raise RuntimeError(_gofile_status(r) or f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError("non-JSON response from API") from None
        if data.get("status") != "ok":
            raise RuntimeError(f"Failed to create guest account: {data}")
        return data["data"]["token"]
    raise GofileUnavailable(
        "rate-limited (429) creating guest account, even after retries", "rate_limited"
    )


def acquire_token() -> str | None:
    """
    Get a usable account token: disk cache -> Firefox localStorage -> new
    guest account. The disk cache is the single source of token truth —
    callers should re-call this (it's cheap) rather than hold a token across
    long-running work, so a refresh by anyone benefits everyone.

    Returns None when guest creation failed in a way where browser scraping
    might still work. Raises GofileUnavailable when gofile can't be used at
    all right now.
    """
    cached = load_cached_token()
    if cached:
        token, age = cached
        age_str = f"{int(age / 3600)}h" if age >= 3600 else f"{int(age / 60)}m"
        print(f"Using cached token ({age_str} old): {token[:8]}...")
        return token

    firefox_token = extract_firefox_token()
    if firefox_token:
        save_token(firefox_token)
        print(
            f"Using token from Firefox: {firefox_token[:8]}... (cached for future runs)"
        )
        return firefox_token

    print("Creating guest account...")
    try:
        token = create_guest_account()
    except GofileUnavailable:
        raise
    except Exception as e:
        print(f"  Failed to create guest account: {e}")
        return None
    save_token(token)
    print(f"  Token: {token[:8]}... (cached for future runs)")
    return token


def refresh_token(bad_token: str | None) -> str | None:
    """
    Replace a token the API rejected: clear the cache, try Firefox, then a
    new guest account. Saves the replacement to the cache so other callers
    (and later batch items) pick it up.

    Returns the fresh token, or None if no replacement could be obtained.
    Raises GofileUnavailable when gofile can't be used at all right now.
    """
    clear_token_cache()
    fresh = extract_firefox_token()
    if fresh and fresh != bad_token:
        save_token(fresh)
        print(f"  Fresh token from Firefox: {fresh[:8]}...")
        return fresh
    if fresh == bad_token:
        print("  Firefox has same token — creating new guest account...")
    try:
        token = create_guest_account()
    except GofileUnavailable:
        raise
    except Exception as e:
        print(f"  Failed to create new account: {e}")
        return None
    save_token(token)
    print(f"  New guest token: {token[:8]}...")
    return token


def get_content(
    content_id: str, account_token: str, password: str | None = None
) -> tuple[dict | None, ApiError | None]:
    """
    Fetch folder/file listing from the API.

    Returns (data_dict, None) on success, (None, ApiError) on failure.
    Classification happens here, where the HTTP status and gofile status
    string are both in hand — callers branch on ApiError.kind, not prose.
    """
    session = _make_session(account_token)
    params = {}
    if password:
        params["password"] = hashlib.sha256(password.encode()).hexdigest()

    try:
        r = session.get(f"{API_BASE}/contents/{content_id}", params=params, timeout=60)
    except (requests.ConnectionError, requests.Timeout) as e:
        return None, ApiError("network", f"cannot reach gofile ({type(e).__name__})")
    except requests.exceptions.RequestException as e:
        return None, ApiError("other", str(e))

    try:
        body = r.json()
    except ValueError:
        body = None

    gf_status = (body or {}).get("status")
    if gf_status == "ok":
        return body["data"], None
    if gf_status:
        return None, ApiError(_classify_status(gf_status), gf_status)
    if r.status_code == 429:
        return None, ApiError("rate_limited", "HTTP 429 Too Many Requests")
    hint = _service_down_hint(r)
    if hint:
        return None, ApiError("unavailable", hint)
    if not r.ok:
        return None, ApiError("other", f"HTTP {r.status_code}")
    return None, ApiError("other", "non-JSON response from API")


def parse_file_tree(
    data: dict, account_token: str | None = None, password: str | None = None
) -> tuple[list[dict], list[dict]]:
    """
    Recursively flatten API content data into a list of file dicts and folder dicts.

    Files have: name, path (relative to root folder), link, size, create_time.
    Folders have: name, path (relative to root folder), create_time.
    The root folder name is NOT included in paths (it's used as the output dir).

    When a subfolder has a code but no children, fetches its contents via the API.

    Returns (files, folders).
    """
    files = []
    folders = []
    # Start with empty prefix — skip root folder name
    children = data.get("children", {})
    if isinstance(children, dict):
        children = children.values()
    for child in children:
        _walk(child, "", files, folders, account_token, password)
    _disambiguate_paths(files)
    return files, folders


def _disambiguate_paths(files: list[dict]) -> None:
    """
    When multiple files in the same folder share a name, gofile lets both
    coexist (different IDs). Writing them to the same local path makes them
    overwrite each other on every sync. Append a short ID-derived suffix to
    every member of a colliding group so the assignment is stable across
    runs regardless of API ordering.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        groups[f["path"]].append(f)
    for group in groups.values():
        if len(group) <= 1:
            continue
        for f in group:
            f["path"] = _suffix_path(f["path"], _short_id(f))


def _short_id(file: dict) -> str:
    fid = file.get("id") or ""
    if not fid:
        m = re.search(r"/([a-f0-9-]{36})/", file.get("link") or "")
        if m:
            fid = m.group(1)
    return fid[:8] if fid else "dup"


def _suffix_path(path: str, suffix: str) -> str:
    """Insert ' (suffix)' before the extension. Path uses forward slashes."""
    slash = path.rfind("/")
    dirname = path[: slash + 1] if slash >= 0 else ""
    basename = path[slash + 1 :] if slash >= 0 else path
    dot = basename.rfind(".")
    if dot > 0:
        return f"{dirname}{basename[:dot]} ({suffix}){basename[dot:]}"
    return f"{dirname}{basename} ({suffix})"


def _fetch_subfolder(
    folder_code: str, account_token: str, password: str | None
) -> tuple[dict | None, ApiError | None]:
    """
    Fetch a subfolder's contents, throttling and retrying on rate-limit.

    A short pause precedes every call so a folder with many subfolders doesn't
    fire requests fast enough to trip the limit in the first place. If a call
    still comes back rate-limited, it's retried with backoff — dropping the
    subfolder would silently omit all its files from an otherwise "successful"
    sync. If every retry is exhausted the IP is being throttled/temp-banned;
    raise GofileUnavailable so the caller aborts rather than write a partial
    tree that looks complete.
    """
    time.sleep(SUBFOLDER_FETCH_DELAY)
    data, err = get_content(folder_code, account_token, password)
    for delay in SUBFOLDER_RETRY_DELAYS:
        if err is None or err.kind != "rate_limited":
            return data, err
        print(f"    Rate-limited on subfolder — retrying in {delay}s...")
        time.sleep(delay)
        data, err = get_content(folder_code, account_token, password)
    if err is not None and err.kind == "rate_limited":
        raise GofileUnavailable(
            "gofile rate-limited subfolder fetches even after retries "
            "(the IP is likely temp-banned) — stopping to avoid a partial sync",
            "rate_limited",
        )
    return data, err


def _walk(
    node: dict,
    prefix: str,
    files: list[dict],
    folders: list[dict],
    account_token: str | None = None,
    password: str | None = None,
):
    if node.get("type") == "file":
        files.append(
            {
                "id": node.get("id"),
                "name": node["name"],
                "path": f"{prefix}{node['name']}" if prefix else node["name"],
                "link": node.get("link"),
                "size": node.get("size"),
                "create_time": node.get("createTime"),
            }
        )
        return

    # It's a folder
    folder_name = node.get("name", "")

    # Skip folders the guest account can't access (owner has them set to private).
    # The API returns the metadata but no children; walking would silently produce
    # an empty local directory.
    if node.get("canAccess") is False:
        print(
            f"  Skipping inaccessible subfolder: {folder_name or '(unnamed)'} "
            f"(owner has not made it public)"
        )
        return

    current_prefix = (
        f"{prefix}{folder_name}/"
        if folder_name and prefix != ""
        else (f"{folder_name}/" if folder_name else prefix)
    )

    # Record this subfolder (skip if it's a nameless/root-level container)
    if folder_name:
        folder_path = current_prefix.rstrip("/")
        folders.append(
            {
                "name": folder_name,
                "path": folder_path,
                "create_time": node.get("createTime"),
            }
        )

    children = node.get("children", {})
    if isinstance(children, dict):
        children = children.values()
    children = list(children)

    # If folder has no children but has a code, fetch its contents via the API
    if not children and node.get("code") and account_token:
        folder_code = node["code"]
        print(f"  Fetching subfolder: {folder_name} ({folder_code})...")
        sub_data, err = _fetch_subfolder(folder_code, account_token, password)
        if sub_data:
            children = sub_data.get("children", {})
            if isinstance(children, dict):
                children = children.values()
            children = list(children)
        else:
            print(f"  Warning: could not fetch subfolder {folder_name}: {err}")

    for child in children:
        _walk(child, current_prefix, files, folders, account_token, password)
