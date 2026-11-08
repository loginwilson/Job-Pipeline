"""Oracle Recruiting Cloud career sites (the public REST API their own career pages use).

Board "slug" = "{host}/{siteNumber}", e.g. "jpmc.fa.oraclecloud.com/CX_1001", from
links like https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{id}.
List:   GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},facetsList=LOCATIONS,limit=25,offset=N,sortBy=POSTING_DATES_DESC
        → items[0] {TotalJobsCount, requisitionList[] {Id, Title, PostedDate,
          PrimaryLocation, PrimaryLocationCountry, secondaryLocations[] {Name},
          WorkplaceType, JobSchedule}, locationsFacet[] {Id, Name, TotalCount}}
        A location filter is finder param selectedLocationsFacet=id1;id2.
Detail: GET .../recruitingCEJobRequisitionDetails?expand=all&onlyData=true
        &finder=ById;Id="{id}",siteNumber={site}
        → items[0] {Title, ExternalDescriptionStr, ExternalResponsibilitiesStr,
          ExternalQualificationsStr, JobSchedule, WorkplaceType, PrimaryLocation, ...}
Only postings that could pass on title + location get a detail request (cached).
"""
from __future__ import annotations

import time
from typing import Any, Callable

import requests

from ..pipeline.html_text import html_to_lines
from .base import BACKOFF_S, RETRIES, TIMEOUT_S, USER_AGENT, RawPosting, Source, SourceError
from .workday import Listing

PAGE = 25
MAX_PAGES = 48          # up to 1,200 postings per site per poll
FILTERED_PAGES_FACTOR = 3   # read through your cities' filters: up to 3,600
MAX_NEW_DETAILS = 80        # new descriptions fetched per site per poll (cached; the rest come next poll)
API = "https://{host}/hcmRestApi/resources/latest/"


def split_slug(slug: str) -> tuple[str, str]:
    host, _, site = slug.partition("/")
    if not host or not site:
        raise SourceError(f"oracle: bad board id {slug!r} (want host/siteNumber)")
    return host, site


class OracleSource(Source):
    platform = "oracle"

    def _get(self, url: str, sess: requests.Session) -> Any:
        last = None
        for attempt in range(RETRIES):
            try:
                r = sess.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT_S)
                if r.status_code == 404:
                    raise SourceError(f"oracle: {url} not found")
                r.raise_for_status()
                return r.json()
            except SourceError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if attempt < RETRIES - 1:
                    time.sleep(BACKOFF_S * (2 ** attempt))
        raise SourceError(f"oracle: {url} failed: {last}")

    def _list_url(self, slug: str, offset: int, location_ids: list[str] | None = None) -> str:
        host, site = split_slug(slug)
        finder = f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit={PAGE},offset={offset},sortBy=POSTING_DATES_DESC"
        if location_ids:
            finder += ",selectedLocationsFacet=" + ";".join(location_ids)
        return (API.format(host=host) + "recruitingCEJobRequisitions?onlyData=true"
                "&expand=requisitionList.secondaryLocations&finder=" + finder)

    def _pages(self, slug, sess, max_pages, location_ids=None):
        items, total, facets = [], 0, []
        for n in range(max_pages):
            data = self._get(self._list_url(slug, n * PAGE, location_ids), sess)
            block = (data.get("items") or [{}])[0]
            if n == 0:
                total = int(block.get("TotalJobsCount") or 0)
                facets = block.get("locationsFacet") or []
            reqs = block.get("requisitionList") or []
            items += reqs
            if not reqs or len(items) >= total:
                break
        return items, total, facets

    def fetch_list(self, slug: str, session: requests.Session | None = None, max_pages: int = MAX_PAGES,
                   place_ok: Callable[[str], bool] | None = None) -> Listing:
        sess = session or requests.Session()
        items, total, facets = self._pages(slug, sess, max_pages)
        out = Listing(items)
        if total > len(items) and max_pages > 1:
            ids = [str(f["Id"]) for f in facets if f.get("Id") and place_ok and place_ok(f.get("Name") or "")]
            if ids:
                items, total, _ = self._pages(slug, sess, max_pages * FILTERED_PAGES_FACTOR, ids)
                out = Listing(items)
                out.filtered_to = len(ids)
            out.truncated = total > len(items)
        return out

    def fetch_payload(self, slug: str, session: requests.Session | None = None) -> Any:
        return self.fetch_payload_list(slug, session)

    def fetch_payload_list(self, slug: str, session=None) -> dict:
        """One-request probe for discovery."""
        return {"pages": [{"requisitionList": self.fetch_list(slug, session, max_pages=1)}], "details": {}}

    def fetch(self, slug: str, session: requests.Session | None = None,
              want: Callable[[dict], bool] | None = None, cache: dict[str, dict] | None = None,
              place_ok: Callable[[str], bool] | None = None) -> list[RawPosting]:
        sess = session or requests.Session()
        host, site = split_slug(slug)
        items = self.fetch_list(slug, sess, place_ok=place_ok)
        details: dict[str, dict] = {}
        fetched, skipped = 0, set()
        for it in items:
            rid = str(it.get("Id") or "")
            if cache is not None and rid in cache:
                details[rid] = cache[rid]
            elif rid and (want is None or want(it)):
                if fetched >= MAX_NEW_DETAILS:
                    skipped.add(rid)       # judged next poll, once its description is in
                    continue
                fetched += 1
                url = (API.format(host=host) + "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                       f"&finder=ById;Id=%22{rid}%22,siteNumber={site}")
                try:
                    details[rid] = (self._get(url, sess).get("items") or [{}])[0]
                except SourceError:
                    continue
                if cache is not None:
                    cache[rid] = details[rid]
        keep = [it for it in items if str(it.get("Id") or "") not in skipped]
        out = Listing(self.parse({"pages": [{"requisitionList": keep}], "details": details}, slug))
        out.truncated = getattr(items, "truncated", False) or bool(skipped)
        return out

    def parse(self, payload: Any, slug: str, now=None) -> list[RawPosting]:
        host, site = split_slug(slug)
        details = payload.get("details") or {}
        out = []
        for page in payload.get("pages") or []:
            for it in page.get("requisitionList") or []:
                rid = str(it.get("Id") or "")
                if not rid:
                    continue
                d = details.get(rid) or {}
                locs = locations_of(it) + [x for x in locations_of(d) if x not in locations_of(it)]
                html = "\n".join(x for x in (d.get("ExternalDescriptionStr"), d.get("ExternalResponsibilitiesStr"),
                                             d.get("ExternalQualificationsStr")) if x)
                wt = (d.get("WorkplaceType") or it.get("WorkplaceType") or "").lower()
                out.append(RawPosting(
                    platform=self.platform, slug=slug, native_id=rid,
                    title=(d.get("Title") or it.get("Title") or "").strip(),
                    url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                    locations=locs, country=(it.get("PrimaryLocationCountry") or None),
                    workplace_type="remote" if "remote" in wt else "hybrid" if "hybrid" in wt else
                                   "onsite" if ("site" in wt or "office" in wt) else None,
                    employment_type=d.get("JobSchedule") or it.get("JobSchedule"),
                    description_lines=html_to_lines(html),
                    posted_at=(it.get("PostedDate") or d.get("ExternalPostedStartDate") or None),
                    raw={"id": rid, "has_detail": bool(d)}))
        return out


def locations_of(item: dict) -> list[str]:
    locs = [item.get("PrimaryLocation") or ""]
    locs += [(x.get("Name") or "") for x in (item.get("secondaryLocations") or []) if isinstance(x, dict)]
    return [l for l in dict.fromkeys(locs) if l]
