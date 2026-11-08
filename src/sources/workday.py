"""Workday career sites (the public job API every Workday site's own page uses).

Verified 2026-11-07 against
POST https://costar.wd1.myworkdayjobs.com/wday/cxs/costar/CoStarCareers/jobs
(fixture: tests/fixtures/workday_costar.json.gz).
List: body {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""} →
{total, jobPostings[] {title, externalPath, locationsText, postedOn
("Posted 9 Days Ago"), bulletFields[req id]}, facets}.
Detail: GET .../wday/cxs/{tenant}/{site}{externalPath} → jobPostingInfo {id,
title, jobDescription (HTML), location, additionalLocations?, timeType,
jobReqId, country {descriptor}, canApply, posted, externalUrl}.
Note: Workday's `startDate` is the date the POSTING started, not the job.

Board "slug" = "{tenant}.{wdN}/{site}", e.g. "costar.wd1/CoStarCareers".
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from ..pipeline.html_text import html_to_lines
from .base import BACKOFF_S, RETRIES, TIMEOUT_S, USER_AGENT, RawPosting, Source, SourceError

PAGE = 20
MAX_PAGES = 60          # up to 1,200 postings per site per poll
FILTERED_PAGES_FACTOR = 3   # read through your cities' filters: up to 3,600
MAX_NEW_DETAILS = 150       # new descriptions fetched per site per poll (cached; the rest come next poll)
_COUNTRY = {"united states of america": "US", "united states": "US", "canada": "CA"}


def split_slug(slug: str) -> tuple[str, str, str]:
    m = re.match(r"([a-z0-9-]+)\.(wd\d+)/([A-Za-z0-9_-]+)$", slug)
    if not m:
        raise SourceError(f"bad Workday board id {slug!r} (expected tenant.wdN/Site)")
    return m.group(1), m.group(2), m.group(3)


def base_url(slug: str) -> str:
    tenant, wd, site = split_slug(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def api_root(slug: str) -> str:
    tenant, wd, site = split_slug(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def posted_date(text: str | None, now: datetime | None = None) -> str | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 9 Days Ago' / 'Posted 30+ Days Ago' → ISO date."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*day", t)
        if not m:
            return None
        days = int(m.group(1))
    return (now - timedelta(days=days)).date().isoformat()


class WorkdaySource(Source):
    platform = "workday"

    def _request(self, method: str, url: str, sess: requests.Session, body: dict | None = None) -> Any:
        last = None
        for attempt in range(RETRIES):
            try:
                r = sess.request(method, url, json=body, timeout=TIMEOUT_S,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                          "Content-Type": "application/json"})
                if r.status_code in (404, 410):
                    raise SourceError(f"workday: {url} not found ({r.status_code})")
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except SourceError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if attempt < RETRIES - 1:
                    time.sleep(BACKOFF_S * (2 ** attempt))
        raise SourceError(f"workday: {url} failed: {last}")

    def fetch_list(self, slug: str, session: requests.Session | None = None, max_pages: int = MAX_PAGES,
                   place_ok: Callable[[str], bool] | None = None) -> "Listing":
        """Every posting on the site. A site too big to read in full (over max_pages) is
        read again with only its location filters that name one of your cities
        (`place_ok`); if it is still too big the result is marked truncated."""
        sess = session or requests.Session()
        items, total, first = self._pages(slug, sess, max_pages, {})
        out = Listing(items)
        if total > len(items) and max_pages > 1:
            ids = [v["id"] for v in _location_values(first.get("facets") or []) if place_ok and place_ok(v.get("descriptor") or "")]
            if ids:
                items, total, _ = self._pages(slug, sess, max_pages * FILTERED_PAGES_FACTOR, {"locations": ids})
                out = Listing(items)
                out.filtered_to = len(ids)
            out.truncated = total > len(items)
        return out

    def _pages(self, slug: str, sess: requests.Session, max_pages: int, facets: dict) -> tuple[list[dict], int, dict]:
        items, offset, total, first = [], 0, None, {}
        for _ in range(max_pages):
            page = self._request("POST", api_root(slug) + "/jobs", sess,
                                 {"appliedFacets": facets, "limit": PAGE, "offset": offset, "searchText": ""})
            if total is None:
                total, first = int(page.get("total") or 0), page
            posts = page.get("jobPostings") or []
            items += posts
            offset += PAGE
            if not posts or offset >= total:
                break
        return items, total or 0, first

    def fetch_payload_list(self, slug: str, session=None) -> dict:
        """One-request probe for discovery."""
        return {"pages": [{"jobPostings": self.fetch_list(slug, session, max_pages=1)}], "details": {}}

    def fetch(self, slug: str, session: requests.Session | None = None,
              want: Callable[[dict], bool] | None = None, cache: dict[str, dict] | None = None,
              place_ok: Callable[[str], bool] | None = None) -> list[RawPosting]:
        sess = session or requests.Session()
        items = self.fetch_list(slug, sess, place_ok=place_ok)
        details: dict[str, dict] = {}
        fetched, skipped = 0, set()
        for it in items:
            path = it.get("externalPath") or ""
            if cache is not None and path in cache:
                details[path] = cache[path]
            elif path and (want is None or want(it)):
                if fetched >= MAX_NEW_DETAILS:
                    skipped.add(path)       # judged next poll, once its description is in
                    continue
                fetched += 1
                try:
                    details[path] = self._request("GET", api_root(slug) + path, sess)
                except SourceError:
                    continue
                if cache is not None:
                    cache[path] = details[path]
        keep = [it for it in items if (it.get("externalPath") or "") not in skipped]
        out = Listing(self.parse({"pages": [{"jobPostings": keep}], "details": details}, slug))
        out.truncated = getattr(items, "truncated", False) or bool(skipped)
        return out

    def parse(self, payload: Any, slug: str, now: datetime | None = None) -> list[RawPosting]:
        details = payload.get("details") or {}
        out = []
        for page in payload.get("pages") or []:
            for it in page.get("jobPostings") or []:
                path = it.get("externalPath") or ""
                d = (details.get(path) or {}).get("jobPostingInfo") or {}
                if d and (d.get("canApply") is False or d.get("posted") is False):
                    continue
                locs = [d.get("location") or it.get("locationsText") or ""]
                locs += [x for x in d.get("additionalLocations") or [] if x]
                country = _COUNTRY.get(((d.get("country") or {}).get("descriptor") or "").lower())
                native = d.get("jobReqId") or (it.get("bulletFields") or [path])[0]
                out.append(RawPosting(
                    platform=self.platform, slug=slug, native_id=str(native or path),
                    title=(d.get("title") or it.get("title") or "").strip(),
                    url=d.get("externalUrl") or f"{base_url(slug)}/{split_slug(slug)[2]}{path}",
                    locations=[l for l in locs if l], country=country,
                    employment_type=d.get("timeType"),
                    description_lines=html_to_lines(d.get("jobDescription")),
                    posted_at=posted_date(it.get("postedOn") or d.get("postedOn"), now),
                    raw={"path": path, "has_detail": bool(d)}))
        return out


class Listing(list):
    """A list of postings that knows whether the site had more than we could read."""
    truncated = False
    filtered_to = 0


def _location_values(facets: list[dict]) -> list[dict]:
    """The values of the site's 'locations' filter (it can sit inside a group)."""
    for f in facets:
        vals = f.get("values") or []
        if f.get("facetParameter") == "locations":
            return [v for v in vals if "id" in v]
        nested = _location_values([v for v in vals if "facetParameter" in v])
        if nested:
            return nested
    return []
