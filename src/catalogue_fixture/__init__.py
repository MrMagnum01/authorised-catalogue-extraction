DEFAULT_PERMISSION_TEMPLATE = """# Crawl permission

Site: {origin}
Allowed-Paths: /catalogue/
Purpose: automated testing of the authorised-catalogue-extraction demo scraper
Permission: granted

This file is the authorisation, in catalogue_extract.permission's exact
grammar (see that module's docstring). robots.txt on this site is a
separate, machine-readable crawl-policy layer and is not itself the
permission grant.
"""
