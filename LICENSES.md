# Third-party licences

Every library this project installs, and the licence it ships under, as
read from each package's installed distribution metadata (`pip show`
against a clean `pip install -r requirements.txt`). All are open
source (OSI-approved licences); no closed-source or paid dependency is
used anywhere in this repo.

## Direct dependencies (pinned in `requirements.txt`)

| Library | Pinned version | Licence | Used for |
|---|---|---|---|
| [httpx](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | HTTP client (no redirect auto-follow, no env proxies) |
| [selectolax](https://github.com/rushter/selectolax) | 0.4.12 | MIT | Fast HTML parsing (Modest/Lexbor bindings) |
| [Protego](https://github.com/scrapy/protego) | 0.7.0 | BSD-3-Clause | robots.txt parsing (Scrapy's RFC-9309-tolerant parser) |
| [openpyxl](https://openpyxl.readthedocs.io) | 3.1.5 | MIT | XLSX export |
| [pytest](https://docs.pytest.org/en/latest/) | 9.1.1 | MIT | Test suite |

## Transitive dependencies (not pinned; versions from a clean install)

| Library | Resolved version | Licence | Pulled in by |
|---|---|---|---|
| [anyio](https://pypi.org/project/anyio/) | 4.15.1 | MIT | httpx |
| [certifi](https://github.com/certifi/python-certifi) | 2026.7.22 | MPL-2.0 | httpx, httpcore |
| [httpcore](https://www.encode.io/httpcore/) | 1.0.9 | BSD-3-Clause | httpx |
| [idna](https://pypi.org/project/idna/) | 3.20 | BSD-3-Clause | httpx, anyio |
| [h11](https://github.com/python-hyper/h11) | 0.16.0 | MIT | httpcore |
| [typing_extensions](https://pypi.org/project/typing_extensions/) | 4.16.0 | PSF-2.0 | anyio |
| [et-xmlfile](https://foss.heptapod.net/openpyxl/et_xmlfile) | 2.0.0 | MIT | openpyxl |
| [iniconfig](https://github.com/pytest-dev/iniconfig) | 2.3.0 | MIT | pytest |
| [packaging](https://pypi.org/project/packaging/) | 26.3 | Apache-2.0 OR BSD-2-Clause | pytest |
| [pluggy](https://pypi.org/project/pluggy/) | 1.6.0 | MIT | pytest |
| [Pygments](https://pygments.org) | 2.21.0 | BSD-2-Clause | pytest |

The fixture site (`catalogue_fixture`) uses only the Python 3 standard
library (`http.server`, `dataclasses`, `threading`, `html`) — no
additional dependency.
