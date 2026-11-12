"""Years-of-experience asks (criteria.yaml experience).

Ranges use their lower bound ("2-4 years" -> 2). Asks in a preferred section
(or on a line using a preferred phrase) are preferred; everything else is
treated as required. Outside required/preferred sections a number of years
only counts when the line ties it to experience.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Criteria
from ..textmatch import find_terms

_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_N = r"(\d{1,2}(?:\.\d)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)"
_YEARS_RE = re.compile(
    rf"\b{_N}\s*\+?\s*(?:(?:-|–|to)\s*{_N}\s*\+?\s*)?(?:\(\d+\)\s*)?(?:\+\s*)?(years?|yrs?|months?|anos|años|ans)\b"
    r"(?:\s*\+)?(?=(?P<tail>[^.;]{0,90}))",
    re.IGNORECASE,
)
_NOT_EXPERIENCE_NEXT = re.compile(
    r"^\s*(?:-\s*)?(?:in a row|running(?=\s*(?:[.,;)!]|$))|straight(?=\s*(?:[.,;)!]|$))|consecutive|old|of age|or older|round|degree|college|university|bachelor|program|programme|commitment|contract|term|"
    r"plan|warranty|cliff|vesting|anniversary|history|track record of growth|ago|from now|later)\b",
    re.IGNORECASE,
)
_EXPERIENCE_WORD = re.compile(r"\bexperience|\bexperiência|\bexperiencia|\bem\b|\ben\b|\bbackground\b|\bworking (?:in|as|on|with)\b|\bin (?:a|an)\b", re.I)


@dataclass
class Ask:
    years: float
    kind: str          # required / preferred
    line: str


@dataclass
class ExperienceResult:
    asks: list[Ask] = field(default_factory=list)
    clean_terms: list[str] = field(default_factory=list)

    def max_years(self, kind: str) -> float | None:
        vals = [a.years for a in self.asks if a.kind == kind]
        return max(vals) if vals else None

    def label(self) -> str:
        req, pref = self.max_years("required"), self.max_years("preferred")
        if req is not None and req >= 1:
            return f"{_fmt(req)}+ yrs required" + (f" ({_fmt(pref)}+ preferred)" if pref and pref > req else "")
        if pref is not None and pref >= 1:
            return f"{_fmt(pref)}+ yrs preferred"
        if self.clean_terms:
            return self.clean_terms[0][:1].upper() + self.clean_terms[0][1:]
        if self.asks:
            return "0–1 yrs"
        return "No years listed"


def _fmt(v: float) -> str:
    return f"{v:g}"


def _num(token: str | None) -> float | None:
    if token is None:
        return None
    t = token.lower()
    if t in _WORDNUM:
        return float(_WORDNUM[t])
    try:
        return float(t)
    except ValueError:
        return None


def _alternatives(line: str) -> bool:
    spans = [m.span() for m in _YEARS_RE.finditer(line)]
    # the "or" must lead into the next year count ("...; or 3+ years", "...as an AE OR 4+ years"),
    # not sit in the middle ("2 years in consulting (MBB or boutique) and 1-2 years ...")
    return any(re.search(r"\bor\b[\s,;:(]*(?:\w+\s+){0,2}$", re.sub(r"\([^)]*\)", " ", line[a[1]:b[0]]), re.I)
               for a, b in zip(spans, spans[1:]))


def parse_experience(kind_lines: list[tuple[str, str]], title: str, criteria: Criteria) -> ExperienceResult:
    res = ExperienceResult()
    pref_terms = criteria.get("experience", "parse_sections", "preferred", default=[]) or []
    for kind, line in kind_lines:
        line_asks: list[Ask] = []
        for m in _YEARS_RE.finditer(line):
            lo = _num(m.group(1))
            if lo is None:
                continue
            unit = m.group(3).lower()
            tail = m.group("tail") or ""
            if _NOT_EXPERIENCE_NEXT.search(tail):
                continue
            if m.group(1).lower() in ("a", "an") and not re.match(r"\s*of\b", tail):
                continue
            in_req_section = kind in ("required", "preferred")
            # Nov 9: "2+ years in revenue operations" / "3-6 years in RCM" are asks too, even when the
            # posting's headings weren't recognised - written as a requirement (a +, a range,
            # "at least" / "minimum", or the line opening with the number) and tied to a field
            as_requirement = bool(re.search(r"\+|\d\s*(?:-|–|to)\s*\d", m.group(0))) or \
                bool(re.search(r"\b(?:at least|minimum|min\.|over)\s*$", line[:m.start()], re.I)) or \
                not line[:m.start()].strip(" -•*")
            field_tail = re.match(r"\s*(?:of\s+)?(?:in|at|with|working|as|across|on|doing|leading|building|"
                                  r"supporting|running|owning|managing|selling|of\s+\w+)\b", tail, re.I)
            if not in_req_section and not _EXPERIENCE_WORD.search(tail) and not (as_requirement and field_tail):
                continue
            before = line[max(0, m.start() - 60):m.start()]
            # Nov 9 audit: a timeline or the company's own history isn't an ask ("over the next 18
            # months", "our 30+ years", "compresses 10 years of learning")
            if re.search(r"\b(?:next|past|last|coming|within|compress\w*|spanning|for)\s+(?:the\s+)?$", before, re.I) or \
                    (not in_req_section and re.search(r"\b(?:we|we've|our|company|companies|firm|founded|history|decades?|celebrat\w*|in business)\b[^.;]{0,40}$|^(?!.*\b(?:candidate|you|applicant|person|hire|someone|individual)\b).*\bhas\s+(?:over\s+|more than\s+|nearly\s+)?$",
                                                      before, re.I)):
                continue
            # "a bachelor's degree, OR five years of equivalent experience": the degree is enough
            if re.search(r"\bor\b[^.;]{0,25}$", before, re.I) and re.search(r"\bequivalent\b", tail[:40], re.I) \
                    and re.search(r"\b(?:degree|bachelor)", line, re.I):
                continue
            # "zero to two years" / "from a recent graduate up to 2 years" allow 0 (Hyphenate, Instawork)
            if re.search(r"\b(?:zero|0|no|none)\s*(?:to|-|–)\s*$", before, re.I) or re.search(r"\bup to\s*$", before, re.I):
                lo = 0.0
            years = lo / 12 if unit.startswith("month") else lo
            if kind == "required":
                ask_kind = "required"     # a required section stays required even if the line says "a plus"
            elif kind == "preferred" or find_terms(line, pref_terms, first_only=True):
                ask_kind = "preferred"
            else:
                ask_kind = "required"
            line_asks.append(Ask(years, ask_kind, line.lstrip("- ").strip()))
        if len(line_asks) > 1 and re.search(r"\bfollowed by\b|\band then\b|\bplus (?:an additional|another)\b", line, re.I):
            # "2+ years in banking, ideally followed by 2+ years in BizOps" → 4 years in all
            line_asks = [Ask(sum(a.years for a in line_asks), line_asks[0].kind, line_asks[0].line)]
        elif len(line_asks) > 1 and _alternatives(line):
            # alternative paths on one line ("1+ years with a bachelor's; or 3+ years") → the easiest one.
            # Only an "or" BETWEEN the year counts makes them alternatives: "3-6 years in RCM, with at
            # least 1-2 years in a consulting or advisory capacity" is 3 years (Nov 9)
            line_asks = [min(line_asks, key=lambda a: a.years)]
        res.asks.extend(line_asks)
    clean = [t for t in criteria.get("experience", "pass_clean", default=[]) if not str(t).startswith("no_")]
    blob = [title] + [ln for _, ln in kind_lines]
    res.clean_terms = sorted({h.term for h in find_terms(blob, clean)}, key=clean.index)
    return res
