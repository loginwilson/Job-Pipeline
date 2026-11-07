"""Field column (criteria.yaml fields) — information only, never drops anything."""
from __future__ import annotations

from ..config import Criteria
from ..textmatch import find_terms
from .normalize import Posting

DESCRIPTION_LINES = 40     # the role/company intro, before long boilerplate


def field_of(p: Posting, criteria: Criteria) -> str:
    fields = criteria.get("fields", default={}) or {}
    best, best_score = "Other", 0
    intro = p.description_lines[:DESCRIPTION_LINES]
    for name, terms in fields.items():
        score = 3 * len(find_terms(p.title, terms))
        score += len({h.term for h in find_terms(intro, terms)})
        score += 2 * len(find_terms(" ".join(p.company_info.sectors), terms))
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 1 else "Other"
