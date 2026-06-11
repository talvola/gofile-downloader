---
name: smoke-test
description: Run a dry-run download against a gofile.io content ID to verify the API client and auth token logic are still working
disable-model-invocation: true
---

Run: python gofile_dl.py <CONTENT_ID> --dry-run

Replace <CONTENT_ID> with any gofile.io content ID you know exists (e.g. one from a recent download).

**Passing indicators** (API and auth are healthy):
- "Token: xxxxxxxx..." printed — guest account creation succeeded
- "Files found: N" or "error-notFound" — API responded correctly either way

**Failing indicators** (auth/API broken, check api_client.py):
- HTTP 401/403 or "Failed to create guest account"
- Connection error or timeout on the API call
- Falls through to browser scraper unexpectedly

If auth breaks, check TOKEN_SALT and the _website_token() formula in api_client.py — gofile.io may have updated their frontend.
