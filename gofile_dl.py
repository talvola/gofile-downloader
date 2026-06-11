#!/usr/bin/env python3
"""
gofile_dl — Download and sync files from gofile.io folders.

Usage:
    python gofile_dl.py PjUhkl
    python gofile_dl.py https://gofile.io/d/PjUhkl
    python gofile_dl.py PjUhkl --output /mnt/r/gofile
    python gofile_dl.py PjUhkl --dry-run
"""

import argparse
import io
import os
import re
import sys

# Ensure stdout/stderr handle Unicode on Windows (cp1252 can't print some chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from api_client import (
    clear_token_cache,
    create_guest_account,
    extract_firefox_token,
    get_content,
    load_cached_token,
    parse_file_tree,
    save_token,
)
from browser_scraper import scrape_content
from downloader import download_files, update_dates_only, find_orphans

# The R: drive is /mnt/r under WSL/Linux and R:\ on native Windows
DEFAULT_OUTPUT = "R:/gofile" if os.name == "nt" else "/mnt/r/gofile"


def parse_content_id(input_str: str) -> str:
    """Extract content ID from a URL or bare ID string."""
    # Full URL: https://gofile.io/d/PjUhkl
    m = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", input_str)
    if m:
        return m.group(1)
    # Bare ID
    if re.match(r"^[A-Za-z0-9]+$", input_str):
        return input_str
    raise ValueError(f"Cannot parse content ID from: {input_str}")


def _normalize_error(error) -> str:
    return str(error).lower().replace("-", "")


def _looks_like_connection_error(error) -> bool:
    err = str(error).lower()
    return any(
        marker in err
        for marker in ("connectionpool", "connection aborted", "timed out", "timeout")
    )


def _acquire_token() -> tuple[str | None, bool]:
    """Get an account token: cached -> Firefox -> new guest account.

    Returns (token, fall_back_to_browser). token is None when acquisition
    failed; fall_back_to_browser says whether browser scraping is worth trying.
    """
    cached = load_cached_token()
    if cached:
        token, age = cached
        age_str = f"{int(age / 3600)}h" if age >= 3600 else f"{int(age / 60)}m"
        print(f"Using cached token ({age_str} old): {token[:8]}...")
        return token, False

    firefox_token = extract_firefox_token()
    if firefox_token:
        save_token(firefox_token)
        print(
            f"Using token from Firefox: {firefox_token[:8]}... (cached for future runs)"
        )
        return firefox_token, False

    print("Creating guest account...")
    try:
        token = create_guest_account()
    except Exception as e:
        msg = str(e)
        print(f"  Failed to create guest account: {msg}")
        if "unavailable" in msg.lower() or "retry later" in msg.lower():
            return None, False
        if "ratelimit" in _normalize_error(msg):
            print("  Rate limited — please wait a few minutes and retry.")
            return None, False
        print("  Falling back to browser scraping...")
        return None, True
    save_token(token)
    print(f"  Token: {token[:8]}... (cached for future runs)")
    return token, False


