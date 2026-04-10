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
import re
import sys

from api_client import create_guest_account, get_content, parse_file_tree
from browser_scraper import scrape_content
from downloader import download_files, update_dates_only


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
        "-o", "--output",
        default="/mnt/r/gofile",
        help="Base output directory (default: /mnt/r/gofile)",
    )
    parser.add_argument(
        "-p", "--password",
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
    args = parser.parse_args()

    content_id = parse_content_id(args.content)
    print(f"Content ID: {content_id}")

    folder_name = None
    files = []
    account_token = None

    # --- Try API first (unless forced to browser) ---
    if not args.force_browser:
        print("Creating guest account...")
        try:
            account_token = create_guest_account()
            print(f"  Token: {account_token[:8]}...")
        except Exception as e:
            print(f"  Failed to create guest account: {e}")
            print("  Falling back to browser scraping...")
            args.force_browser = True

    if not args.force_browser and account_token:
        print("Fetching content via API...")
        data, error = get_content(content_id, account_token, args.password)
        if data:
            folder_name = data.get("name", content_id)
            files = parse_file_tree(data)
            print(f"  Folder: {folder_name}")
            print(f"  Files found: {len(files)}")
        else:
            print(f"  API error: {error}")
            if "premium" in str(error).lower():
                print("  Premium required — falling back to browser scraping...")
                args.force_browser = True
            else:
                print("  Falling back to browser scraping...")
                args.force_browser = True

    # --- Browser fallback ---
    if args.force_browser or not files:
        print("Launching browser to scrape file listing...")
        try:
            folder_name, files = scrape_content(content_id, args.password)
            print(f"  Folder: {folder_name}")
            print(f"  Files found: {len(files)}")
        except Exception as e:
            print(f"  Browser scraping failed: {e}")
            sys.exit(1)

    if not files:
        print("No files found. The content may be empty, removed, or require a password.")
        sys.exit(1)

    # --- Determine output directory ---
    if not folder_name:
        folder_name = content_id
    output_dir = f"{args.output}/{folder_name}"
    print(f"\nOutput directory: {output_dir}")

    # --- Dates-only mode ---
    if args.dates_only:
        print("\nUpdating file dates only...")
        update_dates_only(files, output_dir)
        return

    # --- Download ---
    print()
    download_files(files, output_dir, account_token, args.dry_run)


if __name__ == "__main__":
    main()
