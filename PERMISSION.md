# Crawl permission (repo-level)

This repository only ever crawls a synthetic, local product-catalogue
website that it generates and serves itself, on `127.0.0.1`, for the
sole purpose of demonstrating and testing the scraper in
`src/catalogue_extract`. There is no real client, business or website
behind it.

The fixture site (`src/catalogue_fixture`) serves its own
`PERMISSION.md` at its origin root, naming the exact site, the
permitted paths (`/catalogue/` and its detail pages) and the permitted
purpose. The scraper fetches and SHA-256-hashes that file on every run
and records the hash in the run manifest — see
`README.md#permission-and-robots`.

Nothing in this repository is used, or is intended to be used, against
any third-party site, Upwork, or any other marketplace. See
`README.md#hard-boundaries`.