def run(
    content_id: str,
    output_base: str = DEFAULT_OUTPUT,
    password: str | None = None,
    dry_run: bool = False,
    dates_only: bool = False,
    force_browser: bool = False,
    account_token: str | None = None,
) -> bool:
    """Download a single gofile content ID. Returns True on success.

    Pass account_token to reuse an existing account (avoids gofile's
    rate limit on account creation when downloading many IDs in one run).
    """
    print(f"Content ID: {content_id}")

    folder_name = None
    files = []
    folders = []
    api_succeeded = False

    # --- Acquire a token unless the caller supplied one ---
    if not force_browser and not account_token:
        account_token, fall_back = _acquire_token()
        if account_token is None and not fall_back:
            return False
        force_browser = force_browser or fall_back

    # --- Try API first (unless forced to browser) ---
    if not force_browser and account_token:
        print("Fetching content via API...")
        data, error = None, None
        for _attempt in range(2):
            data, error = get_content(content_id, account_token, password)
            if data:
                break
            # Wrong token (e.g. invalidated during a service outage): refresh and retry once
            if "wrongtoken" in _normalize_error(error) and _attempt == 0:
                bad_token = account_token
                print("  Token rejected (wrong/expired) — fetching fresh token...")
                clear_token_cache()
                fresh = extract_firefox_token()
                if fresh and fresh != bad_token:
                    account_token = fresh
                    save_token(fresh)
                    print(f"  Fresh token from Firefox: {fresh[:8]}...")
                else:
                    if fresh == bad_token:
                        print(
                            "  Firefox has same token — creating new guest account..."
                        )
                    try:
                        account_token = create_guest_account()
                        save_token(account_token)
                        print(f"  New guest token: {account_token[:8]}...")
                    except Exception as e:
                        print(f"  Failed to create new account: {e}")
                        break
                continue
            break

        if data:
            api_succeeded = True
            folder_name = data.get("name", content_id)
            files, folders = parse_file_tree(data, account_token, password)
            print(f"  Folder: {folder_name}")
            print(f"  Files found: {len(files)}")
            if folders:
                print(f"  Subfolders found: {len(folders)}")
        else:
            print(f"  API error: {error}")
            err = _normalize_error(error)
            if "notfound" in err:
                print(
                    f"  Content '{content_id}' does not exist — it may have been deleted or expired."
                )
                return False
            if "notauthorized" in err:
                print(
                    "  Token rejected — clearing cached token. Retry to get a fresh one."
                )
                clear_token_cache()
                return False
            if "ratelimit" in err:
                print("  Rate limited — please wait a few minutes and retry.")
                return False
            if (
                "unavailable" in str(error).lower()
                or "retry later" in str(error).lower()
            ):
                return False
            if _looks_like_connection_error(error):
                # Network problem — the browser can't reach gofile either
                return False
            if "premium" in str(error).lower():
                print("  Premium required — falling back to browser scraping...")
            else:
                print("  Falling back to browser scraping...")
            force_browser = True

    # --- Browser fallback (only when API failed; falling back on a successful
    # API response with 0 files makes the scraper grab page chrome for private
    # content, which masquerades as a successful download of garbage HTML) ---
    if force_browser and not api_succeeded:
        print("Launching browser to scrape file listing...")
        try:
            folder_name, files, folders = scrape_content(content_id, password)
            print(f"  Folder: {folder_name}")
            print(f"  Files found: {len(files)}")
            if folders:
                print(f"  Subfolders found: {len(folders)}")
        except Exception as e:
            print(f"  Browser scraping failed: {e}")
            return False

    if not files:
        if api_succeeded:
            print(
                "\nNo accessible files found. The content's subfolders may be "
                "set to private — only the owner can change that."
            )
        else:
            print(
                "\nNo files found. The content may be empty, removed, or require a password."
            )
        return False

    # --- Determine output directory ---
    if not folder_name or folder_name == "root":
        if folder_name == "root":
            print(f"  Folder name is 'root' — using content ID '{content_id}' instead")
        folder_name = content_id
    output_dir = f"{output_base}/{folder_name}"
    print(f"\nOutput directory: {output_dir}")

    # --- Dates-only mode ---
    if dates_only:
        print("\nUpdating file dates only...")
        update_dates_only(files, folders, output_dir)
        return True

    # --- Download ---
    print()
    stats = download_files(files, folders, output_dir, account_token, dry_run)

    # --- Check for orphaned local files ---
    print("\nChecking for orphaned local files...")
    orphans = find_orphans(files, folders, output_dir)
    if orphans:
        print(f"Found {len(orphans)} local file(s) not present on the server:")
        for o in orphans:
            print(f"  ORPHAN: {o}")
        print("\nThese files may have been deleted, moved, or renamed on the server.")
        print(
            "They have NOT been deleted locally — review and remove manually if desired."
        )
    else:
        print("No orphaned files found — local copy matches server.")

    return stats.get("errors", 0) == 0


def main():
    parser = argparse.ArgumentParser(
        description="Download and sync files from gofile.io",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "content",
        help="Gofile content ID or URL (e.g. PjUhkl or https://gofile.io/d/PjUhkl)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Base output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=None,
        help="Password for protected content",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without downloading",
    )
    parser.add_argument(
        "--dates-only",
        action="store_true",
        help="Only update file dates, don't download anything",
    )
    parser.add_argument(
        "--force-browser",
        action="store_true",
        help="Skip API and go directly to browser scraping",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="Use this account token instead of creating a guest account",
    )
    args = parser.parse_args()

    content_id = parse_content_id(args.content)
    if args.token:
        save_token(args.token)
        print(f"Using provided token: {args.token[:8]}... (cached for future runs)")
    ok = run(
        content_id,
        output_base=args.output,
        password=args.password,
        dry_run=args.dry_run,
        dates_only=args.dates_only,
        force_browser=args.force_browser,
        account_token=args.token,
    )
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
