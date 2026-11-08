"""Workable career sites (the public job-widget API every Workable careers page offers).

  GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
  → {name, description, jobs[] {title, shortcode, code, employment_type, telecommuting,
     department, url, shortlink, application_url, published_on, created_at, country,
     city, state, education, experience, function, industry, locations[] {country,
     countryCode, city, region}, description (HTML)}}
Board "slug" = the account in https://apply.workable.com/{slug}/... links.
One request per board returns every posting with its description.
"""
from __future__ import annotations

from typing import Any

from ..pipeline.html_text import html_to_lines
from .base import RawPosting, Source

_COUNTRY = {"united states": "US", "us": "US", "usa": "US", "canada": "CA", "ca": "CA"}


class WorkableSource(Source):
    platform = "workable"

    def endpoint(self, slug: str) -> str:
        return f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        out = []
        for j in (payload or {}).get("jobs") or []:
            locs = []
            for l in j.get("locations") or []:
                parts = [l.get("city"), l.get("region"), l.get("country")]
                locs.append(", ".join(p for p in parts if p))
            if not locs:
                locs = [", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)]
            country = _COUNTRY.get(str(j.get("country") or "").strip().lower())
            out.append(RawPosting(
                platform=self.platform, slug=slug, native_id=str(j.get("shortcode") or j.get("code") or j.get("url")),
                title=(j.get("title") or "").strip(),
                url=j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
                locations=[l for l in locs if l], country=country,
                workplace_type="remote" if j.get("telecommuting") else None,
                employment_type=j.get("employment_type"),
                department=j.get("department"),
                description_lines=html_to_lines(j.get("description")),
                posted_at=j.get("published_on") or j.get("created_at")))
        return out
