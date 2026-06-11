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
```

## Architecture

Four modules with a strict dependency order:

```
gofile_dl.py          ← CLI entry point / orchestrator
  api_client.py       ← Gofile REST API (guest auth, content listing, tree walk)
  browser_scraper.py  ← Playwright fallback (only when API fails)
  downloader.py       ← File download, skip logic, date setting, orphan detection
```

**Execution flow:**
1. `gofile_dl.py` parses the content ID, creates a guest account, calls the API
2. If the API succeeds, `parse_file_tree` recursively flattens the folder tree into `files[]` and `folders[]` lists
3. If the API fails (not if it succeeds with 0 files — that means private subfolders), the Playwright scraper is tried instead
4. `downloader.py` writes files to `{output}/{folder_name}/`, skipping by exact size match, setting timestamps from `create_time`

## Key design decisions

**API-first, browser-never-on-success:** The browser fallback is only invoked when the API genuinely fails. A successful API response with 0 files means private subfolders — running the browser scraper there would download garbage HTML masquerading as a successful sync.

**Skip logic (downloader.py `download_files`):** Skip if `local_size == remote_size`. Re-download if sizes differ. Skip with warning if no remote size available and local file is non-empty. Always update timestamps even for skipped files.

**TOCTOU avoidance:** File existence is checked with a single `os.path.getsize()` call (EAFP), not `os.path.exists()` + `getsize()`, to avoid races on Windows filesystems (RAMdisk, network drives, antivirus).

**Duplicate filenames:** Gofile allows multiple files with the same name in a folder. `_disambiguate_paths` in `api_client.py` appends a short ID-derived suffix (e.g. `file (abc12345).txt`) to every member of a collision group. The suffix is stable across runs regardless of API ordering.

**Folder date ordering:** Folder timestamps are set deepest-first so that writing a child's mtime doesn't bump the parent's mtime.

**Private subfolders:** In `_walk` (api_client.py), folders with `canAccess == False` are skipped with a message rather than silently producing an empty directory.

**Website token:** The `X-Website-Token` header is a SHA-256 of `"{UA}::en-US::{token}::{time_slot}::{salt}"` where `time_slot = int(time.time() / 14400)`. Reverse-engineered from `https://gofile.io/dist/js/wt.obf.js` (`generateWT` function). The salt rotates when gofile updates the file — if API calls start returning `error-wrongToken` on fresh tokens, re-extract: fetch `wt.obf.js`, patch `_sha256` to log its input, call `generateWT(anyToken)` in Node.js, and read the last `::` segment. Current salt as of 2026-05-25: `g4f8fd9f12h14g`. UA must match `DEFAULT_UA` in `api_client.py` (currently Firefox 151.0).

**Firefox token extraction:** Prefers `appdataAccount` key in localStorage (active session) over `accountsObject` (may contain stale tokens). Using the wrong key produces a valid-looking token that generates a mismatched `X-Website-Token`.
