# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI tool to download and sync files from gofile.io folders, with resume/skip logic, date preservation, and orphan detection.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium   # only needed for browser fallback
```

## Running

```bash
python gofile_dl.py PjUhkl
python gofile_dl.py https://gofile.io/d/PjUhkl
python gofile_dl.py PjUhkl --output /mnt/r/gofile
python gofile_dl.py PjUhkl --password secret
python gofile_dl.py PjUhkl --dry-run
python gofile_dl.py PjUhkl --dates-only
python gofile_dl.py PjUhkl --force-browser

python tg_pdf_thread.py                  # batch: scrape /tg/ PDF Share Thread, download all gofile links
python tg_pdf_thread.py --list-only      # just show the IDs found (no gofile contact)
python tg_pdf_thread.py --dry-run        # fetch listings, write nothing
```

## Architecture

Five modules with a strict dependency order:

```
tg_pdf_thread.py        ← batch driver: scrapes 4chan /tg/ for gofile IDs, calls run() per ID
  gofile_dl.py          ← CLI entry point / orchestrator (exposes run())
    api_client.py       ← Gofile REST API (token lifecycle, content listing, tree walk, error classification)
    browser_scraper.py  ← Playwright fallback (only when API fails)
    downloader.py       ← File download, skip logic, date setting, orphan detection
```

**Execution flow:**
1. `gofile_dl.py` parses the content ID, gets a token via `api_client.acquire_token()` (disk cache → Firefox localStorage → new guest account), calls the API
2. If the API succeeds, `parse_file_tree` recursively flattens the folder tree into `files[]` and `folders[]` lists
3. If the API fails (not if it succeeds with 0 files — that means private subfolders), the Playwright scraper is tried instead
4. `downloader.py` writes files to `{output}/{folder_name}/`, skipping by exact size match, setting timestamps from `create_time`

## Key design decisions

**API-first, browser-never-on-success:** The browser fallback is only invoked when the API genuinely fails. A successful API response with 0 files means private subfolders — running the browser scraper there would download garbage HTML masquerading as a successful sync. Global failures (network down, rate-limited, outage, post-refresh wrongToken) never fall back to the browser either — the browser can't reach gofile any better, and retrying makes rate limits worse.

**Structured API errors:** `get_content` returns `(data, ApiError|None)`; the error `kind` (`network`/`rate_limited`/`unavailable`/`not_found`/`not_authorized`/`wrong_token`/`premium`/`other`) is classified once in `api_client.py` where the HTTP status and gofile status string are in hand. Callers branch on `kind`, never on message prose. `not_authorized` is a content-level condition (private/password-protected), NOT a token problem — don't clear the token cache on it.

**Token single source of truth:** The disk cache (`~/.gofile_dl_token.json`) is the one authority. `acquire_token()`/`refresh_token()` in `api_client.py` own the lifecycle (cache → Firefox → guest account); `run()` re-reads the cache per call instead of holding a caller-frozen token, so a mid-batch refresh by one item is picked up by the next. Batch callers must NOT pass tokens around.

**GofileUnavailable aborts batches:** `run()` raises `api_client.GofileUnavailable` (kind: network/rate_limited/unavailable/wrong_token-after-refresh) for conditions where processing more IDs is futile or harmful; `tg_pdf_thread.py` catches it and aborts the whole batch. Per-content failures (`not_found`, `not_authorized`, empty) just `return False` and the batch continues.

**Subfolder-fetch throttle (`_fetch_subfolder` in api_client.py):** Walking a tree fires one API call per subfolder; a folder with 20+ subfolders trips gofile's rate limit if the calls go back-to-back. Each fetch is preceded by a short pause (`SUBFOLDER_FETCH_DELAY`) and retried with backoff (`SUBFOLDER_RETRY_DELAYS`) on `error-rateLimit`. Dropping a rate-limited subfolder is NOT acceptable — it silently omits every file under it while `run()` still reports success (and those files then show up as false orphans). If retries are exhausted the IP is being throttled/temp-banned, so `_fetch_subfolder` raises `GofileUnavailable(rate_limited)`, which propagates through `_walk`→`parse_file_tree`→`run()` to abort the batch rather than persist a partial tree.

**Exit codes:** `gofile_dl.py` exits 1 when content can't be fetched or any file fails a real download attempt; `--dry-run` reports link-less files without failing the exit code.

**Skip logic (downloader.py `download_files`):** Skip if `local_size == remote_size`. Re-download if sizes differ. Skip with warning if no remote size available and local file is non-empty. Always update timestamps even for skipped files.

**TOCTOU avoidance:** File existence is checked with a single `os.path.getsize()` call (EAFP), not `os.path.exists()` + `getsize()`, to avoid races on Windows filesystems (RAMdisk, network drives, antivirus).

**Duplicate filenames:** Gofile allows multiple files with the same name in a folder. `_disambiguate_paths` in `api_client.py` appends a short ID-derived suffix (e.g. `file (abc12345).txt`) to every member of a collision group. The suffix is stable across runs regardless of API ordering.

**Folder date ordering:** Folder timestamps are set deepest-first so that writing a child's mtime doesn't bump the parent's mtime.

**Private subfolders:** In `_walk` (api_client.py), folders with `canAccess == False` are skipped with a message rather than silently producing an empty directory.

**Website token:** The `X-Website-Token` header is a SHA-256 of `"{UA}::en-US::{token}::{time_slot}::{salt}"` where `time_slot = int(time.time() / 14400)`. Reverse-engineered from `https://gofile.io/dist/js/wt.obf.js` (`generateWT` function). The salt rotates every couple of months (2026-06-12: `9844d94d963d30`; 2026-08-14: `12af056dacea0b`) and a stale one is reported as `error-notPremium` (since ~2026-06) or `error-wrongToken` — neither of which sounds like what it is. UA must match `DEFAULT_UA` in `api_client.py` (currently Firefox 151.0).

