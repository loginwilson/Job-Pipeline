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


_NOT_ABOUT_TOOLS = re.compile(
    r"\b\w+ly (?:strong|solid|advanced)\b|\b(?:strong|solid|deep|advanced|excellent) (?:analytical|analytic|"
    r"communication|communicator|written|verbal|problem[- ]solving|business|commercial|interpersonal|"
    r"organizational|quantitative reasoning|critical thinking|judgment|attention)\b", re.I)


_BUSINESS_AS_WORK = re.compile(
    r"\bbusiness (?:operations|ops|development|teams?|needs|process(?:es)?|systems|outcomes|problems|units?|goals|"
    r"partners?|stakeholders|leaders|impact|requirements|cases?|reviews?)\b", re.I)


def required_lines(p: Posting, criteria: Criteria) -> list[str]:
    pref_terms = criteria.get("credentials", "softeners", default=[]) or []
    out = [item_text(ln) for ln in p.sections.lines("required")]
    if not out:
        # no recognised requirements heading: read bullets everywhere except duties / perks / about
        skip = criteria.get("experience", "parse_sections", "not_requirements", default=[]) or []
        for sec in p.sections.sections:
            if sec.kind == "preferred" or (sec.heading and find_terms(sec.heading, skip, first_only=True)):
                continue
            if sec.kind == "intro" and not sec.heading:
                # the untitled opening: only lines that state a degree or years
                out += [item_text(ln) for ln in sec.lines if _DEGREE.search(ln) or re.search(r"\byears?\b", ln, re.I)]
            else:
                out += [item_text(ln) for ln in sec.lines]
    for kind, ln in p.sections.kind_of_line():
        if kind != "required" and _REQUIRED_CUE.search(ln) and not find_terms(ln, pref_terms, first_only=True):
            out.append(item_text(ln))
    return [ln for ln in dict.fromkeys(out) if ln]


_FLUENCY = re.compile(r"\b(?:fluen\w*|proficien\w*|native|bilingual|business[- ]level|professional[- ]level|written and spoken|spoken and written|speak\w*)\b", re.I)
_ADVANCED_PREFERRED = re.compile(
    r"\(?[^,.;()]*\b(?:advanced|graduate|master'?s|masters|mba|ph\.?d)\b[^,.;()]*\b(?:preferred|a plus|desired|nice to have)\b[^,.;()]*\)?",
    re.I)


def _case_sensitive(line: str, terms: list[str]) -> str | None:
    for t in terms:
        if re.search(rf"(?<![A-Za-z0-9+#]){re.escape(t)}(?![A-Za-z0-9+#])", line):
            return t
    return None


def _negated(line: str, term: str) -> bool:
    """'You will not be expected to write production code' / 'no coding required' (FSH, Nov 9)."""
    return bool(re.search(r"\b(?:not|no|never|don't|doesn't|won't|without|isn't|aren't|nor)\b[^.;]{0,45}"
                          + re.escape(term), line, re.I))


