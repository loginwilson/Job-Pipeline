"""RawPosting -> Posting (BUILD_BRIEF Step 3 schema)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Company, Criteria
from ..sources.base import RawPosting
from .comp import CompResult, parse_comp
from .experience import ExperienceResult, parse_experience
from .locations import LocationMatcher, LocationResult
from .sections import SectionSplit, item_text, split_sections
from .timing import TimingResult, parse_timing


@dataclass
class Posting:
    posting_id: str
    company: str
    title: str
    team: str | None
    department: str | None
    locations: list[str]
    location: LocationResult
    work_arrangement: str
    employment_type: str
    employment_type_reason: str
    comp: CompResult
    description_lines: list[str]
    sections: SectionSplit
    required_text: str
    preferred_text: str
    experience: ExperienceResult
    timing: TimingResult
    url: str
    posted_at: str | None
    company_info: Company
    first_seen: str | None = None
    last_seen: str | None = None
    closed_at: str | None = None
    raw: RawPosting | None = field(default=None, repr=False)

    @property
    def description_text(self) -> str:
        return "\n".join(self.description_lines)

    # Step 3 schema field names
    @property
    def comp_min(self): return self.comp.min
    @property
    def comp_max(self): return self.comp.max
    @property
    def comp_currency(self): return self.comp.currency
    @property
    def comp_period(self): return self.comp.period
    @property
    def comp_midpoint_annual(self): return self.comp.midpoint_annual
    @property
    def extra_comp(self): return self.comp.extra_terms

    @property
    def is_us(self) -> bool:
        return self.location.market == "US"

    @property
    def is_canada(self) -> bool:
        return self.location.market == "CA"


# --- employment type ------------------------------------------------------
_RAW_TYPES = [
    (re.compile(r"full[\s_-]?time|permanent|regular", re.I), "full_time"),
    (re.compile(r"part[\s_-]?time", re.I), "part_time"),
    (re.compile(r"intern", re.I), "internship"),
    (re.compile(r"contract[\s_-]?to[\s_-]?hire", re.I), "contract_to_hire"),
    (re.compile(r"contract", re.I), "contract"),
    (re.compile(r"temp|fixed[\s_-]?term|seasonal", re.I), "temp"),
    (re.compile(r"co[\s_-]?op", re.I), "co_op"),
    (re.compile(r"apprentic", re.I), "apprenticeship"),
]
_TITLE_TYPES = [
    (re.compile(r"\binternships?\b|\binterns?\b|\bsummer (?:analyst|associate|analysts|associates)\b", re.I), "internship"),
    (re.compile(r"\bpart[\s_-]?time\b", re.I), "part_time"),
    (re.compile(r"\bco[- ]?op\b", re.I), "co_op"),
    (re.compile(r"\bapprentice(?:ship)?s?\b", re.I), "apprenticeship"),
    (re.compile(r"\bcontract[- ]to[- ]hire\b", re.I), "contract_to_hire"),
    (re.compile(r"\bcontractor\b|\(contract\)|\bcontract (?:role|position)\b|[-–,]\s*contract\s*$"
                r"|^\s*contract\s+(?!operations|management|manager|administrator|specialist|analyst|negotiat|attorney|counsel)",
                re.I), "contract"),
    (re.compile(r"\btemporary\b|\(temp\)|\btemp\b|\bfixed[- ]term\b|\bseasonal\b", re.I), "temp"),
]
_DESC_TYPES = re.compile(
    r"\bthis is an? (?:\d+[- ](?:month|week)[- ])?(contract[- ]to[- ]hire|contract|temporary|temp|fixed[- ]term)\b"
    r"(?: role| position| assignment| opportunity)?", re.I)
_DURATION = re.compile(r"\b(\d{1,2})[- ]?(weeks?|months?)\b(?:[- ]long)?", re.I)
_PROGRAM_WORD = re.compile(r"\b(?:fellowship|program|programme|cohort|runs|lasts|duration)\b", re.I)


def employment_type_of(raw: RawPosting, lines: list[str]) -> tuple[str, str]:
    raw_val = (raw.employment_type or "").strip()
    base = "unknown"
    for rx, kind in _RAW_TYPES:
        if raw_val and rx.search(raw_val):
            base = kind
            break
    else:
        if raw_val:
            base = "other"
    reason = f"board field: {raw_val}" if raw_val else "not stated"
    for rx, kind in _TITLE_TYPES:
        if rx.search(raw.title):
            return kind, f"title: {raw.title}"
    blob = "\n".join(lines)
    m = _DESC_TYPES.search(blob)
    if m:
        word = m.group(1).lower().replace(" ", "_").replace("-", "_")
        kind = {"temporary": "temp", "fixed_term": "temp"}.get(word, word)
        return kind, f"description: {m.group(0)}"
    if re.search(r"\bfellow(?:ship)?s?\b", raw.title, re.I) or base == "other":
        for line in lines:
            if not _PROGRAM_WORD.search(line):
                continue
            for d in _DURATION.finditer(line):
                n = int(d.group(1))
                months = n / 4.33 if d.group(2).lower().startswith("week") else n
                if 0 < months < 6:
                    return "fellowship_under_6_months", f"{n} {d.group(2)}: {line.strip()[:120]}"
    return base, reason


def normalize_posting(raw: RawPosting, company: Company, criteria: Criteria,
                      matcher: LocationMatcher) -> Posting:
    lines = raw.description_lines
    sections = split_sections(lines, criteria)
    req = [item_text(ln) for ln in sections.lines("required")]
    pref = [item_text(ln) for ln in sections.lines("preferred")]
    loc = matcher.match(raw.locations, raw.country, raw.workplace_type, "\n".join(lines))
    etype, ereason = employment_type_of(raw, lines)
    best = loc.best
    comp = parse_comp(([f"Pay: {raw.title}"] if "$" in raw.title else []) + lines, raw.comp_ranges, raw.comp_extra, criteria,
                      market=loc.market, region=best.region if best else None,
                      employment_type=etype, matcher=matcher)
    exp = parse_experience(sections.kind_of_line(), raw.title, criteria)
    timing = parse_timing(lines)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return Posting(
        posting_id=f"{raw.platform}:{raw.slug}:{raw.native_id}",
        company=company.name, title=raw.title,
        team=raw.team, department=raw.department, locations=raw.locations,
        location=loc, work_arrangement=loc.arrangement,
        employment_type=etype, employment_type_reason=ereason,
        comp=comp, description_lines=lines, sections=sections,
        required_text="\n".join(req), preferred_text="\n".join(pref),
        experience=exp, timing=timing, url=raw.url, posted_at=raw.posted_at,
        company_info=company, first_seen=now, last_seen=now, raw=raw,
    )
