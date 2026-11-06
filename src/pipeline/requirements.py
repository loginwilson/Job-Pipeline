"""Requirements match (criteria.yaml requirements_match) — v1 keyword matcher.

Each required / preferred line is classified against candidate_profile.skills
and requirement_synonyms:
  met      matches skills.have or a requirement_synonyms.met phrase
  stretch  matches skills.stretch or a requirement_synonyms.stretch phrase
  gap      matches skills.gap, a hard_gap_drop phrase, or asks 2+ years
  unclear  nothing matched -> half-credit stretch, never a gap

Phrases are turned into "probes" (sets of word stems) by splitting on
" / ", commas, semicolons, "or" and parenthesised lists; a probe matches a
line when all its stems appear in the line. When a line offers alternatives
("A, B or C", "e.g.") the best class wins; otherwise the worst does. Years,
graduation date and degree level are hard constraints applied on top.
The AI reviewer refines this in Phase 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Criteria
from ..textmatch import STOPWORDS, content_stems, find_terms, normalize, stem, words
from .experience import _YEARS_RE, _num
from .timing import _GRAD_RE, _windows, grad_ok

RANK = {"gap": 0, "unclear": 1, "stretch": 2, "met": 3}
_ALTERNATIVES = re.compile(r"\bor\b|\be\.?g\.?\b|such as|\band/or\b|/|\bany of\b|\bone of\b", re.I)
_DEGREE = re.compile(r"\b(?:degree|bachelor'?s?|undergraduate|b\.?[as]\.?|ba/bs|bs/ba|mba|master'?s?|"
                     r"jd|juris doctor|ph\.?d|doctorate)\b", re.I)
_GRAD_DEGREE = re.compile(r"\b(?:master'?s?|m\.?b\.?a|mba|jd|j\.d\.?|juris doctor|ph\.?d|doctorate|law degree)\b", re.I)
_UNDERGRAD = re.compile(r"\b(?:bachelor'?s?|undergraduate|b\.?[as]\.?|ba/bs|bs/ba)\b", re.I)
_SOFT = re.compile(r"\bor equivalent\b|\bequivalent (?:practical )?experience\b|\bpreferred\b|\bpreference\b", re.I)
_PREPS = {"for", "with", "of", "in", "by", "from", "to", "on"}


@dataclass
class Probe:
    stems: tuple[str, ...]
    label: str
    source: str            # where it came from (profile entry / synonym key)
    cls: str               # met / stretch / gap
    acronym: str | None = None


@dataclass
class ItemMatch:
    section: str           # required / preferred
    text: str
    cls: str               # met / stretch / gap / unclear
    label: str = ""
    evidence: list[str] = field(default_factory=list)
    hard: bool = False


@dataclass
class RequirementsResult:
    items: list[ItemMatch]
    score: float | None
    summary: str
    hard_gap: ItemMatch | None = None

    @property
    def has_required_gap(self) -> bool:
        return any(i.cls == "gap" and i.section == "required" for i in self.items)


# --- probe extraction -------------------------------------------------------
def _clean_piece(piece: str) -> str:
    piece = re.sub(r"^\s*[\w ]{2,20}:\s*", "", piece) if ":" in piece[:22] else piece
    return piece.strip(" .;:-—")


def _split_or(piece: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\s+or\s+", piece) if p.strip()]
    out = []
    for i, part in enumerate(parts):
        pw = part.split()
        if len(pw) == 1 and i + 1 < len(parts):
            nxt = parts[i + 1].split()
            if pw[0].lower().endswith("ing") and nxt and nxt[0].lower().endswith("ing") and len(nxt) > 1:
                part = " ".join(pw + nxt[1:])
        out.append(part)
    return out


def _split_and(piece: str) -> list[str]:
    parts = re.split(r"\s+(?:and|&)\s+", piece)
    if len(parts) == 2 and all(len(content_stems(p)) >= 2 for p in parts):
        return parts
    return [piece]


def extract_probes(entry: str, cls: str, source: str | None = None) -> list[Probe]:
    source = source or entry
    pieces: list[str] = []
    main = entry
    for paren in re.findall(r"\(([^)]*)\)", entry):
        main = main.replace(f"({paren})", " ")
        if "," in paren or ";" in paren:
            inner = _clean_piece(paren)
            for p in re.split(r"[,;]", inner):
                pieces.extend(_split_and(p.strip()) if len(p.split()) > 3 else re.split(r"\s+(?:and|&)\s+", p.strip()))
    for p in re.split(r"\s+/\s+|/|,|;|\s+—\s+|\s+-\s+", main):
        p = _clean_piece(p)
        if not p:
            continue
        for q in _split_or(p):
            pieces.extend(_split_and(q))
    probes: list[Probe] = []
    seen = set()

    def add(text: str):
        text = _clean_piece(text)
        ws = words(text)
        content = [w for w in ws if w.lower() not in STOPWORDS]
        if not content:
            return
        if len(content) == 1:
            w = content[0]
            if len(w) < 4 and not (w.isupper() and len(w) >= 2):
                return
        st = tuple(stem(w) for w in content)
        if st in seen:
            return
        seen.add(st)
        # single proper nouns / acronyms (SQL, Figma, Argus) match as exact words
        acr = content[0] if len(content) == 1 and content[0][:1].isupper() else None
        probes.append(Probe(st, " ".join(content), source, cls, acr))

    for piece in pieces:
        add(piece)
        ws = piece.split()
        for i, w in enumerate(ws[1:], start=1):
            if w.lower() in _PREPS:
                head = " ".join(ws[:i])
                if len(content_stems(head)) >= 2:
                    add(head)
                break
    return probes


class RequirementsMatcher:
    def __init__(self, criteria: Criteria):
        self.c = criteria
        skills = criteria.get("candidate_profile", "skills", default={}) or {}
        syn = criteria.get("requirements_match", "requirement_synonyms", default={}) or {}
        self.probes: list[Probe] = []
        self.met_degree_fields: list[str] = []
        self.gap_degree_fields: list[str] = []
        self.hard_degree_fields: list[str] = []
        self.hard_probes: list[Probe] = []
        self.hard_years_probes: list[tuple[tuple[str, ...], str]] = []

        for cls in ("met", "stretch"):
            for key, value in (syn.get(cls) or {}).items():
                if _DEGREE.search(key):
                    if cls == "met":
                        self.met_degree_fields += self._degree_fields(key)
                    continue
                self.probes += extract_probes(key, cls, f"synonym: {key} → {value}")
        for cls, bucket in (("met", "have"), ("stretch", "stretch"), ("gap", "gap")):
            for entry in skills.get(bucket) or []:
                entry = _flatten(entry)
                if _DEGREE.search(entry):
                    if cls == "gap":
                        self.gap_degree_fields += self._degree_fields(entry)
                    continue
                if re.search(r"\byears?\b", entry):
                    continue  # handled by the years rule
                self.probes += extract_probes(entry, cls, f"profile.{bucket}: {entry}")
        for phrase in criteria.get("requirements_match", "hard_gap_drop", default=[]) or []:
            phrase = str(phrase)
            if _DEGREE.search(phrase):
                self.hard_degree_fields += self._degree_fields(phrase)
            elif re.match(r"\s*years? of\b", phrase, re.I):
                self.hard_years_probes.append((tuple(content_stems(phrase)), phrase))
            else:
                for pr in extract_probes(phrase, "gap", f"hard_gap_drop: {phrase}"):
                    if len(pr.stems) >= 2:
                        self.hard_probes.append(pr)
        self.grad = criteria.graduation
        self.gpa = criteria.gpa
        self.friendly = criteria.get("experience", "friendly_wording_bonus", "terms", default=[]) or []

    @staticmethod
    def _degree_fields(text: str) -> list[str]:
        t = re.sub(r"\b(?:bachelor'?s?|degree|required|in)\b", " ", text, flags=re.I)
        fields = []
        for part in re.split(r"[/,]|\bor\b|\band\b", t):
            part = part.strip(" .")
            if part and part.lower() not in ("related", "a related field", "equivalent"):
                fields.append(part)
        return fields

    # --- per-item ---------------------------------------------------------
    def _probe_hits(self, text: str) -> list[Probe]:
        item_stems = set(stem(w) for w in words(text))
        item_words = set(words(text))
        hits = []
        for pr in self.probes:
            if pr.acronym:
                if pr.acronym.isupper():
                    ok = pr.acronym in item_words or pr.acronym + "s" in item_words
                else:
                    lw = {w.lower() for w in item_words}
                    ok = pr.acronym.lower() in lw
                if ok:
                    hits.append(pr)
            elif all(s in item_stems for s in pr.stems):
                hits.append(pr)
        return hits

    def _has_field(self, text: str, fields: list[str]) -> str | None:
        for f in fields:
            if f.isupper():
                if f in words(text):
                    return f
            elif find_terms(text, [f], first_only=True):
                return f
        return None

    def classify(self, text: str, section: str) -> ItemMatch:
        item = ItemMatch(section=section, text=text, cls="unclear")
        constraints: list[tuple[str, str, str]] = []   # (cls, label, evidence)
        skill: list[tuple[str, str, str]] = []

        # hard gaps (required items only)
        soft = bool(_SOFT.search(text)) or bool(find_terms(text, self.friendly, first_only=True))
        is_degree = bool(_DEGREE.search(text))
        met_field = self._has_field(text, self.met_degree_fields) if is_degree else None
        if is_degree:
            hard_field = self._has_field(text, self.hard_degree_fields)
            gap_field = hard_field or self._has_field(text, self.gap_degree_fields)
            if _GRAD_DEGREE.search(text) and not _UNDERGRAD.search(text):
                constraints.append(("gap", "graduate degree", "asks for a graduate degree"))
            elif met_field:
                skill.append(("met", "degree", f"degree field '{met_field}' (graduating "
                              f"{self.grad[0]}-{self.grad[1]:02d})"))
            elif gap_field:
                cls = "gap"
                skill.append((cls, f"{gap_field} degree", f"degree field '{gap_field}'"))
                if hard_field and section == "required" and not soft:
                    item.hard = True
            elif not re.search(r"\b(?:in|of)\s+[A-Z]", text) and not re.search(
                    r"\b(?!Bachelor|Undergraduate|BA\b|BS\b)[A-Z][A-Za-z]*\s+degree", text):
                skill.append(("met", "degree", "degree, no field specified"))
        for pr in self.hard_probes:
            item_stems = set(stem(w) for w in words(text))
            if all(s in item_stems for s in pr.stems):
                skill.append(("gap", pr.label, pr.source))
                if section == "required":
                    item.hard = True

        # years of experience
        for m in _YEARS_RE.finditer(text):
            lo = _num(m.group(1))
            if lo is None or m.group(1).lower() in ("a", "an"):
                continue
            yrs = lo / 12 if m.group(3).lower().startswith("month") else lo
            label = f"{yrs:g}+ yrs"
            for st, phrase in self.hard_years_probes:
                if all(s in set(stem(w) for w in words(text)) for s in st if s not in ("year",)):
                    item.hard = item.hard or section == "required"
                    constraints.append(("gap", label + " software engineering", f"hard_gap_drop: {phrase}"))
            if yrs >= 2:
                constraints.append(("gap", label, "profile.gap: multiple years of full-time experience"))
            elif yrs >= 1:
                constraints.append(("stretch", label, "1 year asked; two CRE summers + projects"))
            break

        # graduation window
        if _GRAD_RE.search(text):
            wins = _windows(text)
            ok = grad_ok(wins, self.grad)
            if ok is True:
                constraints.append(("met", "graduation date", f"graduating {self.grad[0]}-{self.grad[1]:02d}"))
            elif ok is False:
                constraints.append(("gap", "graduation date", f"graduating {self.grad[0]}-{self.grad[1]:02d} "
                                    "is outside the window"))

        # GPA
        g = re.search(r"\bGPA\b[^0-9]{0,20}(\d\.\d{1,2})|(\d\.\d{1,2})\s*\+?\s*(?:/\s*4\.0\s*)?GPA", text)
        if g and self.gpa is not None:
            need = float(g.group(1) or g.group(2))
            constraints.append(("met" if self.gpa >= need else "gap", f"GPA {need:g}", f"GPA {self.gpa:g}"))

        for pr in self._probe_hits(text):
            skill.append((pr.cls, pr.label, pr.source))

        if not skill and not constraints:
            return item
        if skill:
            pick = max if _ALTERNATIVES.search(text) else min
            chosen = pick(skill, key=lambda s: RANK[s[0]])
            if any(s[1] == "degree" and s[0] == "met" for s in skill):
                chosen = next(s for s in skill if s[1] == "degree" and s[0] == "met")
                item.hard = False
        candidates = ([chosen] if skill else []) + constraints
        final = min(candidates, key=lambda s: RANK[s[0]])
        item.cls, item.label = final[0], final[1]
        item.evidence = [f"{c}: {lbl} ← {ev}" for c, lbl, ev in skill + constraints]
        if item.cls != "gap":
            item.hard = False
        return item

    def match(self, required: list[str], preferred: list[str]) -> RequirementsResult:
        items = [self.classify(t, "required") for t in required if _is_requirement(t)]
        items += [self.classify(t, "preferred") for t in preferred if _is_requirement(t)]
        sc = self.c.get("requirements_match", "scoring", default={}) or {}
        pts = {("required", "met"): sc.get("required_met", 0), ("required", "stretch"): sc.get("required_stretch", 0),
               ("required", "gap"): sc.get("required_gap", 0), ("preferred", "met"): sc.get("preferred_met", 0),
               ("preferred", "stretch"): sc.get("preferred_stretch", 0), ("preferred", "gap"): 0}
        got = maxp = 0.0
        for it in items:
            best = pts[(it.section, "met")]
            maxp += best
            if it.cls == "unclear":
                got += pts[(it.section, "stretch")] / 2
            else:
                got += pts[(it.section, it.cls)]
        score = round(100 * got / maxp, 1) if maxp else None
        hard = next((i for i in items if i.hard), None)
        return RequirementsResult(items, score, summarize(items), hard)


def _flatten(entry) -> str:
    """YAML reads 'x (coursework: a, b)' as {'x (coursework': 'a, b)'}; undo that."""
    if isinstance(entry, dict):
        return "; ".join(f"{k}: {v}" for k, v in entry.items())
    return str(entry)


def _is_requirement(text: str) -> bool:
    return len(text.split()) >= 2 and not text.endswith("?")


def summarize(items: list[ItemMatch]) -> str:
    if not items:
        return "No requirements section found"
    met = sum(1 for i in items if i.cls == "met")
    stretch = sorted({i.label for i in items if i.cls == "stretch"})
    gap = sorted({i.label for i in items if i.cls == "gap"})
    unclear = sum(1 for i in items if i.cls == "unclear")
    s = f"Met {met}/{len(items)} · Stretch: {', '.join(stretch) or 'none'} · Gap: {', '.join(gap) or 'none'}"
    if unclear:
        s += f" · Unclear: {unclear}"
    return s
