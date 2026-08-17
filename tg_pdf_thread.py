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

from api_client import GofileUnavailable, acquire_token
from gofile_dl import DEFAULT_OUTPUT
from gofile_dl import run as download_gofile

API_BASE = "https://a.4cdn.org"
BOARD = "tg"
THREAD_TITLE = "pdf share thread"

# When the live catalog has no PDF Share Thread (old one archived, successor
# not posted yet), scan back through the archive newest-first for it. The gap
# can be large on a busy board — /tg/ archived 30+ other threads before a
# successor appeared in one observed case — so scan deep. archive.json only
# gives thread numbers (no subjects), so each candidate must be fetched; the
# scan early-exits on the first (newest) match, so this cap only bounds the
# rare case where no PDF thread exists in the archive at all.
ARCHIVE_SCAN_LIMIT = 150

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# gofile content IDs are alphanumeric: historically 6 chars, and since ~2026
# newly-issued ones are 8 (e.g. gofile.io/d/ILUoTvWO). Old 6-char links still
# work, so both lengths are accepted — but ONLY those two, never a range:
# "6-or-more" would swallow other services' long IDs and re-open the
# false-positive problem the anchors below exist to prevent.
ID_CHARS = r"(?:[A-Za-z0-9]{8}|[A-Za-z0-9]{6})"

# Two accepted forms:
#
# 1. d/ form — the canonical gofile.io/d/AS3dnB, the bare /d/AS3dnB, the
#    obfuscated "g0f1le /d/3tDCbP", AND the leading-slash-dropped "d/GDeWzi"
#    (seen in the wild). We anchor on "d/" preceded by whitespace, a slash, or
#    string start so a word ending in "d" (e.g. "read/foobar") can't match.
#    The (?![A-Za-z0-9]) guard rejects longer IDs from other services
#    (e.g. Google Drive's /d/<33 chars>) and odd lengths (7, 9+) alike.
GOFILE_DFORM_RE = re.compile(r"(?:^|(?<=[\s/]))d/\s*(" + ID_CHARS + r")(?![A-Za-z0-9])")

