"""Gofile API client — guest account, website token, content listing."""

import hashlib
import re
import time
from collections import defaultdict

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


def create_guest_account() -> str:
    """Create a guest account and return its token."""
    r = requests.post(f"{API_BASE}/accounts", headers={"User-Agent": DEFAULT_UA})
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to create guest account: {data}")
    return data["data"]["token"]


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

    r = session.get(f"{API_BASE}/contents/{content_id}", params=params)
    r.raise_for_status()
    body = r.json()

    if body.get("status") == "ok":
        return body["data"], None
    return None, body.get("status", "unknown-error")


def parse_file_tree(data: dict, account_token: str | None = None, password: str | None = None) -> tuple[list[dict], list[dict]]:
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


def _walk(node: dict, prefix: str, files: list[dict], folders: list[dict],
          account_token: str | None = None, password: str | None = None):
    if node.get("type") == "file":
        files.append({
            "id": node.get("id"),
            "name": node["name"],
            "path": f"{prefix}{node['name']}" if prefix else node["name"],
            "link": node.get("link"),
            "size": node.get("size"),
            "create_time": node.get("createTime"),
        })
        return

    # It's a folder
    folder_name = node.get("name", "")

    # Skip folders the guest account can't access (owner has them set to private).
    # The API returns the metadata but no children; walking would silently produce
    # an empty local directory.
    if node.get("canAccess") is False:
        print(f"  Skipping inaccessible subfolder: {folder_name or '(unnamed)'} "
              f"(owner has not made it public)")
        return

    current_prefix = f"{prefix}{folder_name}/" if folder_name and prefix != "" else (
        f"{folder_name}/" if folder_name else prefix
    )

    # Record this subfolder (skip if it's a nameless/root-level container)
    if folder_name:
        folder_path = current_prefix.rstrip("/")
        folders.append({
            "name": folder_name,
            "path": folder_path,
            "create_time": node.get("createTime"),
        })

    children = node.get("children", {})
    if isinstance(children, dict):
        children = children.values()
    children = list(children)

    # If folder has no children but has a code, fetch its contents via the API
    if not children and node.get("code") and account_token:
        folder_code = node["code"]
        print(f"  Fetching subfolder: {folder_name} ({folder_code})...")
        sub_data, err = get_content(folder_code, account_token, password)
        if sub_data:
            children = sub_data.get("children", {})
            if isinstance(children, dict):
                children = children.values()
            children = list(children)
        else:
            print(f"  Warning: could not fetch subfolder {folder_name}: {err}")

    for child in children:
        _walk(child, current_prefix, files, folders, account_token, password)
