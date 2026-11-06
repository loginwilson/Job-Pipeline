"""TN category mapping for US roles (criteria.yaml work_authorization.tn_alignment).

Judged on DUTIES, not title: duties text = the posting minus its
required/preferred sections. Company sector tags come from watchlist.yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Criteria
from ..textmatch import find_terms, normalize
from .normalize import Posting

_DISPLAY = {"hotel_manager": "Hotel Manager", "management_consultant": "Mgmt Consultant"}


@dataclass
class TNResult:
    applies: bool
    category: str | None = None   # hotel_manager / management_consultant / None
    fit: str | None = None        # likely / possible / unlikely
    label: str = ""
    flags: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    points: float = 0.0


def _phrases(text: str) -> list[str]:
    """'a, b and c roles — note' -> ['a', 'b', 'c']"""
    head = re.split(r"\s+[—-]\s+|;", str(text))[0]
    parts = re.split(r",|\band\b|\bor\b", head)
    out = []
    for p in parts:
        p = re.sub(r"\broles?\b", "", p).strip()
        if p:
            out.append(p)
    return out


def _duties_lines(p: Posting) -> list[str]:
    return [ln for s in p.sections.sections if s.kind not in ("required", "preferred")
            for ln in ([s.heading] if s.heading else []) + s.lines]


def map_tn(p: Posting, criteria: Criteria) -> TNResult:
    tn = criteria.get("work_authorization", "tn_alignment", default={}) or {}
    if p.is_canada:
        return TNResult(applies=False, label="No visa needed", flags=["No visa needed"])
    if not p.is_us:
        return TNResult(applies=False)
    cats = tn.get("categories", {})
    scoring = tn.get("scoring", {})
    duties = _duties_lines(p)
    res = TNResult(applies=True)

    hm = cats.get("hotel_manager", {})
    employer_text = normalize(str(hm.get("employer_must_be", "")))
    hotel_employer = any(normalize(tag) and normalize(tag) in employer_text for tag in p.company_info.sectors)
    excluded = [x for x in _phrases(hm.get("excluded", "")) if find_terms(p.title, [x])]
    if hotel_employer and not excluded:
        unclear = find_terms([p.title] + duties, _phrases(hm.get("unclear", "")), first_only=True)
        duty_hits = find_terms(duties, hm.get("duties_signal", []))
        if duty_hits and not unclear:
            res.category, res.fit = "hotel_manager", "likely"
            res.evidence = sorted({h.term for h in duty_hits})
        elif duty_hits or unclear:
            res.category, res.fit = "hotel_manager", "possible"
            res.flags.append("TN Hotel Manager — verify")
            res.evidence = sorted({h.term for h in duty_hits}) + ([f"unclear: {unclear[0].term}"] if unclear else [])

    if res.category is None:
        mc = cats.get("management_consultant", {})
        hits = find_terms(duties, mc.get("duties_signal", []))
        if hits:
            res.category, res.fit = "management_consultant", "possible"
            res.evidence = sorted({h.term for h in hits})

    if res.category is None:
        res.fit = "unlikely"
        res.flags.append(scoring.get("flag_if_unlikely", "TN unlikely"))
        res.label = "TN: unlikely"
    else:
        res.label = f"TN: {_DISPLAY.get(res.category, res.category)} ({res.fit})"
    res.points = criteria.weight(scoring.get(res.fit, 0))

    rule = str((tn.get("timing_rule") or {}).get("us_role_starting_before_graduation", ""))
    start = p.timing.earliest_start()
    if start and start.start < criteria.graduation:
        m = re.search(r'flag\s+"([^"]+)"', rule)
        res.flags.append(m.group(1) if m else rule)
    return res
