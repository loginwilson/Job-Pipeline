"""Ashby public job-board API.

Verified 2026-11-05 against
https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
(fixtures: ashby_ramp.json.gz, ashby_eliseai.json.gz).
Response: {"apiVersion": "1", "jobs": [...]}. Each job has id, title,
department, team, employmentType (FullTime/PartTime/Intern/Contract/Temporary),
location, secondaryLocations[] {location, address}, address.postalAddress
{addressLocality, addressRegion, addressCountry}, workplaceType
(OnSite/Hybrid/Remote), isRemote, isListed, publishedAt, jobUrl, applyUrl,
descriptionHtml, descriptionPlain, and compensation.compensationTiers[]
{title (e.g. "SF/NY", "Canada"), components[] {compensationType: Salary /
EquityCashValue / EquityPercentage / Commission / Bonus, interval ("1 YEAR",
"1 MONTH", "NONE"), currencyCode, minValue, maxValue}}.
"""
from __future__ import annotations

from typing import Any

from ..pipeline.html_text import html_to_lines
from .base import CompRange, RawPosting, Source

_PERIOD = {"1 YEAR": "year", "1 HOUR": "hour", "1 MONTH": "month", "1 WEEK": "week", "1 DAY": "day"}
_COUNTRY = {"united states": "US", "usa": "US", "us": "US", "canada": "CA"}


class AshbySource(Source):
    platform = "ashby"

    def endpoint(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

    def parse(self, payload: Any, slug: str) -> list[RawPosting]:
        out = []
        for j in (payload or {}).get("jobs", []):
            if j.get("isListed") is False:
                continue
            locs = [j["location"]] if j.get("location") else []
            for sec in j.get("secondaryLocations") or []:
                if sec.get("location") and sec["location"] not in locs:
                    locs.append(sec["location"])
            addr = ((j.get("address") or {}).get("postalAddress") or {})
            country = _COUNTRY.get((addr.get("addressCountry") or "").strip().lower())

            ranges, extra = [], []
            for tier in (j.get("compensation") or {}).get("compensationTiers") or []:
                for comp in tier.get("components") or []:
                    ctype = comp.get("compensationType") or ""
                    if ctype in ("Salary", "Commission"):
                        ranges.append(CompRange(
                            comp.get("minValue"), comp.get("maxValue"), comp.get("currencyCode"),
                            _PERIOD.get(comp.get("interval") or ""), label=tier.get("title"),
                            kind="base" if ctype == "Salary" else "ote"))
                    elif ctype.startswith("Equity"):
                        quantified = comp.get("minValue") is not None or comp.get("maxValue") is not None
                        extra.append("equity" if quantified else "equity_unquantified")
                    elif ctype == "Bonus":
                        extra.append("bonus")
            workplace = j.get("workplaceType") or ("Remote" if j.get("isRemote") else None)
            out.append(RawPosting(
                platform=self.platform, slug=slug, native_id=str(j["id"]),
                title=(j.get("title") or "").strip(), url=j.get("jobUrl") or "",
                team=j.get("team"), department=j.get("department"),
                locations=locs, country=country, workplace_type=workplace,
                employment_type=j.get("employmentType"),
                description_lines=html_to_lines(j.get("descriptionHtml")),
                comp_ranges=ranges, comp_extra=sorted(set(extra)),
                posted_at=j.get("publishedAt"), raw=j,
            ))
        return out
