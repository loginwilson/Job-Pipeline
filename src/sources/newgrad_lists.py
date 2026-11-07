"""Community-maintained new-grad job lists on GitHub — used for DISCOVERY only.

Verified 2026-11-07: SimplifyJobs/New-Grad-Positions publishes every listing
it tracks as one JSON file (fixture: tests/fixtures/newgrad_list.json.gz):
  GET https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json
  → [ {company_name, title, url (apply link), locations[], active, is_visible,
        category, date_posted (unix), sponsorship, ...}, ... ]
A listing in one of your cities whose apply link goes to a Greenhouse / Lever /
Ashby / SmartRecruiters / Workday board gives us that company's board; every
role on it is then checked against the must-haves like any other board.
"""
from __future__ import annotations

import requests

from .base import TIMEOUT_S, USER_AGENT, SourceError


def fetch(url: str, session: requests.Session | None = None) -> list[dict]:
    sess = session or requests.Session()
    try:
        r = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S * 4)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise SourceError(f"new-grad list {url}: {exc}") from exc
    if not isinstance(data, list):
        raise SourceError(f"new-grad list {url}: expected a JSON list")
    return data


def open_listings(data: list[dict]) -> list[dict]:
    """Listings still open, as {company, url, locations}."""
    out = []
    for x in data:
        if not isinstance(x, dict) or not x.get("active") or x.get("is_visible") is False:
            continue
        if x.get("url") and x.get("company_name"):
            out.append({"company": str(x["company_name"]).strip(), "url": x["url"],
                        "locations": [str(l) for l in (x.get("locations") or []) if l]})
    return out
