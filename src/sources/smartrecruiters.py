"""SmartRecruiters public postings API.

Verified 2026-11-07 against
https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=N
(fixture: tests/fixtures/smartrecruiters_servicenow.json.gz).
List response: {offset, limit, totalFound, content[]}; each item has id, name
(title), company {identifier, name}, releasedDate, location {city, region,
country, remote, hybrid, fullLocation}, typeOfEmployment {label},
experienceLevel {label}, department {label}, ref (detail URL).
Detail response adds postingUrl, applyUrl, active (bool) and
jobAd.sections {companyDescription, jobDescription, qualifications,
additionalInformation} each {title, text (HTML)}.
An unknown company returns 200 with totalFound 0.

The list is read every run; details are fetched only for postings the caller
wants (new, in a listed city, passing title rules) and cached by the caller.
"""
from __future__ import annotations

from typing import Any, Callable

import requests

from ..pipeline.html_text import html_to_lines
from .base import RawPosting, Source, SourceError

PAGE = 100
MAX_PAGES = 20


class SmartRecruitersSource(Source):
    platform = "smartrecruiters"

    def endpoint(self, slug: str) -> str:
        return self._url

    def list_url(self, slug: str, offset: int) -> str:
        return f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={PAGE}&offset={offset}"

    def fetch_list(self, slug: str, session: requests.Session | None = None) -> list[dict]:
        sess = session or requests.Session()
        items, offset = [], 0
        for _ in range(MAX_PAGES):
            self._url = self.list_url(slug, offset)
            page = self.fetch_payload(slug, sess)
            content = page.get("content") or []
            items += content
            offset += PAGE
            if not content or offset >= int(page.get("totalFound", 0)):
                break
        return items

    def fetch_payload_list(self, slug: str, session=None) -> dict:
        """Single-request probe used by discovery: {"pages": [first page]}."""
        self._url = self.list_url(slug, 0)
        return {"pages": [self.fetch_payload(slug, session)], "details": {}}

    def fetch(self, slug: str, session: requests.Session | None = None,
              want: Callable[[dict], bool] | None = None,
              cache: dict[str, dict] | None = None) -> list[RawPosting]:
        sess = session or requests.Session()
        items = self.fetch_list(slug, sess)
        details: dict[str, dict] = {}
        for it in items:
            pid = str(it["id"])
            if cache is not None and pid in cache:
                details[pid] = cache[pid]
            elif want is None or want(it):
                self._url = it.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}"
                try:
                    details[pid] = self.fetch_payload(slug, sess)
                except SourceError:
                    continue
                if cache is not None:
                    cache[pid] = details[pid]
        return self.parse({"pages": [{"content": items}], "details": details}, slug)

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        if isinstance(payload, dict) and "pages" not in payload and "content" in payload:
            payload = {"pages": [payload], "details": {}}
        details = payload.get("details") or {}
        out = []
        for page in payload.get("pages") or []:
            for it in page.get("content") or []:
                pid = str(it["id"])
                d = details.get(pid) or {}
                if d and d.get("active") is False:
                    continue
                loc = it.get("location") or {}
                lines: list[str] = []
                for sec in ((d.get("jobAd") or {}).get("sections") or {}).values():
                    if sec.get("title"):
                        lines.append("## " + sec["title"].rstrip(":"))
                    lines += html_to_lines(sec.get("text"))
                workplace = "remote" if loc.get("remote") and not loc.get("hybrid") else (
                    "hybrid" if loc.get("hybrid") else None)
                country = (loc.get("country") or "").upper() or None
                out.append(RawPosting(
                    platform=self.platform, slug=slug, native_id=pid, title=(it.get("name") or "").strip(),
                    url=d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                    team=(it.get("department") or {}).get("label"),
                    locations=[loc["fullLocation"]] if loc.get("fullLocation") else [],
                    country=country, workplace_type=workplace,
                    employment_type=(it.get("typeOfEmployment") or {}).get("label"),
                    description_lines=lines, posted_at=it.get("releasedDate"),
                    company=(it.get("company") or {}).get("name"), raw={"list": it, "has_detail": bool(d)}))
        return out
