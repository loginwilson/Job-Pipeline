"""Rules 7 and 8 — technical asks and credentials, checked on REQUIRED lines only.

A line counts as required when it sits in a requirements section
(experience.parse_sections.required) or itself says "required" / "must".
A line never drops the posting when it also offers a way Login qualifies:
a non-coding alternative (technical.ok_alternatives), his degree field, a
bachelor's degree, or softening wording (credentials.softeners).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Criteria
from ..textmatch import find_terms
from .normalize import Posting
from .sections import item_text

_REQUIRED_CUE = re.compile(r"\b(?:required|requirement|must)\b", re.I)
_DEGREE = re.compile(r"\b(?:degree|bachelor'?s?|b\.?s\.?|b\.?a\.?|undergraduate|major(?:ing)? in)\b", re.I)
_BACHELOR = re.compile(r"\b(?:bachelor'?s?|undergraduate|b\.?s\.?|b\.?a\.?|four[- ]year degree)\b", re.I)


@dataclass
class Hit:
    rule: str          # technical / credentials
    line: str
    reason: str


def required_lines(p: Posting, criteria: Criteria) -> list[str]:
    pref_terms = criteria.get("credentials", "softeners", default=[]) or []
    out = [item_text(ln) for ln in p.sections.lines("required")]
    for kind, ln in p.sections.kind_of_line():
        if kind != "required" and _REQUIRED_CUE.search(ln) and not find_terms(ln, pref_terms, first_only=True):
            out.append(item_text(ln))
    return [ln for ln in dict.fromkeys(out) if ln]


_ADVANCED_PREFERRED = re.compile(
    r"\(?[^,.;()]*\b(?:advanced|graduate|master'?s|masters|mba|ph\.?d)\b[^,.;()]*\b(?:preferred|a plus|desired|nice to have)\b[^,.;()]*\)?",
    re.I)


def _case_sensitive(line: str, terms: list[str]) -> str | None:
    for t in terms:
        if re.search(rf"(?<![A-Za-z0-9+#]){re.escape(t)}(?![A-Za-z0-9+#])", line):
            return t
    return None


def check(p: Posting, criteria: Criteria) -> list[Hit]:
    tech = criteria.get("technical", default={}) or {}
    req = tech.get("required_drop", {}) or {}
    ok_alt = tech.get("ok_alternatives", []) or []
    cred = criteria.get("credentials", default={}) or {}
    soft = cred.get("softeners", []) or []
    fields = criteria.get("candidate", "degree_fields", default=[]) or []
    hits: list[Hit] = []
    strong = cred.get("strong_degree_fields", []) or []
    quant = cred.get("quantitative_field_terms", []) or []
    for line in required_lines(p, criteria):
        # "(Advanced degree preferred)" softens the advanced degree, not the bachelor's ask
        core = _ADVANCED_PREFERRED.sub(" ", line)
        softened = bool(find_terms(core, soft, first_only=True))
        # 7. technical
        lang = _case_sensitive(line, req.get("languages", []) or [])
        phrase = find_terms(line, req.get("phrases", []) or [], first_only=True)
        if (lang or phrase) and not softened and not find_terms(line, ok_alt, first_only=True):
            hits.append(Hit("technical", line, f"requires {lang or phrase[0].term}"))
            continue
        # 8. credentials
        if softened:
            continue
        # a degree "in a quantitative field" / mostly math-stats-CS-physics: finance or economics
        # on the list doesn't make it yours; only a business / hospitality / real-estate field does
        if _DEGREE.search(line) and not find_terms(line, strong, first_only=True):
            q = find_terms(line, quant, first_only=True)
            unrel = {h.term for h in find_terms(line, cred.get("unrelated_degree_fields", []) or [])}
            if q or len(unrel) >= 2:
                why = q[0].term if q else ", ".join(sorted(unrel)[:3])
                hits.append(Hit("credentials", line, f"requires a degree in a {why} field" if q else f"requires a {why} degree"))
                continue
        has_field = bool(find_terms(line, fields, first_only=True))
        c = find_terms(line, cred.get("drop_required", []) or [], first_only=True)
        if c and not _BACHELOR.search(line) and not has_field:
            hits.append(Hit("credentials", line, f"requires {c[0].term}"))
            continue
        if _DEGREE.search(line) and not has_field:
            f = find_terms(line, cred.get("unrelated_degree_fields", []) or [], first_only=True)
            if f:
                hits.append(Hit("credentials", line, f"requires a {f[0].term} degree"))
    return hits