# 2. Bare "/XXXXXX" or "/XXXXXXXX" with no d/ — trusted ONLY when the slash is
#    whitespace- or line-bounded on the left and the slug is whitespace-/line-
#    bounded on the right. Without those anchors every mid-URL path segment
#    (e.g. drivethrurpg.com/product/564646/…) would masquerade as a gofile ID;
#    with them, a lone "/slug" posted on its own (e.g. "This one?\n/3BKw5y")
#    still matches while the false positives vanish.
GOFILE_BARE_RE = re.compile(r"(?:^|(?<=\s))/(" + ID_CHARS + r")(?=\s|$)", re.MULTILINE)

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
    # has no match — scan archived threads newest-first for it.
    archive = _get_json(f"{API_BASE}/{BOARD}/archive.json")
    # archive.json is oldest-first; reverse for newest-first, cap the depth.
    candidates = list(reversed(archive))[:ARCHIVE_SCAN_LIMIT]
    print(
        f"Not in the live catalog — scanning up to {len(candidates)} "
        f"archived threads (newest first)..."
    )
    for i, thread_no in enumerate(candidates, 1):
        try:
            posts = fetch_thread(thread_no)
        except requests.RequestException:
            continue  # transient failure on one archived thread — keep scanning
        if posts:
            subject = html.unescape(posts[0].get("sub", "")).lower()
            if THREAD_TITLE in subject:
                print(f"  Found it in the archive after {i} thread(s).")
                return thread_no
        time.sleep(1)
    raise RuntimeError(
        f"No '{THREAD_TITLE}' found in /{BOARD}/ catalog or the "
        f"{len(candidates)} newest archived threads"
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
        # d/ forms first, then whitespace-bounded bare slugs; dedup spans both.
        for m in GOFILE_DFORM_RE.finditer(text):
            content_id = m.group(1)
            if content_id not in seen:
                seen.add(content_id)
                found.append((content_id, post["no"]))
        for m in GOFILE_BARE_RE.finditer(text):
            content_id = m.group(1)
            if content_id not in seen:
                seen.add(content_id)
                found.append((content_id, post["no"]))
                print(
                    f"  note: accepted bare slug /{content_id} "
                    f"(post {post['no']}) — no d/ prefix"
                )
    return found


def find_linked_threads(posts: list[dict]) -> list[int]:
    """All cross-thread quotelinks in the OP, in order of appearance."""
    if not posts:
        return []
    out: list[int] = []
    for m in PREV_THREAD_RE.finditer(posts[0].get("com", "")):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out


def find_previous_pdf_thread(
    posts: list[dict], visited: set[int]
) -> tuple[int, list[dict]] | None:
    """
    Follow the OP's cross-thread quotelinks to the previous PDF Share Thread.

    OPs often link several threads (related generals, resource threads), so
    fetch each candidate in order of appearance and take the first whose
    subject matches, instead of blindly trusting the first link.

    Returns (thread_no, posts) so the caller doesn't re-fetch, or None.
    """
    for cand in find_linked_threads(posts):
        if cand in visited:
            continue
        try:
            cand_posts = fetch_thread(cand)
        except requests.RequestException as e:
            print(f"4chan API error fetching linked thread {cand}: {e}")
            return None
        if cand_posts:
            subject = html.unescape(cand_posts[0].get("sub", ""))
            if THREAD_TITLE in subject.lower():
                return cand, cand_posts
            print(
                f"  Skipping linked thread {cand} "
                f"('{subject or '(no subject)'}') — not a PDF Share Thread"
            )
        time.sleep(1)  # 4chan API rate-limit courtesy
    return None


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
        try:
            thread_no = find_pdf_share_thread()
        except requests.RequestException as e:
            print(f"4chan API error while searching for the thread: {e}")
            sys.exit(1)
        except RuntimeError as e:
            print(e)
            sys.exit(1)
    print(f"Starting thread: https://boards.4chan.org/{BOARD}/thread/{thread_no}")

    # --- Walk threads, collecting IDs (oldest thread first so downloads are chronological) ---
    all_ids: list[tuple[str, int, int]] = []  # (content_id, post_no, thread_no)
    seen_ids = set()
    visited_threads: set[int] = set()
    depth = max(1, args.depth)
    posts = None  # posts for thread_no; the previous-thread lookup pre-fetches them

    for depth_i in range(depth):
        visited_threads.add(thread_no)
        if posts is None:
            try:
                posts = fetch_thread(thread_no)
            except requests.RequestException as e:
                print(
                    f"4chan API error fetching thread {thread_no}: {e} — "
                    f"continuing with IDs collected so far."
                )
                break
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

        if depth_i == depth - 1:
            break
        time.sleep(1)  # 4chan API rate-limit courtesy

        prev = find_previous_pdf_thread(posts, visited_threads)
        if prev is None:
            print("No previous PDF Share Thread link found in OP.")
            break
        thread_no, posts = prev

    if not all_ids:
        print("No gofile IDs found.")
        sys.exit(1)

    print(f"\nTotal unique gofile IDs: {len(all_ids)}")
    for cid, pno, tno in all_ids:
        print(f"  https://gofile.io/d/{cid}  (post {pno}, thread {tno})")

    if args.list_only:
        return

    # --- Warm the shared token cache before the batch (gofile rate-limits
    # account creation; each download_gofile call reads the cache, so one
    # token — refreshed at most once — serves the whole run) ---
    print()
    try:
        if acquire_token() is None:
            print("Could not obtain a gofile token — aborting before downloads.")
            sys.exit(1)
    except GofileUnavailable as e:
        print(f"Gofile unavailable: {e}")
        print("Please retry later.")
        sys.exit(1)

    # --- Download each ---
    succeeded, failed = [], []
    for i, (cid, pno, tno) in enumerate(all_ids, 1):
        print(f"\n{'=' * 60}")
        print(f"[{i}/{len(all_ids)}] gofile.io/d/{cid}")
        print("=" * 60)
        try:
            ok = download_gofile(cid, output_base=args.output, dry_run=args.dry_run)
        except KeyboardInterrupt:
            raise
        except GofileUnavailable as e:
            print(f"Gofile unavailable ({e}) — aborting run.")
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
