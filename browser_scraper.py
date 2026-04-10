"""Playwright-based fallback for extracting file listings from gofile.io."""

import re
import time

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext
except ImportError:
    sync_playwright = None
    Page = None
    BrowserContext = None


def scrape_content(content_id: str, password: str | None = None) -> tuple[str, list[dict]]:
    """
    Use a headless browser to load gofile.io/d/{content_id} and extract
    the file listing.

    Returns (folder_name, list_of_file_dicts).
    Each file dict has: name, path, link, size, create_time (may be None).
    """
    if sync_playwright is None:
        raise RuntimeError(
            "playwright is not installed. Install it with:\n"
            "  pip install playwright && python -m playwright install chromium"
        )

    url = f"https://gofile.io/d/{content_id}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        # Dismiss popups / overlays
        page.on("dialog", lambda dialog: dialog.dismiss())

        page.goto(url, wait_until="networkidle", timeout=60000)

        # Handle password if needed
        if password:
            _enter_password(page, password)

        # Wait for content to render
        _wait_for_content(page)

        # Extract folder name and file listing
        folder_name = _extract_folder_name(page, content_id)
        files = _extract_files(page, context, content_id)

        browser.close()

    return folder_name, files


def _enter_password(page: Page, password: str):
    try:
        pw_input = page.wait_for_selector(
            'input[type="password"], input[placeholder*="assword"]',
            timeout=5000,
        )
        if pw_input:
            pw_input.fill(password)
            # Click submit button near password field
            page.click('button:has-text("OK"), button:has-text("Submit"), button[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # No password prompt found


def _wait_for_content(page: Page):
    """Wait for the file listing table/grid to appear."""
    selectors = [
        "table",
        ".content-table",
        "[class*='file']",
        "[class*='item']",
        "[class*='list']",
        "a[href*='download']",
    ]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=10000)
            return
        except Exception:
            continue
    # Final fallback — just wait a bit for JS to render
    page.wait_for_timeout(5000)


def _extract_folder_name(page: Page, content_id: str) -> str:
    """Try to get the folder name from the page title or breadcrumbs."""
    try:
        # Look for breadcrumb or heading
        for selector in ["h1", "h2", ".folder-name", "[class*='breadcrumb']", "[class*='title']"]:
            el = page.query_selector(selector)
            if el:
                text = el.inner_text().strip()
                if text and text != content_id and len(text) < 200:
                    return text
    except Exception:
        pass
    return content_id


def _extract_files(page: Page, context: BrowserContext, content_id: str) -> list[dict]:
    """
    Extract file information from the rendered page.
    Uses JavaScript evaluation to pull data from the page's internal state.
    """
    # Strategy 1: Try to intercept the API response data from page JS context
    files = _try_js_extraction(page)
    if files:
        return files

    # Strategy 2: Parse the DOM for file rows
    files = _try_dom_extraction(page)
    if files:
        return files

    return []


def _try_js_extraction(page: Page) -> list[dict] | None:
    """Try to extract file data from JavaScript variables on the page."""
    try:
        # Gofile stores content data in window/app state — try common patterns
        result = page.evaluate("""() => {
            // Try various places gofile might store data
            const sources = [
                window.__NEXT_DATA__,
                window.__NUXT__,
                window.appData,
                window.contentData,
                window.files,
            ];
            for (const src of sources) {
                if (src) return JSON.stringify(src);
            }

            // Try to find data in script tags
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const text = s.textContent || '';
                if (text.includes('contentId') || text.includes('children')) {
                    // Find JSON-like objects
                    const match = text.match(/\\{[^{}]*"children"[^{}]*\\}/);
                    if (match) return match[0];
                }
            }
            return null;
        }""")
        if result:
            import json
            data = json.loads(result)
            # Try to parse as API-like structure
            from api_client import parse_file_tree
            return parse_file_tree(data)
    except Exception:
        pass
    return None


def _try_dom_extraction(page: Page) -> list[dict]:
    """Parse file info directly from DOM elements."""
    files = []
    try:
        # Extract all links and associated metadata from the page
        items = page.evaluate("""() => {
            const results = [];

            // Look for table rows with file info
            const rows = document.querySelectorAll(
                'tr, [class*="file-row"], [class*="item"], [class*="content-item"]'
            );

            for (const row of rows) {
                const links = row.querySelectorAll('a[href]');
                const texts = row.innerText.split('\\n').map(t => t.trim()).filter(Boolean);

                for (const link of links) {
                    const href = link.href;
                    const name = link.textContent.trim();
                    if (!name || !href) continue;

                    // Skip navigation links
                    if (href.includes('/d/') && !href.includes('download'))
                        continue;

                    results.push({
                        name: name,
                        link: href,
                        texts: texts,
                    });
                }
            }

            // Also look for direct download buttons/links
            const dlLinks = document.querySelectorAll(
                'a[href*="download"], a[href*=".gofile.io/download"], button[onclick*="download"]'
            );
            for (const dl of dlLinks) {
                const name = dl.textContent.trim() ||
                             dl.getAttribute('download') ||
                             dl.getAttribute('title') || '';
                if (name) {
                    results.push({
                        name: name,
                        link: dl.href || '',
                        texts: [],
                    });
                }
            }

            return results;
        }""")

        seen = set()
        for item in items:
            name = item.get("name", "").strip()
            link = item.get("link", "")
            if not name or name in seen:
                continue
            seen.add(name)

            # Try to parse size and date from surrounding text
            size = _parse_size(item.get("texts", []))
            create_time = _parse_date(item.get("texts", []))

            files.append({
                "name": name,
                "path": name,
                "link": link if link else None,
                "size": size,
                "create_time": create_time,
            })

    except Exception as e:
        print(f"  DOM extraction error: {e}")

    return files


def _parse_size(texts: list[str]) -> int | None:
    """Try to parse a file size from text fragments."""
    size_re = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB|B)", re.IGNORECASE)
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for t in texts:
        m = size_re.search(t)
        if m:
            val = float(m.group(1))
            unit = m.group(2).upper()
            return int(val * multipliers.get(unit, 1))
    return None


def _parse_date(texts: list[str]) -> float | None:
    """Try to parse a date from text fragments and return as Unix timestamp."""
    from datetime import datetime
    date_patterns = [
        (r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", "%Y-%m-%d %H:%M"),
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{2}/\d{2}/\d{4}", "%m/%d/%Y"),
    ]
    for t in texts:
        for pattern, fmt in date_patterns:
            m = re.search(pattern, t)
            if m:
                try:
                    dt = datetime.strptime(m.group(0), fmt)
                    return dt.timestamp()
                except ValueError:
                    continue
    return None
