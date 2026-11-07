"""Text normalization and term matching shared by every rule module.

Matching rules (applied the same way to every criteria term list):
  * case-insensitive, except all-caps acronyms (OPT, CPT, ITAR, OTE, RSU),
    which match case-sensitively so "opt out" is not OPT;
  * whole words only ("vp" does not match "AVP", "lead" not "leadership");
  * curly quotes/dashes unified, hyphens read as spaces, apostrophes dropped,
    dotted abbreviations collapsed ("U.S." == "US");
  * the last word may be singular or plural ("U.S. persons" matches
    "U.S. Person", "export control" matches "export controls").
Terms are matched line by line so a reported match carries its line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-", "−": "-", " ": " ",
                         "•": " ", "·": " "})
_ABBREV = re.compile(r"\b(?:[A-Za-z]\.){2,}")


@lru_cache(maxsize=300_000)
def normalize(text: str, lower: bool = True) -> str:
    """Normalize text for matching. Keeps commas/periods (some terms use them)."""
    if not text:
        return ""
    t = text.translate(_QUOTES)
    t = _ABBREV.sub(lambda m: m.group(0).replace(".", "") + " ", t)
    t = t.replace("'", "")
    t = re.sub(r"(?<=\w)-(?=\w)|\s-\s|-", " ", t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t).strip()
    return t.lower() if lower else t


def _is_acronym(term: str) -> bool:
    letters = [c for c in term if c.isalpha()]
    return len(letters) >= 2 and not any(c.islower() for c in term)


def _plural_alternatives(word: str) -> str:
    w = re.escape(word)
    if word.endswith("ies") and len(word) > 4:
        return f"(?:{w}|{re.escape(word[:-3])}y)"
    if word.endswith("ses") or word.endswith("xes") or word.endswith("ches") or word.endswith("shes"):
        return f"(?:{w}|{re.escape(word[:-2])})"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return f"(?:{w}|{re.escape(word[:-1])})"
    if word.endswith("y") and len(word) > 3 and word[-2] not in "aeiou":
        return f"(?:{w}|{re.escape(word[:-1])}ies)"
    if word[-1:].isalpha():
        return f"(?:{w}(?:e?s)?)"
    return w


@lru_cache(maxsize=4096)
def term_regex(term: str) -> re.Pattern:
    acronym = _is_acronym(term.strip())
    norm = normalize(term, lower=not acronym)
    words = norm.split(" ")
    if not words or not norm:
        raise ValueError(f"empty term {term!r}")
    parts = [re.escape(w) for w in words[:-1]]
    last = words[-1]
    parts.append(_plural_alternatives(last) if last[-1:].isalnum() else re.escape(last))
    body = r"\s+".join(parts)
    left = r"(?<![A-Za-z0-9])" if norm[0].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if norm[-1].isalnum() else ""
    flags = 0 if acronym else re.IGNORECASE
    return re.compile(left + body + right, flags)


@lru_cache(maxsize=4096)
def _any_term_regex(terms: tuple[str, ...]) -> re.Pattern | None:
    """One alternation of every term, used to skip lines that match nothing."""
    parts = [term_regex(t) for t in terms]
    if not parts:
        return None
    sensitive = [p.pattern for p in parts if not p.flags & re.IGNORECASE]
    insensitive = [p.pattern for p in parts if p.flags & re.IGNORECASE]
    alts = []
    if insensitive:
        alts.append("(?i:" + "|".join(f"(?:{x})" for x in insensitive) + ")")
    if sensitive:
        alts.append("|".join(f"(?:{x})" for x in sensitive))
    return re.compile("|".join(alts))


@dataclass
class TermMatch:
    term: str
    matched: str
    line: str


def find_terms(lines: Iterable[str] | str, terms: Iterable[str], first_only: bool = False,
               ignore_spans: dict[int, list[tuple[int, int]]] | None = None) -> list[TermMatch]:
    """Return every (term, line) match. `lines` may be one string or many lines."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    out: list[TermMatch] = []
    terms = [str(t) for t in terms if t and str(t).strip()]
    quick = _any_term_regex(tuple(terms))
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        norm_cased = normalize(line, lower=False)
        if quick is None or not quick.search(norm_cased):
            continue
        for term in terms:
            for m in term_regex(str(term)).finditer(norm_cased):
                if ignore_spans and _inside(m.span(), ignore_spans.get(idx, [])):
                    continue
                out.append(TermMatch(str(term), m.group(0), line.strip()))
                if first_only:
                    return out
                break
    return out


def term_spans(text: str, terms: Iterable[str]) -> list[tuple[int, int]]:
    norm_cased = normalize(text, lower=False)
    spans = []
    for term in terms:
        for m in term_regex(str(term)).finditer(norm_cased):
            spans.append(m.span())
    return spans


def _inside(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(s <= span[0] and span[1] <= e for s, e in spans)


def has_term(text: str, terms: Iterable[str]) -> TermMatch | None:
    hits = find_terms(text, terms, first_only=True)
    return hits[0] if hits else None


# --- word-level helpers for requirement / heading matching -----------------

STOPWORDS = frozenset("""
a an the and or of in on at to for with by from as is are be been being this that these those
your you our we us it its their them they will would should can could may might must
any some all other such e eg ie etc using use used via into onto over under per
required requirement requirements require requires preferred plus skill skills ability able
experience experienced strong strongly excellent good great solid proven demonstrated
knowledge familiarity familiar proficiency proficient understanding working work
specific level years year least minimum including include includes like well also
""".split())

_SUFFIXES = ("ments", "ment", "ings", "ing", "ies", "ied", "ed", "es", "s", "e", "ly")


def stem(word: str) -> str:
    w = word.lower()
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                if suf in ("ies", "ied"):
                    w += "y"
                changed = True
                break
    if len(w) > 4 and w.endswith("ll"):
        w = w[:-1]  # modelling -> model
    return w


WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.]*[A-Za-z0-9+#]|[A-Za-z0-9]")


def words(text: str) -> list[str]:
    return WORD_RE.findall(normalize(text, lower=False))


def stems(text: str) -> list[str]:
    return [stem(w) for w in words(text)]


def content_stems(text: str) -> list[str]:
    return [stem(w) for w in words(text) if w.lower() not in STOPWORDS]


def contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    if n == 0:
        return False
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))
