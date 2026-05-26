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
        default="/mnt/r/gofile",
        help="Base output directory (default: /mnt/r/gofile)",
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
    print(f"Content ID: {content_id}")

    folder_name = None
    files = []
    folders = []
    account_token = None
    api_succeeded = False

    # --- Try API first (unless forced to browser) ---
    if not args.force_browser:
        if args.token:
            account_token = args.token
            save_token(account_token)
            print(
                f"Using provided token: {account_token[:8]}... (cached for future runs)"
            )
        else:
            cached = load_cached_token()
            if cached:
                account_token, age = cached
                age_str = f"{int(age / 3600)}h" if age >= 3600 else f"{int(age / 60)}m"
                print(f"Using cached token ({age_str} old): {account_token[:8]}...")
            else:
                firefox_token = extract_firefox_token()
                if firefox_token:
                    account_token = firefox_token
                    save_token(account_token)
                    print(
                        f"Using token from Firefox: {account_token[:8]}... (cached for future runs)"
                    )
                else:
                    print("Creating guest account...")
                    try:
                        account_token = create_guest_account()
                        save_token(account_token)
                        print(
                            f"  Token: {account_token[:8]}... (cached for future runs)"
                        )
                    except Exception as e:
                        msg = str(e)
                        print(f"  Failed to create guest account: {msg}")
                        if "unavailable" in msg.lower() or "retry later" in msg.lower():
                            sys.exit(1)
                        if "ratelimit" in msg.lower().replace("-", ""):
                            print(
                                "  Rate limited — please wait a few minutes and retry."
                            )
                            sys.exit(1)
                        print("  Falling back to browser scraping...")
                        args.force_browser = True

    if not args.force_browser and account_token:
        print("Fetching content via API...")
        data, error = None, None
        for _attempt in range(2):
            data, error = get_content(content_id, account_token, args.password)
            if data:
                break
            # Wrong token (e.g. invalidated during a service outage): refresh and retry once
            if "wrongtoken" in str(error).lower().replace("-", "") and _attempt == 0:
                bad_token = account_token
                print(f"  Token rejected (wrong/expired) — fetching fresh token...")
                clear_token_cache()
                fresh = extract_firefox_token()
                if fresh and fresh != bad_token:
                    account_token = fresh
                    save_token(fresh)
                    print(f"  Fresh token from Firefox: {fresh[:8]}...")
                else:
                    if fresh == bad_token:
                        print(
                            f"  Firefox has same token — creating new guest account..."
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
            files, folders = parse_file_tree(data, account_token, args.password)
            print(f"  Folder: {folder_name}")
            print(f"  Files found: {len(files)}")
            if folders:
                print(f"  Subfolders found: {len(folders)}")
        else:
            print(f"  API error: {error}")
            if "notfound" in str(error).lower().replace("-", ""):
                print(
                    f"  Content '{content_id}' does not exist — it may have been deleted or expired."
                )
                sys.exit(1)
            if "notauthorized" in str(error).lower().replace("-", ""):
                print(
                    "  Token rejected — clearing cached token. Retry to get a fresh one."
                )
                clear_token_cache()
                sys.exit(1)
            if "ratelimit" in str(error).lower().replace("-", ""):
                print("  Rate limited — please wait a few minutes and retry.")
                sys.exit(1)
            if (
                "unavailable" in str(error).lower()
                or "retry later" in str(error).lower()
            ):
                sys.exit(1)
            if "premium" in str(error).lower():
                print("  Premium required — falling back to browser scraping...")
                args.force_browser = True
            else:
                print("  Falling back to browser scraping...")
                args.force_browser = True

    # --- Browser fallback (only when API failed; falling back on a successful
    # API response with 0 files makes the scraper grab page chrome for private
    # content, which masquerades as a successful download of garbage HTML) ---
    if args.force_browser and not api_succeeded:
        print("Launching browser to scrape file listing...")
        try:
            folder_name, files, folders = scrape_content(content_id, args.password)
            print(f"  Folder: {folder_name}")
            print(f"  Files found: {len(files)}")
            if folders:
                print(f"  Subfolders found: {len(folders)}")
        except Exception as e:
            print(f"  Browser scraping failed: {e}")
            sys.exit(1)

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
        sys.exit(1)

    # --- Determine output directory ---
    if not folder_name or folder_name == "root":
        if folder_name == "root":
            print(f"  Folder name is 'root' — using content ID '{content_id}' instead")
        folder_name = content_id
    output_dir = f"{args.output}/{folder_name}"
    print(f"\nOutput directory: {output_dir}")

    # --- Dates-only mode ---
    if args.dates_only:
        print("\nUpdating file dates only...")
        update_dates_only(files, folders, output_dir)
        return

    # --- Download ---
    print()
    download_files(files, folders, output_dir, account_token, args.dry_run)

    # --- Check for orphaned local files ---
    print("\nChecking for orphaned local files...")
    orphans = find_orphans(files, folders, output_dir)
    if orphans:
        print(f"Found {len(orphans)} local file(s) not present on the server:")
        for o in orphans:
            print(f"  ORPHAN: {o}")
        print(f"\nThese files may have been deleted, moved, or renamed on the server.")
        print(
            f"They have NOT been deleted locally — review and remove manually if desired."
        )
    else:
        print("No orphaned files found — local copy matches server.")


if __name__ == "__main__":
    main()
