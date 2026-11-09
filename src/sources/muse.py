"""The Muse public jobs API — searched by level and every criteria city.

Verified 2026-11-07 against https://www.themuse.com/api/public/jobs
(fixture: tests/fixtures/muse_entry_pages.json.gz). Response:
{page, page_count, items_per_page, total, results[]}; each result has id,
name (title), contents (HTML), publication_date, locations[] {name, e.g.
"New York, NY" / "Toronto, Canada" / "Flexible / Remote"}, levels[] {name},
company {id, short_name, name}, refs.landing_page (Muse job page with the
apply link). Repeated `location` params are OR-ed, so one paged query covers
every city. Results can't be sorted by date, so a sweep reads every page.
"""
from __future__ import annotations

import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from concurrent.futures import ThreadPoolExecutor

import requests

from ..config import Criteria, load_reference
from ..pipeline.html_text import html_to_lines
from ..textmatch import parse_when
from .base import TIMEOUT_S, USER_AGENT, RawPosting, Source, SourceError

API = "https://www.themuse.com/api/public/jobs"


def muse_locations(criteria: Criteria, reference: dict | None = None) -> list[str]:
    """'City, ST' / 'City, Canada' for every city in criteria location tiers."""
    geo = (reference or load_reference())["geo"]
    out: list[str] = []
    for tier in (criteria.get("location", "tiers") or {}).values():
        for region, cities in tier.items():
            if not isinstance(cities, list):
                continue
            meta = geo["regions"][region]
            names = list(cities) + [a for a in meta.get("aliases", []) if " " in a or a.istitle()]
            names = [n for n in names if n not in ("NYC", "GTA", "New York City", "Greater Toronto Area")]
            if meta["country"] == "CA":
                out += [f"{n}, Canada" for n in names]
            else:
                codes = [s for s in meta.get("states", []) if len(s) == 2 and s.isupper()]
                out += [f"{n}, {c}" for n in names for c in codes]
    return list(dict.fromkeys(out))


class MuseSource(Source):
    platform = "muse"

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}

    def page_url(self, page: int, locations: list[str]) -> str:
        params = [("page", page)] + [("level", lv) for lv in self.settings.get("levels", ["Entry Level"])]
        params += [("location", loc) for loc in locations]
        key = os.environ.get("MUSE_API_KEY")       # your registered key (free): identified, not anonymous
        if key:
            params.append(("api_key", key))
        return API + "?" + urllib.parse.urlencode(params)

    def get_page(self, page: int, locations: list[str], session: requests.Session) -> dict:
        url = self.page_url(page, locations)
        last = None
        for attempt in range(3):
            try:
                headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
                if os.environ.get("MUSE_API_KEY"):
                    headers["X-Muse-Api-Key"] = os.environ["MUSE_API_KEY"]   # some docs pass it as a header
                r = session.get(url, headers=headers, timeout=TIMEOUT_S)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        raise SourceError(f"The Muse page {page} failed: {last}")

    def sweep(self, locations: list[str], session: requests.Session | None = None,
              workers: int = 4) -> tuple[list[dict], bool]:
        """Read every page (4 at a time). Returns (pages, complete)."""
        sess = session or requests.Session()
        first = self.get_page(0, locations, sess)
        total = min(int(first.get("page_count", 0)), int(self.settings.get("max_pages", 120)))
        rest = list(range(1, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pages = [first] + list(pool.map(lambda p: self.get_page(p, locations, sess), rest))
        return pages, total == int(first.get("page_count", 0))

    def endpoint(self, slug: str) -> str:
        return self._url

    def parse(self, payload: Any, slug: str = "", now: datetime | None = None) -> list[RawPosting]:
        pages = payload if isinstance(payload, list) else [payload]
        max_age = int(self.settings.get("max_age_days", 60))
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age)
        out = []
        for page in pages:
            for r in page.get("results", []):
                pub = r.get("publication_date")
                try:
                    when = parse_when(pub)
                    if when and when < cutoff:
                        continue
                except ValueError:
                    pass
                comp = r.get("company") or {}
                locs = [l.get("name", "") for l in r.get("locations") or [] if l.get("name")]
                remote_only = bool(locs) and all("remote" in l.lower() for l in locs)
                out.append(RawPosting(
                    platform=self.platform, slug=comp.get("short_name") or str(comp.get("id", "")),
                    native_id=str(r["id"]), title=(r.get("name") or "").strip(),
                    url=(r.get("refs") or {}).get("landing_page", ""),
                    locations=[l for l in locs if "remote" not in l.lower()] or locs,
                    workplace_type="remote" if remote_only else None,
                    description_lines=html_to_lines(r.get("contents")),
                    posted_at=pub, company=comp.get("name") or comp.get("short_name"), raw=r))
        return out
