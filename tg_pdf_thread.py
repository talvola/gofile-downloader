#!/usr/bin/env python3
"""
tg_pdf_thread — Find gofile links in the 4chan /tg/ PDF Share Thread and download them.

Finds the current PDF Share Thread in the /tg/ catalog (or takes a thread
URL/number), extracts gofile content IDs from posts (including obfuscated
forms like "g0f1le /d/3tDCbP"), optionally follows the "Previous thread"
link in the OP, and runs the gofile downloader on each ID.

Usage:
    python tg_pdf_thread.py                      # auto-find thread, also do previous thread
    python tg_pdf_thread.py 98169921             # specific thread number
    python tg_pdf_thread.py https://boards.4chan.org/tg/thread/98169921/pdf-share-thread
    python tg_pdf_thread.py --depth 1            # current thread only
    python tg_pdf_thread.py --depth 4            # follow previous-thread links 3 times
    python tg_pdf_thread.py --list-only          # just show the gofile IDs found
    python tg_pdf_thread.py --dry-run            # fetch listings but don't download
"""

import argparse
import html
import re
import sys
import time

import requests

from api_client import create_guest_account, load_cached_token, save_token
from gofile_dl import DEFAULT_OUTPUT
from gofile_dl import run as download_gofile

API_BASE = "https://a.4cdn.org"
BOARD = "tg"
THREAD_TITLE = "pdf share thread"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# gofile content IDs are 6 alphanumeric chars after /d/, in both plain
# (gofile.io/d/AS3dnB) and obfuscated (g0f1le /d/3tDCbP) posts.
# The (?![A-Za-z0-9]) guard rejects longer IDs from other services
# (e.g. Google Drive's /d/<33 chars>).
GOFILE_ID_RE = re.compile(r"/d/\s*([A-Za-z0-9]{6})(?![A-Za-z0-9])")

# Cross-thread quotelinks in post HTML look like:
#   <a href="/tg/thread/98130506#p98130506" class="quotelink">&gt;&gt;98130506</a>
PREV_THREAD_RE = re.compile(r'href="/tg/thread/(\d+)')


def _get_json(url: str):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_thread_arg(input_str: str) -> int:
    """Extract a thread number from a URL or bare number."""
    m = re.search(r"/thread/(\d+)", input_str)
    if m:
        return int(m.group(1))
    if input_str.isdigit():
        return int(input_str)
    raise ValueError(f"Cannot parse thread number from: {input_str}")


def find_pdf_share_thread() -> int:
    """Search the /tg/ catalog (then recently archived threads) for the newest PDF Share Thread."""
    catalog = _get_json(f"{API_BASE}/{BOARD}/catalog.json")
    matches = []
    for page in catalog:
        for thread in page.get("threads", []):
            subject = html.unescape(thread.get("sub", "")).lower()
            if THREAD_TITLE in subject:
                matches.append(thread["no"])
    if matches:
        # Newest thread = highest post number
        return max(matches)

    # Between threads (old one archived, successor not posted yet) the catalog
    # has no match — scan the newest archived threads for it.
    print("Not in the live catalog — checking recently archived threads...")
    archive = _get_json(f"{API_BASE}/{BOARD}/archive.json")
    for thread_no in reversed(archive[-30:]):
        posts = fetch_thread(thread_no)
        if posts:
            subject = html.unescape(posts[0].get("sub", "")).lower()
            if THREAD_TITLE in subject:
                return thread_no
        time.sleep(1)
    raise RuntimeError(
        f"No '{THREAD_TITLE}' found in /{BOARD}/ catalog or recent archive"
    )


def fetch_thread(thread_no: int) -> list[dict]:
    """Fetch all posts in a thread. Returns [] if the thread is gone."""
    try:
        data = _get_json(f"{API_BASE}/{BOARD}/thread/{thread_no}.json")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise
    return data.get("posts", [])


def _clean_comment(com: str) -> str:
    """Convert post HTML to plain text suitable for ID extraction."""
    # 4chan inserts <wbr> mid-word in long strings — remove so IDs aren't split
    text = com.replace("<wbr>", "")
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def extract_gofile_ids(posts: list[dict]) -> list[tuple[str, int]]:
    """Return [(content_id, post_no), ...] in thread order, deduplicated."""
    seen = set()
    found = []
    for post in posts:
        com = post.get("com")
        if not com:
            continue
        text = _clean_comment(com)
        for m in GOFILE_ID_RE.finditer(text):
            content_id = m.group(1)
            if content_id not in seen:
                seen.add(content_id)
                found.append((content_id, post["no"]))
    return found


