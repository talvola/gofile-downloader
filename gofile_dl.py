#!/usr/bin/env python3
"""
gofile_dl — Download and sync files from gofile.io folders.

Usage:
    python gofile_dl.py PjUhkl
    python gofile_dl.py https://gofile.io/d/PjUhkl
    python gofile_dl.py PjUhkl --output /mnt/r/gofile
    python gofile_dl.py PjUhkl --dry-run

Exit code is 1 when the content can't be fetched or any file fails to
download (a real download attempt, not --dry-run reporting).
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
    GofileUnavailable,
    acquire_token,
    get_content,
    parse_file_tree,
    refresh_token,
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


def run(
    content_id: str,
    output_base: str = DEFAULT_OUTPUT,
    password: str | None = None,
    dry_run: bool = False,
    dates_only: bool = False,
    force_browser: bool = False,
) -> bool:
    """Download a single gofile content ID. Returns True on success.

    The account token comes from api_client.acquire_token() (disk cache ->
    Firefox -> guest account); the disk cache is the single source of token
    truth, so batch callers don't pass a token — a refresh during one item
    is picked up by the next.

    Raises GofileUnavailable when gofile can't be used at all right now
    (network down, rate-limited, outage, or auth systemically broken) so
    batch callers can abort instead of grinding through every remaining ID.
    """
    print(f"Content ID: {content_id}")

    folder_name = None
    files = []
    folders = []
    account_token = None
    api_succeeded = False

    # --- Try API first (unless forced to browser) ---
    if not force_browser:
        account_token = acquire_token()
        if account_token is None:
            print("  Falling back to browser scraping...")
            force_browser = True

    if not force_browser:
        print("Fetching content via API...")
        data, error = get_content(content_id, account_token, password)
        if error is not None and error.kind == "wrong_token":
            # e.g. invalidated during a service outage: refresh and retry once
            print("  Token rejected (wrong/expired) — fetching fresh token...")
            fresh = refresh_token(account_token)
            if fresh:
                account_token = fresh
                data, error = get_content(content_id, account_token, password)

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
            if error.kind == "not_found":
                print(
                    f"  Content '{content_id}' does not exist — it may have been deleted or expired."
                )
                return False
            if error.kind == "not_authorized":
                print(
                    "  Content is not accessible with this account — it may be "
                    "private or password-protected (try --password)."
                )
                return False
            if error.kind == "wrong_token":
                # Still rejected after a refresh: systemic auth breakage, most
                # likely a rotated website-token salt. Hammering on won't help.
                raise GofileUnavailable(
                    "token rejected even after refresh — gofile may have rotated "
                    "the website-token salt (re-extract from wt.obf.js, see CLAUDE.md)",
                    "wrong_token",
                )
            if error.kind in ("rate_limited", "unavailable", "network"):
                # Global conditions: the browser can't reach gofile either,
                # and retrying other IDs right now only makes it worse.
                raise GofileUnavailable(str(error), error.kind)
            if error.kind == "premium":
                print(
                    "  Premium required — falling back to browser scraping...\n"
                    "  (If this content opens fine in a normal browser, the "
                    "website-token salt has likely rotated — see CLAUDE.md "
                    "'Website token' to re-extract it from wt.obf.js.)"
                )
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
    # Seed the token cache with the user's token — but only when this run will
    # actually exercise it; --force-browser would cache it unvalidated.
    if args.token and not args.force_browser:
        save_token(args.token)
        print(f"Using provided token: {args.token[:8]}... (cached for future runs)")
    try:
        ok = run(
            content_id,
            output_base=args.output,
            password=args.password,
            dry_run=args.dry_run,
            dates_only=args.dates_only,
            force_browser=args.force_browser,
        )
    except GofileUnavailable as e:
        print(f"\nGofile unavailable: {e}")
        print("Please retry later.")
        sys.exit(1)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
