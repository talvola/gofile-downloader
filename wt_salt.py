"""Website-token salt — resolution, and re-extraction from gofile's live script.

Gofile signs API calls with

    X-Website-Token = sha256("{UA}::{lang}::{account_token}::{time_slot}::{salt}")

where the salt is a constant baked into https://gofile.io/dist/js/wt.obf.js.
Gofile rotates that salt every couple of months and reports a stale one as
`error-notPremium` (older builds: `error-wrongToken`) — an error that reads
like an account problem and isn't. A hardcoded salt therefore breaks the tool
on gofile's schedule, so DEFAULT_SALT below is only a starting point:
`refresh()` re-extracts the live salt and caches it in ~/.gofile_dl_wt.json,
and `current_salt()` prefers the cached value.

Extraction runs the real script in a real browser instead of deobfuscating it:
hook the string->bytes conversions, call the page's own `generateWT()` with a
probe token, and read back the exact string it hashed. That survives any
reshuffle of the obfuscation, and the hash `generateWT` returns doubles as a
check that our Python formula still reproduces gofile's — if the formula
itself changed (not just the salt), verification fails loudly instead of
silently signing every request wrong.
"""

import hashlib
import json
import os
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# Last known-good salt (extracted 2026-08-14). Superseded by the cache file
# once refresh() has run — see current_salt().
DEFAULT_SALT = "12af056dacea0b"

SALT_CACHE_PATH = Path.home() / ".gofile_dl_wt.json"
WT_JS_URL = "https://gofile.io/dist/js/wt.obf.js"
WT_JS_PATH = "/dist/js/wt.obf.js"

# Arbitrary stand-in for an account token: it only has to be recognisable in
# the string generateWT() hashes, so we can split the salt off the end.
PROBE_TOKEN = "wtProbeToken00000000"

# Hooks the string->bytes conversions a SHA-256 implementation has to go
# through (pure-JS charCodeAt loop, TextEncoder, or WebCrypto), calls the
# page's generateWT(), and returns every hashed string containing the probe.
_EXTRACT_JS = """
async (probe) => {
  const seen = [];
  const note = (s) => {
    if (typeof s === 'string' && s.length > 32 && s.length < 4096
        && s.indexOf('::') !== -1 && seen.indexOf(s) === -1) {
      seen.push(s);
    }
  };
  const origCharCodeAt = String.prototype.charCodeAt;
  const origEncode = window.TextEncoder ? TextEncoder.prototype.encode : null;
  const origDigest = (window.crypto && crypto.subtle) ? crypto.subtle.digest : null;
  let wt = null, error = null;
  const fn = window.generateWT;
  if (typeof fn !== 'function') {
    return { wt: null, candidates: [], fnFound: false, error: 'generateWT is not defined',
             ua: navigator.userAgent, lang: navigator.language };
  }
  try {
    String.prototype.charCodeAt = function (i) {
      note(String(this));
      return origCharCodeAt.call(this, i);
    };
    if (origEncode) {
      TextEncoder.prototype.encode = function (s) { note(s); return origEncode.call(this, s); };
    }
    if (origDigest) {
      crypto.subtle.digest = function (alg, buf) {
        try { note(new TextDecoder().decode(buf)); } catch (e) {}
        return origDigest.call(crypto.subtle, alg, buf);
      };
    }
    wt = await fn(probe);
  } catch (e) {
    error = String((e && e.message) || e);
  } finally {
    String.prototype.charCodeAt = origCharCodeAt;
    if (origEncode) TextEncoder.prototype.encode = origEncode;
    if (origDigest) crypto.subtle.digest = origDigest;
  }
  return {
    wt: wt == null ? null : String(wt),
    candidates: seen.slice(0, 8),
    fnFound: true,
    error,
    ua: navigator.userAgent,
    lang: navigator.language,
  };
}
"""


def load_cached_salt() -> str | None:
    """Return the cached salt, or None if there isn't a usable one.

    The cache is ignored when it was written against a different DEFAULT_SALT
    than the code now carries: that means the constant was updated after the
    cache was written, so the constant is the newer of the two.
    """
    try:
        data = json.loads(SALT_CACHE_PATH.read_text())
    except Exception:
        return None
    salt = data.get("salt")
    if not salt or data.get("code_default") != DEFAULT_SALT:
        return None
    return salt


def save_salt(salt: str) -> None:
    """Cache an extracted salt (non-fatal if it can't be written)."""
    try:
        SALT_CACHE_PATH.write_text(
            json.dumps(
                {"salt": salt, "updated_at": time.time(), "code_default": DEFAULT_SALT}
            )
        )
    except Exception:
        pass


def current_salt() -> str:
    """The salt to sign with: env override -> cached -> code default."""
    return os.environ.get("GOFILE_WT_SALT") or load_cached_salt() or DEFAULT_SALT


