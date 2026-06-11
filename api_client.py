"""Gofile API client — guest account, website token, content listing."""

import hashlib
import time
import requests

API_BASE = "https://api.gofile.io"
WEBSITE_ORIGIN = "https://gofile.io"
TOKEN_SALT = "5d4f7g8sd45fsd"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _make_session(account_token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Origin": WEBSITE_ORIGIN,
        "Referer": f"{WEBSITE_ORIGIN}/",
        "X-BL": "en-US",
        "Authorization": f"Bearer {account_token}",
        "X-Website-Token": _website_token(account_token),
    })
    s.cookies.set("accountToken", account_token, domain=".gofile.io")
    return s


def _website_token(account_token: str) -> str:
    time_slot = int(time.time() / 14400)
    raw = f"{DEFAULT_UA}::en-US::{account_token}::{time_slot}::{TOKEN_SALT}"
    return hashlib.sha256(raw.encode()).hexdigest()


def create_guest_account(max_retries: int = 5) -> str:
    """Create a guest account and return its token. Retries with backoff on rate limit."""
    delay = 30
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"{API_BASE}/accounts", headers={"User-Agent": DEFAULT_UA}, timeout=30
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == max_retries - 1:
                raise
            print(f"  Connection problem ({type(e).__name__}), retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        if r.status_code == 429:
            if attempt == max_retries - 1:
                break
            print(f"  Rate-limited creating account, retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"Failed to create guest account: {data}")
        return data["data"]["token"]
    raise RuntimeError("Failed to create guest account: rate-limited (429) after retries")


def get_content(content_id: str, account_token: str, password: str | None = None):
    """
    Fetch folder/file listing from the API.

    Returns (data_dict, None) on success.
    Returns (None, error_string) on failure (e.g. 'error-notPremium').
    """
    session = _make_session(account_token)
    params = {}
    if password:
        params["password"] = hashlib.sha256(password.encode()).hexdigest()

    r = session.get(f"{API_BASE}/contents/{content_id}", params=params, timeout=60)
    r.raise_for_status()
    body = r.json()

    if body.get("status") == "ok":
        return body["data"], None
    return None, body.get("status", "unknown-error")


def parse_file_tree(data: dict) -> list[dict]:
    """
    Recursively flatten API content data into a list of file dicts.
    Each dict has: name, path (relative to root folder), link, size, create_time.
    The root folder name is NOT included in paths (it's used as the output dir).
    """
    files = []
    # Start with empty prefix — skip root folder name
    children = data.get("children", {})
    if isinstance(children, dict):
        children = children.values()
    for child in children:
        _walk(child, "", files)
    return files


def _walk(node: dict, prefix: str, out: list[dict]):
    if node.get("type") == "file":
        out.append({
            "name": node["name"],
            "path": f"{prefix}{node['name']}" if prefix else node["name"],
            "link": node.get("link"),
            "size": node.get("size"),
            "create_time": node.get("createTime"),
        })
        return

    # It's a folder
    folder_name = node.get("name", "")
    current_prefix = f"{prefix}{folder_name}/" if folder_name and prefix != "" else (
        f"{folder_name}/" if folder_name else prefix
    )

    children = node.get("children", {})
    if isinstance(children, dict):
        children = children.values()
    for child in children:
        _walk(child, current_prefix, out)
