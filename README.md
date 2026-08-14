# gofile-downloader

Download and sync files from [gofile.io](https://gofile.io) folders, with
resume/skip logic, date preservation, and orphan detection. Includes a
companion batch tool that scrapes the 4chan /tg/ PDF Share Thread for gofile
links and downloads them all.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium   # only needed for the browser fallback
```

## gofile_dl.py — download one gofile folder

```bash
python gofile_dl.py PjUhkl                          # bare content ID
python gofile_dl.py https://gofile.io/d/PjUhkl      # or the full URL
python gofile_dl.py PjUhkl --output D:/downloads    # override output dir
python gofile_dl.py PjUhkl --password secret        # password-protected content
python gofile_dl.py PjUhkl --dry-run                # show what would happen, write nothing
python gofile_dl.py PjUhkl --dates-only             # only fix file timestamps
python gofile_dl.py PjUhkl --force-browser          # skip the API, scrape with Playwright
python gofile_dl.py PjUhkl --token YOURTOKEN        # use your own account token
```

Files land in `{output}/{folder_name}/`, mirroring the gofile folder
structure. The default output base is `R:\gofile` on Windows and
`/mnt/r/gofile` elsewhere.

Behavior:

- **Sync, not just download.** A file that already exists locally with the
  same size is skipped, so re-running is cheap and an interrupted run can
  simply be re-run. A size mismatch triggers a re-download.
- **Dates preserved.** File and folder timestamps are set from gofile's
  upload times, even for skipped files.
- **Orphan report.** After a sync it lists local files that no longer exist
  on the server. Nothing is ever deleted locally — the report is
  informational.
- **Auth is automatic.** A token is taken from the local cache, then from a
  logged-in Firefox session, then by creating a guest account — whichever
  works first. The token is cached for 24h in `~/.gofile_dl_token.json`.
- **Exit code** is 1 when the content can't be fetched or any file fails a
  real download attempt.

## tg_pdf_thread.py — batch-download the /tg/ PDF Share Thread

```bash
python tg_pdf_thread.py                  # find the thread, do it and its predecessor
python tg_pdf_thread.py --list-only      # just print the gofile IDs found (no gofile contact)
python tg_pdf_thread.py --dry-run        # fetch all listings, write nothing
python tg_pdf_thread.py 98169921         # start from a specific thread number
python tg_pdf_thread.py https://boards.4chan.org/tg/thread/98169921/pdf-share-thread
python tg_pdf_thread.py --depth 1        # current thread only
python tg_pdf_thread.py --depth 4        # follow previous-thread links 3 times
python tg_pdf_thread.py --output D:/pdfs # same meaning as gofile_dl --output
```

What it does:

1. Finds the current PDF Share Thread in the /tg/ catalog by subject (if the
   thread just got archived and no successor exists yet, it checks recently
   archived threads too).
2. Follows the OP's "Previous thread" link — verified by subject, so
   unrelated quote-links aren't followed — collecting gofile IDs from every
   post, including obfuscated forms like `g0f1le /d/3tDCbP`. Default depth
   is 2 (current + previous).
3. Downloads every ID through `gofile_dl.run()`, oldest thread first, with
   one shared account token and a 3-second courtesy delay between IDs.

At the end it prints a summary of succeeded vs. failed/dead IDs. Dead links
(`error-notFound`), private folders, and cold-storage files are normal in
these threads — they're reported and skipped, and don't stop the run.

If gofile becomes unusable mid-batch (network drop, rate limiting, service
outage), the run aborts immediately rather than grinding through the
remaining IDs; re-running later resumes where it left off thanks to the
skip logic.

## Notes

- **Be gentle with gofile.** It temp-bans IPs that hammer the API — that's
  why the batch tool paces itself and aborts on rate limiting. If both
  `gofile.io` and `api.gofile.io` suddenly time out, you're likely
  temp-banned; it usually clears in ~15 minutes.
- **Cold storage:** gofile moves inactive files to "cold storage" — the
  listing still shows them but the download returns 403 from a `cold-*`
  host. Retrieving those requires importing the file into a premium
  account; this tool just reports them as failed.
- **`error-wrongToken` on fresh tokens — or `error-notPremium` on content
  that opens fine in a browser** — usually means gofile rotated the salt in
  their website-token scheme. The tool now recovers on its own: it re-extracts
  the salt from gofile's live script, caches it, and retries the call. Run
  `python gofile_dl.py --refresh-wt` to do that by hand, or set
  `GOFILE_WT_SALT` to override the salt entirely. See CLAUDE.md
  ("Website token", "Salt self-healing").
