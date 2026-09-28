# Crawl permission (repo-level)

This repository only ever crawls a synthetic, local product-catalogue
website that it generates and serves itself, on `127.0.0.1`, for the
sole purpose of demonstrating and testing the scraper in
`src/catalogue_extract`. There is no real client, business or website
behind it.

The fixture site (`src/catalogue_fixture`) serves its own
`PERMISSION.md` at its origin root, in the exact grammar the scraper
validates (`src/catalogue_extract/permission.py`): a `Site:` line that
must match the crawl's exact origin, `Allowed-Paths:` (which the CLI's
own `--allow` prefixes must fall under, never exceed), a `Purpose:`
line, and `Permission: granted`. A `200` response alone is not
authorisation — the scraper parses and validates this content before
crawling anything; a body that fails the grammar, denies permission, or
names a different site refuses the whole run. The scraper also
SHA-256-hashes the raw file on every run and records the hash (plus the
parsed site/paths/purpose) in the run manifest — see
`README.md#permission-and-robots`.

Nothing in this repository is used, or is intended to be used, against
any third-party site, Upwork, or any other marketplace. See
`README.md#hard-boundaries`.