**Salt self-healing (`wt_salt.py`):** Rotations are handled at runtime, not by editing code. On `premium`/`wrong_token`, `run()` calls `refresh_website_token_salt()` (once per process — a batch must not launch a browser per ID), which loads gofile.io in Playwright under `DEFAULT_UA`, hooks `String.prototype.charCodeAt` / `TextEncoder.encode` / `crypto.subtle.digest`, calls the page's own `generateWT(probe)`, and reads the salt off the end of the string it hashed; the result is cached in `~/.gofile_dl_wt.json` and the failed call is retried once. Resolution order is `GOFILE_WT_SALT` env → cache → `DEFAULT_SALT`, and the cache is ignored when it records a different `code_default` than the constant now in the file (that means the constant is newer). Hooking beats deobfuscating: it survives any reshuffle of `wt.obf.js`. Two traps this handles, both real: gofile's `_sha256` is pure-JS and appends its own `\x80`+NUL block padding **before** walking the string with `charCodeAt`, so the captured string must be trimmed at the first control char or the "salt" comes out polluted; and the hash `generateWT` returns is what decides which captured/trimmed form was the real message — a salt that doesn't reproduce it is never cached. `python gofile_dl.py --refresh-wt` runs the extraction standalone. If the *formula* (not just the salt) changes, extraction fails loudly with the string it actually hashed rather than caching a wrong salt.

**Firefox token extraction:** Prefers `appdataAccount` key in localStorage (active session) over `accountsObject` (may contain stale tokens). Using the wrong key produces a valid-looking token that generates a mismatched `X-Website-Token`.

**Thread discovery (tg_pdf_thread.py):** Searches the live /tg/ catalog by subject, then falls back to scanning archived threads newest-first (up to `ARCHIVE_SCAN_LIMIT`, currently 150) for it — the gap between a thread archiving and its successor being posted can exceed 30 threads on a busy board. `archive.json` gives only thread numbers, so each candidate is fetched to check its subject; the scan early-exits on the first (newest) match, so the cap only bites when no PDF thread exists in the archive at all. Previous-thread links are followed only if the linked thread's subject actually matches `THREAD_TITLE` — OPs often quote-link unrelated threads. 4chan API failures mid-walk keep the IDs already collected rather than crashing.

**Gofile ID extraction (tg_pdf_thread.py):** Two matchers. `GOFILE_DFORM_RE` accepts `d/XXXXXX` anchored on whitespace/slash/start — covers `gofile.io/d/…`, `/d/…`, obfuscated `g0f1le /d/…`, and the leading-slash-dropped `d/GDeWzi`. `GOFILE_BARE_RE` accepts a lone `/XXXXXX` (exactly 6 alnum) ONLY when whitespace-/line-bounded on both sides — without those anchors every mid-URL path segment (e.g. `drivethrurpg.com/product/564646/…`) matches as a false gofile ID. Don't relax the bare-slug anchors; on one /tg/ thread they were the difference between 2 real IDs and 12 junk ones. Every accepted bare slug prints a `note:` line for eyeballing.

**Windows console encoding:** The default console is cp1252 — `print()`-ing non-ASCII (arrows, box-drawing) in a throwaway script raises `UnicodeEncodeError`. Stick to ASCII markers in scratch output.
