"""Download files with skip logic, date preservation, and progress display."""

import os
import time
import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def download_files(
    files: list[dict],
    folders: list[dict],
    output_dir: str,
    account_token: str | None = None,
    dry_run: bool = False,
):
    """
    Download a list of files to output_dir.

    Each file dict should have: name, path, link, size (optional), create_time (optional).
    Each folder dict should have: name, path, create_time (optional).

    Skips files that already exist with matching size.
    Always updates file dates when create_time is available (even for skipped files).
    Creates subdirectories and sets their dates after all files are processed.
    """
    # Create subdirectories up front
    _create_folders(folders, output_dir, dry_run)

    total = len(files)
    downloaded = 0
    skipped = 0
    date_updated = 0
    errors = 0

    for i, f in enumerate(files, 1):
        rel_path = f["path"]
        local_path = os.path.join(output_dir, rel_path)
        size = f.get("size")
        create_time = f.get("create_time")
        link = f.get("link")

        prefix = f"[{i}/{total}] {rel_path}"

        # Check if file exists and can be skipped
        if os.path.exists(local_path):
            local_size = os.path.getsize(local_path)
            if size is not None and local_size == size:
                msg = f"{prefix} — SKIP (exists, {_fmt_size(size)})"
                skipped += 1
                if create_time:
                    _set_file_time(local_path, create_time)
                    date_updated += 1
                    msg += " [date updated]"
                print(msg)
                continue
            elif size is not None and local_size != size:
                print(f"{prefix} — RE-DOWNLOAD (size mismatch: local={local_size}, remote={size})")
                # fall through to download below
            else:
                # No remote size info — skip if file exists with non-zero size
                if local_size > 0:
                    msg = f"{prefix} — SKIP (exists, {_fmt_size(local_size)}, no remote size to verify)"
                    skipped += 1
                    if create_time:
                        _set_file_time(local_path, create_time)
                        date_updated += 1
                        msg += " [date updated]"
                    print(msg)
                    continue

        if not link:
            print(f"{prefix} — ERROR: no download link")
            errors += 1
            continue

        if dry_run:
            print(f"{prefix} — DRY RUN (would download, {_fmt_size(size)})")
            continue

        # Ensure directory exists
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        # Download the file
        try:
            _download_file(link, local_path, account_token, size, prefix)
            downloaded += 1
            msg = f"{prefix} — OK ({_fmt_size(os.path.getsize(local_path))})"

            if create_time:
                _set_file_time(local_path, create_time)
                date_updated += 1
                msg += " [date set]"
            print(msg)

        except Exception as e:
            print(f"{prefix} — ERROR: {e}")
            errors += 1
            # Clean up partial file
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass

    # Set folder dates after all files are written (deepest first so parent
    # mtime isn't updated by later child writes)
    _set_folder_dates(folders, output_dir)

    print()
    print(f"Done: {downloaded} downloaded, {skipped} skipped, {date_updated} dates updated, {errors} errors")
    return {"downloaded": downloaded, "skipped": skipped, "date_updated": date_updated, "errors": errors}


def update_dates_only(files: list[dict], folders: list[dict], output_dir: str):
    """Update file and folder dates for all existing items without downloading anything."""
    updated = 0
    missing = 0
    no_date = 0

    for f in files:
        rel_path = f["path"]
        local_path = os.path.join(output_dir, rel_path)
        create_time = f.get("create_time")

        if not os.path.exists(local_path):
            missing += 1
            continue

        if not create_time:
            no_date += 1
            continue

        _set_file_time(local_path, create_time)
        updated += 1
        print(f"  Date updated: {rel_path}")

    # Update folder dates (deepest first)
    folder_dates = _set_folder_dates(folders, output_dir)
    updated += folder_dates

    print(f"Dates: {updated} updated, {missing} files not found locally, {no_date} had no date info")
    return updated


def find_orphans(files: list[dict], folders: list[dict], output_dir: str) -> list[str]:
    """
    Find local files that don't exist on the remote server.

    Returns a list of relative paths for files present locally but not in the
    remote file listing.
    """
    remote_paths = {f["path"].replace("\\", "/") for f in files}

    orphans = []
    if not os.path.isdir(output_dir):
        return orphans

    for dirpath, dirnames, filenames in os.walk(output_dir):
        for filename in filenames:
            local_abs = os.path.join(dirpath, filename)
            rel = os.path.relpath(local_abs, output_dir).replace("\\", "/")
            if rel not in remote_paths:
                orphans.append(rel)

    orphans.sort()
    return orphans


def _create_folders(folders: list[dict], output_dir: str, dry_run: bool = False):
    """Create subdirectories listed in folders."""
    for folder in folders:
        local_path = os.path.join(output_dir, folder["path"])
        if not os.path.exists(local_path):
            if dry_run:
                print(f"  DRY RUN: would create directory {folder['path']}")
            else:
                os.makedirs(local_path, exist_ok=True)
                print(f"  Created directory: {folder['path']}")
        else:
            print(f"  Directory exists: {folder['path']}")


def _set_folder_dates(folders: list[dict], output_dir: str) -> int:
    """Set dates on folders, deepest first so parent mtime isn't disturbed."""
    updated = 0
    # Sort by path depth descending so children are processed before parents
    sorted_folders = sorted(folders, key=lambda f: f["path"].count("/"), reverse=True)
    for folder in sorted_folders:
        create_time = folder.get("create_time")
        if not create_time:
            continue
        local_path = os.path.join(output_dir, folder["path"])
        if os.path.isdir(local_path):
            _set_file_time(local_path, create_time)
            print(f"  Directory date set: {folder['path']}")
            updated += 1
    return updated


def _download_file(url: str, local_path: str, account_token: str | None,
                   expected_size: int | None, prefix: str = ""):
    """Stream-download a file with proper headers."""
    headers = {
        "User-Agent": DEFAULT_UA,
        "Origin": "https://gofile.io",
        "Referer": "https://gofile.io/",
    }
    cookies = {}
    if account_token:
        cookies["accountToken"] = account_token

    r = requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=300)
    r.raise_for_status()

    total = int(r.headers.get("content-length", 0)) or expected_size or 0
    wrote = 0
    max_line_len = 0

    with open(local_path, "wb") as fp:
        for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
            fp.write(chunk)
            wrote += len(chunk)
            if total:
                pct = wrote * 100 // total
                line = f"{prefix} — Downloading... {pct}% ({_fmt_size(wrote)}/{_fmt_size(total)})"
                max_line_len = max(max_line_len, len(line))
                print(f"\r{line}", end="", flush=True)

    if max_line_len:
        # Clear the progress line so the caller can print the final status cleanly
        print("\r" + " " * max_line_len + "\r", end="")

    if expected_size and wrote != expected_size:
        raise RuntimeError(f"Size mismatch: got {wrote}, expected {expected_size}")


def _set_file_time(path: str, timestamp: float):
    """Set both atime and mtime on a file."""
    try:
        os.utime(path, (timestamp, timestamp))
    except OSError as e:
        print(f"  Warning: could not set date on {path}: {e}")


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if isinstance(size, float) or size >= 100 else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
