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

    def fetch_list(self, slug: str, session: requests.Session | None = None, max_pages: int = MAX_PAGES) -> list[dict]:
        sess = session or requests.Session()
        items, offset, total = [], 0, None
        for _ in range(max_pages):
            page = self._request("POST", api_root(slug) + "/jobs", sess,
                                 {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""})
            if total is None:
                total = int(page.get("total") or 0)
            posts = page.get("jobPostings") or []
            items += posts
            offset += PAGE
            if not posts or offset >= total:
                break
        return items

    def fetch_payload_list(self, slug: str, session=None) -> dict:
        """One-request probe for discovery."""
        return {"pages": [{"jobPostings": self.fetch_list(slug, session, max_pages=1)}], "details": {}}

    def fetch(self, slug: str, session: requests.Session | None = None,
              want: Callable[[dict], bool] | None = None, cache: dict[str, dict] | None = None) -> list[RawPosting]:
        sess = session or requests.Session()
        items = self.fetch_list(slug, sess)
        details: dict[str, dict] = {}
        for it in items:
            path = it.get("externalPath") or ""
            if cache is not None and path in cache:
                details[path] = cache[path]
            elif path and (want is None or want(it)):
                try:
                    details[path] = self._request("GET", api_root(slug) + path, sess)
                except SourceError:
                    continue
                if cache is not None:
                    cache[path] = details[path]
        return self.parse({"pages": [{"jobPostings": items}], "details": details}, slug)

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