def find_previous_thread(posts: list[dict]) -> int | None:
    """Find the previous-thread link in the OP, if any."""
    if not posts:
        return None
    op_com = posts[0].get("com", "")
    m = PREV_THREAD_RE.search(op_com)
    return int(m.group(1)) if m else None


def main():
    parser = argparse.ArgumentParser(
        description="Download gofile links from the /tg/ PDF Share Thread",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "thread",
        nargs="?",
        default=None,
        help="Thread URL or number (default: search the catalog for the PDF Share Thread)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Base output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="How many threads to process, following previous-thread links (default: 2 = current + previous)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list the gofile IDs found, don't touch gofile at all",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch gofile listings but don't download files",
    )
    args = parser.parse_args()

    # --- Resolve starting thread ---
    if args.thread:
        thread_no = parse_thread_arg(args.thread)
    else:
        print(f"Searching /{BOARD}/ catalog for the PDF Share Thread...")
        thread_no = find_pdf_share_thread()
    print(f"Starting thread: https://boards.4chan.org/{BOARD}/thread/{thread_no}")

    # --- Walk threads, collecting IDs (oldest thread first so downloads are chronological) ---
    all_ids: list[tuple[str, int, int]] = []  # (content_id, post_no, thread_no)
    seen_ids = set()
    visited_threads = set()

    for _ in range(max(1, args.depth)):
        if thread_no in visited_threads:
            break
        visited_threads.add(thread_no)

        posts = fetch_thread(thread_no)
        if not posts:
            print(f"Thread {thread_no} is gone (404) — stopping.")
            break

        ids = extract_gofile_ids(posts)
        fresh = [(cid, pno, thread_no) for cid, pno in ids if cid not in seen_ids]
        seen_ids.update(cid for cid, _, _ in fresh)
        all_ids = fresh + all_ids  # prepend: previous threads end up first
        print(
            f"Thread {thread_no}: {len(posts)} posts, {len(ids)} gofile IDs ({len(fresh)} new)"
        )

        prev = find_previous_thread(posts)
        if prev is None:
            print("No previous-thread link found in OP.")
            break
        thread_no = prev
        time.sleep(1)  # 4chan API rate-limit courtesy

    if not all_ids:
        print("No gofile IDs found.")
        sys.exit(1)

    print(f"\nTotal unique gofile IDs: {len(all_ids)}")
    for cid, pno, tno in all_ids:
        print(f"  https://gofile.io/d/{cid}  (post {pno}, thread {tno})")

    if args.list_only:
        return

    # --- One shared token for all downloads (gofile rate-limits account creation) ---
    cached = load_cached_token()
    if cached:
        account_token, age = cached
        age_str = f"{int(age / 3600)}h" if age >= 3600 else f"{int(age / 60)}m"
        print(f"\nUsing cached gofile token ({age_str} old): {account_token[:8]}...")
    else:
        print("\nCreating shared gofile guest account...")
        account_token = create_guest_account()
        save_token(account_token)
        print(f"  Token: {account_token[:8]}...")

    # --- Download each ---
    succeeded, failed = [], []
    for i, (cid, pno, tno) in enumerate(all_ids, 1):
        print(f"\n{'=' * 60}")
        print(f"[{i}/{len(all_ids)}] gofile.io/d/{cid}")
        print("=" * 60)
        try:
            ok = download_gofile(
                cid,
                output_base=args.output,
                dry_run=args.dry_run,
                account_token=account_token,
            )
        except KeyboardInterrupt:
            raise
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"Connection to gofile lost ({type(e).__name__}) — aborting run.")
            print("Re-run later; already-downloaded files will be skipped.")
            failed.extend(c for c, _, _ in all_ids[i - 1 :])
            break
        except Exception as e:
            print(f"Unexpected error: {e}")
            ok = False
        (succeeded if ok else failed).append(cid)
        time.sleep(3)  # be gentle with the gofile API — it IP-bans aggressive clients

    print(f"\n{'=' * 60}")
    print(f"Summary: {len(succeeded)} succeeded, {len(failed)} failed/dead")
    if failed:
        print("Failed IDs:")
        for cid in failed:
            print(f"  https://gofile.io/d/{cid}")


if __name__ == "__main__":
    main()