def is_ai_builder(title: str, criteria: Criteria) -> bool:
    """Automation / GTM-engineering / forward-deployed titles you qualify for (see criteria).
    An 'engineer' title counts only as a GTM or automation engineer: 'Forward Deployed
    (Software / AI) Engineer' is a software job."""
    t = title or ""
    if not find_terms(t, criteria.get("technical", "ai_builder_titles", default=[]) or [], first_only=True):
        return False
    if re.search(r"\bengineer", t, re.I) and not re.search(r"\b(?:gtm|go[- ]to[- ]market|automation)\s+engineer", t, re.I):
        return False
    return not re.search(r"\b(?:software|full[- ]?stack|back[- ]?end|front[- ]?end|machine learning|ML|data engineer|"
                         r"developer|scientist|research)\b", t, re.I)


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
    mine = {str(x).lower() for x in criteria.get("candidate", "languages", default=["English"]) or []}
    others = [x for x in criteria.get("other_languages", default=[]) or [] if str(x).lower() not in mine]
    # Nov 9: AI-builder titles get NO exemption any more - a required programming language or
    # "write code" drops them like any other job (GTM / automation engineers asking for Python)
    ok_phrases: set[str] = set()
    for line in required_lines(p, criteria):
        lang_hit = find_terms(line, others, first_only=True)
        if lang_hit and _FLUENCY.search(line) and not find_terms(_ADVANCED_PREFERRED.sub(" ", line), soft, first_only=True):
            hits.append(Hit("credentials", line, f"requires {lang_hit[0].term}"))
            continue
        # "(Advanced degree preferred)" softens the advanced degree, not the bachelor's ask
        core = _ADVANCED_PREFERRED.sub(" ", line)
        softened = bool(find_terms(core, soft, first_only=True))
        # 7. technical
        lang = _case_sensitive(line, req.get("languages", []) or [])
        phrase = [h for h in find_terms(line, req.get("phrases", []) or [])
                  if h.term.lower() not in ok_phrases and not _negated(line, h.term)][:1]
        if lang or phrase:
            # Nov 9: you can do technical work with AI tools, but not when they expect proficiency
            hard = [h for h in find_terms(line, tech.get("always_drop_phrases") or []) if not _negated(line, h.term)][:1]
            if hard:
                hits.append(Hit("technical", line, f"expects you to {hard[0].term}"))
                continue
            # "analytically strong (comfortable with SQL or Python)" (Jerry): 'strong' describes the
            # person's analysis or communication, not the language - only the familiarity word is about SQL
            prof_line = _NOT_ABOUT_TOOLS.sub(" ", line)
            proficient = find_terms(prof_line, tech.get("proficiency_words") or [], first_only=True)
            familiar = find_terms(line, tech.get("familiarity_words") or [], first_only=True)
            # Nov 9: a degree softener ("or equivalent", "or related") never softens a coding ask -
            # "CS or equivalent. You write real scripts in Python" is still writing code
            tech_soft = find_terms(core, [t for t in soft if not re.search(r"equivalent|related|similar", t, re.I)],
                                   first_only=True)
            if proficient and not tech_soft:         # "fluent in Python ... comfortable with X": fluent wins
                hits.append(Hit("technical", line, f"expects {proficient[0].term} {lang or phrase[0].term}"))
                continue
            if not softened and not familiar and not find_terms(line, ok_alt, first_only=True):
                hits.append(Hit("technical", line, f"requires {lang or phrase[0].term}"))
                continue
        # hard skills you don't have (e.g. financial modeling): softening wording keeps the line,
        # but a tool on the same line (Excel) doesn't - modeling in Excel is still modeling
        # Nov 9 audit: "equally comfortable building a financial model, drafting a customer email..."
        # describes a generalist; familiarity wording keeps the line here too
        if not softened and not find_terms(line, tech.get("familiarity_words") or [], first_only=True):
            miss = find_terms(line, criteria.get("missing_skills", default=[]) or [], first_only=True)
            if miss:
                hits.append(Hit("technical", line, f"requires {miss[0].term}"))
                continue
        # 8. credentials
        if softened:
            continue
        # a degree "in a quantitative field" / mostly math-stats-CS-physics: finance or economics
        # on the list doesn't make it yours; only a business / hospitality / real-estate field does
        # "business operations" on the same line is the job, not a degree field (SnapMagic: "A STEM
        # degree and 1-3 years in operations, business operations, automation ...")
        if _DEGREE.search(line) and not find_terms(_BUSINESS_AS_WORK.sub(" ", line), strong, first_only=True):
            q = find_terms(line, quant, first_only=True)
            unrel = {h.term for h in find_terms(line, cred.get("unrelated_degree_fields", []) or [])}
            if q or len(unrel) >= 2:
                why = q[0].term if q else ", ".join(sorted(unrel)[:3])
                hits.append(Hit("credentials", line, f"requires a {why} degree" if q else f"requires a {why} degree"))
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
