"""Greenhouse job board API.

Response shape verified 2026-11-05 against live boards
(fixtures: greenhouse_withcoverage.json.gz, greenhouse_figma.json.gz).
Response: {"jobs": [...], "meta": {"total": n}}. Each job has id, title,
absolute_url, location {name}, offices[] {name, location}, departments[] {name},
content (HTML that is itself HTML-escaped), updated_at, first_published, and,
with pay_transparency=true, pay_input_ranges[] {min_cents, max_cents,
currency_type, title, blurb}. The same single request returns pay ranges, so
the one-request-per-company rule holds.
"""
from __future__ import annotations

import re

from typing import Any

from ..pipeline.html_text import html_to_lines
from .base import CompRange, RawPosting, Source


class GreenhouseSource(Source):
    platform = "greenhouse"

    def endpoint(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true"

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        out = []
        for j in (payload or {}).get("jobs", []):
            locs = []
            name = ((j.get("location") or {}).get("name") or "").strip()
            if name:
                locs.append(name)
            # offices are the company's internal groupings ("HQ" = San Jose for a São Paulo job):
            # used only when the job's own location is blank or vague
            vague = not name or re.search(r"multiple|various|locations|anywhere|flexible", name, re.I)
            for office in (j.get("offices") or []) if vague else []:
                loc = (office.get("location") or office.get("name") or "").strip()
                if loc and loc not in locs:
                    locs.append(loc)
            lines = html_to_lines(j.get("content"), unescape_first=True)
            ranges = []
            for r in j.get("pay_input_ranges") or []:
                title = r.get("title") or ""
                period = "hour" if "hour" in title.lower() else None
                lo = r.get("min_cents")
                hi = r.get("max_cents")
                ranges.append(CompRange(lo / 100 if lo is not None else None,
                                        hi / 100 if hi is not None else None,
                                        r.get("currency_type"), period, label=title, kind="unknown"))
                if r.get("blurb"):
                    lines.extend(html_to_lines(r["blurb"]))
            depts = [d.get("name", "").strip() for d in j.get("departments") or [] if d.get("name")]
            out.append(RawPosting(
                platform=self.platform, slug=slug, native_id=str(j["id"]),
                title=(j.get("title") or "").strip(), url=j.get("absolute_url") or "",
                team=", ".join(depts) or None, department=depts[0] if depts else None,
                locations=locs, description_lines=lines, comp_ranges=ranges,
                posted_at=j.get("first_published") or j.get("updated_at"), raw=j,
            ))
        return out
