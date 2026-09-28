# authorised-catalogue-extraction

**Synthetic portfolio demonstration, implemented with AI coding agents;
independent review pending. No client data or client work.**

A web scraper that extracts a product catalogue into structured rows
and monitors it for changes between runs — built and tested only
against a local, synthetic fixture site that this repository generates
and serves itself, on `127.0.0.1`. Upwork job types this demonstrates:
**"web scraping to CSV/Excel"**, **"price monitoring"**, **"product
catalogue extraction"**.

## Hard boundaries

- This scraper has only ever run against the local fixture in
  `src/catalogue_fixture`. It has never touched a real third-party
  site, and it does not scrape Upwork or any marketplace.
- No login or authentication bypass, no CAPTCHA handling, no personal
  or lead data — the fixture has none of these, and the scraper has no
  code path for them.
- It runs only against sources it owns or has written permission to
  crawl. That permission is a file (`PERMISSION.md`) the target site
  itself serves and the scraper hashes into its run manifest — see
  [Permission and robots](#permission-and-robots) below.

## What it does

1. **Crawl** (`catalogue_extract crawl`) — given one exact origin and a
   list of allowed path prefixes, discovers listing and detail pages,
   extracts products, and writes a JSON "generation" plus (if the
   crawl was complete) advances a `current` pointer.
2. **Diff** (`catalogue_extract diff`) — compares two generations and
   reports added / removed / price-changed / unchanged product IDs, or
   `INDETERMINATE` with reasons if either side wasn't a complete crawl.
3. **Export** (`catalogue_extract export`) — writes the current
   generation's products to CSV, XLSX, or a JSON accounting report.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## One-command demo

```bash
./run_demo.sh
```

Runs the fixture and the scraper in a single process
(`scripts/demo.py`): a baseline crawl, a deliberate catalogue change
(one product removed, one added, one repriced), a second crawl, a
change report, and CSV/XLSX exports — all under `data/demo_run/`
(git-ignored; generated, not source).

## Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

60+ tests, all run in-process against private `FixtureServer`
instances on ephemeral loopback ports — no shared state between tests,
nothing left running afterwards. Runtime is ~35s, dominated by tests
that deliberately exercise real backoff/retry timing.

## Architecture

```
src/catalogue_fixture/   the synthetic site: deterministic product generator,
                         v1/v2 HTML templates, a threaded stdlib HTTP server
                         with test-controlled robots/error/version injection
src/catalogue_extract/   the scraper: boundary enforcement, robots, rate
                         limiting/retry, fetch, HTML parsing, crawl
                         orchestration, snapshot storage, diff, export
scripts/demo.py          single-process driver used by run_demo.sh
tests/                   pytest suite, one FixtureServer per test
```

Product records come **exclusively from detail pages**; listing pages
only ever yield discovery links (more listing pages, or detail links).
Listing and detail records never overlap.

## Permission and robots

These are deliberately two separate gates, checked in this order, with
the stricter outcome on failure:

1. **Permission.** The scraper fetches `<origin>/PERMISSION.md` before
   anything else. It must return `200`; its bytes are SHA-256-hashed
   and the hash + fetch time go into the run manifest. Unreachable or
   non-200 → the run is `refused`, nothing else is fetched.
2. **Robots.** Parsed with [Protego](https://github.com/scrapy/protego)
   0.7.0 (pinned), which implements the tolerant, group-merging
   semantics of [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html)
   (checked 2026-09-28) plus the common `*`/`$` wildcard extensions.
   `Crawl-delay` is parsed by Protego but **not used** — this demo
   always uses its own fixed rate limit instead. Missing (404),
   rate-limited (429), erroring (5xx), or an unreadable/non-UTF-8 body
   → the run is `refused`. **This is this demo's own, stricter-than-RFC
   policy** — RFC 9309 does not itself require refusing on a missing
   robots.txt; we choose to, on the theory that "no policy found" is
   not the same as "no policy". robots.txt bytes/hash/fetch time are
   recorded the same way as PERMISSION.md.

Only after both pass does the crawl attempt any listing or detail
page, and `can_fetch()` is still checked per-URL against the parsed
robots rules throughout.

## Scope enforcement

Every candidate URL — the seed, a pagination link, a detail link, or a
redirect `Location` — is checked against a `Scope` (exact scheme, host,
port, plus an allow-list of path prefixes) **before** any request is
made:

- Credentials in the URL, a different scheme, host or port, or a path
  outside the allow-list are all refused pre-request.
- A second service on `127.0.0.1` on a different port is **not**
  treated as authorised just because it's loopback — it's out of scope
  exactly like a public third-party host (see
  `tests/test_boundary.py::test_another_loopback_service_is_not_authorised`).
- Percent-encoded traversal (`%2e%2e`) is decoded and normalized before
  the scope check, so it can't sneak a path outside the allow-list.
- `httpx`'s automatic redirect following is **never** used
  (`follow_redirects` is off everywhere). Each redirect hop is resolved
  with `urljoin`, re-checked against scope, and tracked against the
  visited chain — an out-of-scope redirect target is refused without
  ever being requested, and a redirect back to an already-visited URL
  is a detected loop, not an infinite fetch.
- Environment proxies are disabled (`httpx.Client(trust_env=False)`).
- No browser and no JavaScript execution is used or needed — the
  fixture's markup is fully present in the initial HTML response.

`tests/test_boundary.py` and the boundary cases in
`tests/test_ratelimit_retry.py` cover: an absolute external link, a
protocol-relative external link, another loopback service, a
credentials-in-URL link, encoded path traversal, a redirect leaving the
origin, and a redirect loop.

## Politeness

- One concurrent request, always (the crawler is single-threaded by
  construction).
- At least 1 second between requests plus seeded jitter
  (`CrawlConfig.min_interval_s` / `jitter_s`, both overridable —
  the test suite uses smaller values purely for its own runtime).
- Connect/read timeouts, a 2 MB response-byte cap (checked after the
  full body is read, not streamed — a real large-scale crawler should
  stream and cap incrementally; this demo's fixture pages are all
  small, so this limitation doesn't affect its own correctness, but
  it is a real gap against a hostile server), a page-count cap, a
  redirect-hop cap (5), and a **maximum of 5 attempts per URL**.
- A shared **120-second retry-time budget** for the whole crawl.
  `Retry-After` is parsed as either delta-seconds or an HTTP-date
  (RFC 9110 §10.2.3). If the parsed wait would exceed the *remaining*
  budget, the fetch fails immediately with `retry_budget_exceeded`
  rather than sleeping a truncated amount and retrying early.
- Every attempt (status/error, elapsed time, wait-before) is recorded
  against its page, not just the final outcome.
- User-Agent identifies the scraper:
  `catalogue-extract-demo/1.0 (+authorised fixture crawl; contact: demo-bot)`.

## Accounting (what "complete" means)

On run termination, for the discovered in-scope URL set:

```
unique in-scope URLs = parsed + failed + skipped_by_robots + unattempted_limit
```

mutually exclusive, computed from a canonical-URL-keyed map so repeat
links and retries never inflate discovery. A page popped after the
page-count limit is hit is `unattempted_limit`, never silently
dropped. Out-of-scope links are reported separately, with **zero**
fetches against them. Permission/robots/snapshot-meta requests are
counted separately as `control_requests`, not page accounting.

At the product level, `parsed` pages may yield zero or many records:
`accepted`, `exact_duplicate` (same ID, identical data, multiple
source URLs — merged), `conflicting_id` (same ID, different data —
rejected, fails completeness), and `rejected` (missing required field,
unknown/ambiguous price, or `template_unrecognised`). A crawl is
`complete` only when **none** of failed / unattempted / robots-skipped
/ conflicting / rejected pages occurred **and** the fixture's
snapshot ID/version was stable from before the first request to after
the last (see below) — otherwise it's `incomplete`, with the specific
reasons listed. This is a deliberately strict definition: a single
unsupported-template page or a single 404 marks the whole crawl
incomplete, rather than trying to guess how much of it can still be
trusted.

## Change monitoring

`added` / `removed` / `price_changed` / `unchanged` is only ever
computed between **two complete generations**. Anything less (a
`refused` or `incomplete` crawl on either side, or no prior generation
at all) returns `INDETERMINATE` with diagnostics — a detail-page 404
by itself never becomes a "removed" claim, and a failed crawl never
overwrites the `current` pointer, so the next diff still compares
against the last good baseline. Products are matched by stable ID, not
URL or listing order. A currency change on the same ID is reported
separately as `currency_changed_anomaly` and is never folded into
`price_changed` (amounts in different currencies are never compared or
summed as if interchangeable). Price is always an integer minor-unit
amount plus an explicit currency; a missing or ambiguous price is
`unknown`/`ambiguous`, never `0`.

Generations are stored under `<out>/generations/<crawl_id>/` and are
**never deleted** by this code. `<out>/current` is a plain text pointer
updated via write-temp-then-`os.replace` (atomic on the same
filesystem) and only ever advanced by a `complete` crawl — this repo
makes no broader guarantee about generations still open by a reader
during a hypothetical future GC pass, because it never runs one.

## Export safety

CSV and XLSX rows sanitize any text field starting with `=`, `+`, `-`,
`@`, tab, or carriage-return by prefixing a leading single quote — the
conventional Excel/Sheets "force text" marker on import. **This is not
a universal CSV-injection guarantee**: plain CSV has no native
escaping for spreadsheet formulas, and not every consumer honours the
leading-quote convention (see `tests/test_export.py`, which crawls a
fixture product deliberately named `=cmd|'/c calc'!A1` and checks both
the export mitigation and that the unmodified name is still present,
untouched, in the underlying JSON generation).

## Known limitations (narrowing the claims to what's tested)

- Response-size limiting is post-download, not streamed (see
  Politeness above).
- The retry-time budget is one shared pool for the whole crawl, not
  per-URL — the brief text is ambiguous between the two readings; this
  is the documented choice.
- "Malformed" robots.txt here means non-UTF-8 bytes or an embedded NUL;
  anything else is left to Protego's own tolerant parsing. This is
  this demo's supported policy subset, not a claim of RFC 9309
  compliance testing or universal real-world robots.txt compatibility.
- Only two template generations (v1, v2) are modelled; a third,
  genuinely different layout would correctly fall through to
  `template_unrecognised` rather than being silently mis-parsed, but
  there's no claim of broader template coverage.
- Completeness is intentionally strict (see Accounting) — this
  favours refusing to publish a change claim over guessing.

## Repository layout

```
src/catalogue_extract/   the scraper library + CLI (python -m catalogue_extract)
src/catalogue_fixture/   the synthetic site (python -m catalogue_fixture to run standalone)
scripts/demo.py          single-process demo driver (used by run_demo.sh)
tests/                   pytest suite
data/                    generated at runtime; git-ignored
```

## Licences

See [LICENSES.md](LICENSES.md) — every direct and transitive
dependency, all OSI-approved licences, read from installed package
metadata.