def refresh(ua: str, lang: str = "en-US", timeout_ms: int = 60000) -> str | None:
    """
    Re-extract the website-token salt from gofile's live wt.obf.js.

    Loads gofile.io in a headless browser under the same UA the API client
    signs with, calls the page's own generateWT() with a probe token, and
    recovers the salt from the string it hashed. Caches and returns the salt,
    or returns None (with an explanation printed) if extraction failed —
    callers should carry on with the existing salt rather than treat this as
    fatal.
    """
    if sync_playwright is None:
        print(
            "  Cannot re-extract the website-token salt: playwright is not installed\n"
            "  (pip install playwright && python -m playwright install chromium)"
        )
        return None

    print("  Re-extracting website-token salt from gofile.io/dist/js/wt.obf.js...")
    try:
        result = _extract_in_browser(ua, timeout_ms)
    except Exception as e:
        print(f"  Salt extraction failed: {type(e).__name__}: {e}")
        return None

    if not result.get("fnFound"):
        print(
            "  Salt extraction failed: gofile's page no longer defines generateWT()"
            " — the token scheme itself has changed (see CLAUDE.md 'Website token')."
        )
        return None
    if result.get("error"):
        print(f"  Salt extraction failed: generateWT() raised: {result['error']}")
        return None

    candidates = result.get("candidates") or []
    if not candidates:
        print(
            "  Salt extraction failed: generateWT() hashed nothing recognisable"
            " — the token scheme has changed (see CLAUDE.md 'Website token')."
        )
        return None

    previous = current_salt()
    salt = _parse_salt(candidates, result, ua, lang)
    if salt is None:
        return None

    save_salt(salt)
    if salt == previous:
        print(f"  Salt unchanged ({salt}) — the salt is not what's failing.")
    else:
        print(
            f"  Salt rotated: {previous} -> {salt} (cached in {SALT_CACHE_PATH.name};"
            f" update DEFAULT_SALT in wt_salt.py to make it the built-in default)."
        )
    return salt


def _extract_in_browser(ua: str, timeout_ms: int) -> dict:
    """Load gofile.io and run _EXTRACT_JS against its generateWT()."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=ua, locale="en-US")
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.goto(
                "https://gofile.io/", wait_until="domcontentloaded", timeout=timeout_ms
            )
            # The homepage normally loads wt.obf.js itself; inject it (same
            # origin, so no CSP fight) if it hasn't by the time we look.
            if not page.evaluate("() => typeof window.generateWT === 'function'"):
                page.add_script_tag(url=WT_JS_PATH)
                page.wait_for_function(
                    "() => typeof window.generateWT === 'function'", timeout=15000
                )
            return page.evaluate(_EXTRACT_JS, PROBE_TOKEN)
        finally:
            browser.close()


def _strip_sha_padding(raw: str) -> str:
    """
    Drop SHA-256's own block padding from a captured string.

    A pure-JS sha256 (which is what gofile ships) appends 0x80 and a run of
    NULs to the message *before* walking it with charCodeAt, so the string the
    hook sees is the padded one. Everything from the first control character
    on is padding, never salt.
    """
    for i, ch in enumerate(raw):
        if ord(ch) < 0x20 or ord(ch) == 0x80:
            return raw[:i]
    return raw


def _parse_salt(candidates: list[str], result: dict, ua: str, lang: str) -> str | None:
    """
    Recover the salt from the strings generateWT() hashed.

    Expects "{ua}::{lang}::{probe}::{slot}::{salt}". Anything else means the
    formula changed, which is worth saying out loud — signing with a salt
    parsed out of a formula we no longer understand would fail confusingly
    later instead of here.

    When generateWT() returned its hash, that hash decides which candidate
    (and which de-padded form of it) is the real message: a salt that doesn't
    reproduce gofile's own output is not a salt worth caching.
    """
    wt = result.get("wt")
    unverified = None

    seen = []
    for raw in candidates:
        for form in (raw, _strip_sha_padding(raw)):
            if form and form not in seen:
                seen.append(form)

    for form in seen:
        parts = form.split("::")
        if len(parts) < 5 or parts[2] != PROBE_TOKEN or not parts[3].isdigit():
            continue
        salt = "::".join(parts[4:])
        if wt and hashlib.sha256(form.encode()).hexdigest() != wt:
            continue  # a padded/partial capture, or not the string that was hashed
        if parts[0] != ua:
            print(
                f"  Warning: page hashed a different User-Agent than we send.\n"
                f"    page: {parts[0]}\n    ours: {ua}\n"
                "  Update DEFAULT_UA in api_client.py to match."
            )
        if parts[1] != lang:
            print(
                f"  Warning: page hashed language {parts[1]!r}, we send {lang!r}"
                " (X-BL header in api_client.py)."
            )
        if not wt:
            # No hash to check against — well-formed is the best we can do.
            unverified = salt
            continue
        return salt

    if unverified is not None:
        print(
            "  Note: generateWT() returned no hash to verify against, so the salt"
            " below is taken on the shape of the string it hashed alone."
        )
        return unverified

    if wt and seen:
        print(
            "  Salt extraction failed: nothing generateWT() hashed reproduces the"
            " hash it returned — the hash step itself has changed. It hashed:"
        )
        for form in seen[:3]:
            print(f"    {ascii(form.replace(PROBE_TOKEN, '<token>')[:300])}")
        return None

    print(
        "  Salt extraction failed: generateWT() hashed an unexpected string shape,"
        " so the formula has changed. It hashed:"
    )
    for raw in candidates[:3]:
        print(f"    {ascii(raw.replace(PROBE_TOKEN, '<token>')[:300])}")
    print("  Update _website_token() in api_client.py to match.")
    return None


if __name__ == "__main__":  # manual check: python wt_salt.py
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from api_client import DEFAULT_UA

    print(f"Current salt: {current_salt()}")
    refresh(DEFAULT_UA)
