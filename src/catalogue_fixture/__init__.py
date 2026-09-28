DEFAULT_PERMISSION_TEXT = """# Crawl permission

Site: synthetic-catalogue.local (served only on 127.0.0.1, for demo purposes)
Owner: this repository's author (demo-bot). No real business, brand or data.
Permitted paths: /catalogue/ and its detail pages under /catalogue/product/
Permitted purpose: automated testing of the authorised-catalogue-extraction demo scraper.
Not permitted: anything outside /catalogue/, any other host or port, any login flow.

This file is the authorisation. robots.txt on this site is a separate,
machine-readable crawl-policy layer and is not itself the permission grant.
"""
