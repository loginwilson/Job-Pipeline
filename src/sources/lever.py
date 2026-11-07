"""Lever public postings API.

Response shape verified 2026-11-05 against a live board (fixture:
tests/fixtures/lever_palantir.json.gz). Each posting has:
  id, text (title), hostedUrl, applyUrl, createdAt (ms epoch), country (ISO-2),
  workplaceType (onsite/hybrid/remote), categories {commitment, location, team,
  allLocations[]}, description/descriptionPlain (opening + body),
  lists[] {text: heading, content: <li> html}, additional/additionalPlain.
Many boards put pay only in the `additional` text. Lever also documents an
optional salaryRange {min, max, currency, interval}; it is read if present.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..pipeline.html_text import html_to_lines
from .base import CompRange, RawPosting, Source

_INTERVALS = {"per-year-salary": "year", "per-hour-wage": "hour", "per-month-salary": "month",
              "per-week-salary": "week", "per-day-wage": "day"}


class LeverSource(Source):
    platform = "lever"

    def endpoint(self, slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        out = []
        for p in payload or []:
            cats = p.get("categories") or {}
            lines = html_to_lines(p.get("description"))
            for lst in p.get("lists") or []:
                heading = (lst.get("text") or "").strip()
                if heading:
                    lines.append("## " + heading.rstrip(":").strip())
                lines.extend(html_to_lines(lst.get("content")))
            additional = html_to_lines(p.get("additional"))
            if additional:
                # boundary so closing boilerplate never joins the last list section
                lines.append("## Additional information")
                lines.extend(additional)

            ranges = []
            sr = p.get("salaryRange")
            if isinstance(sr, dict) and (sr.get("min") or sr.get("max")):
                ranges.append(CompRange(sr.get("min"), sr.get("max"), sr.get("currency"),
                                        _INTERVALS.get(sr.get("interval") or "", None)))
            if p.get("salaryDescriptionPlain"):
                lines.append(p["salaryDescriptionPlain"])

            locs = list(cats.get("allLocations") or [])
            if cats.get("location") and cats["location"] not in locs:
                locs.insert(0, cats["location"])
            created = p.get("createdAt")
            posted = (datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
                      if isinstance(created, (int, float)) else None)
            out.append(RawPosting(
                platform=self.platform, slug=slug, native_id=str(p["id"]),
                title=(p.get("text") or "").strip(), url=p.get("hostedUrl") or "",
                team=cats.get("team"), department=cats.get("department"),
                locations=locs, country=p.get("country"),
                workplace_type=p.get("workplaceType"), employment_type=cats.get("commitment"),
                description_lines=lines, comp_ranges=ranges, posted_at=posted, raw=p,
            ))
        return out
