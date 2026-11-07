"""Field column (criteria.yaml fields) — information only, never drops anything."""
from __future__ import annotations

from ..config import Criteria
from ..textmatch import find_terms
from .normalize import Posting

DESCRIPTION_LINES = 40     # the role/company intro, before long boilerplate


def field_of(p: Posting, criteria: Criteria) -> str:
    """The job title decides when it names a field (so company boilerplate can't relabel
    a trading role as travel); otherwise the description intro and company sectors."""
    fields = criteria.get("fields", default={}) or {}
    by_title = {name: len({h.term for h in find_terms(p.title, terms)}) for name, terms in fields.items()}
    if max(by_title.values(), default=0) > 0:
        return max(by_title, key=lambda n: (by_title[n], -list(fields).index(n)))
    intro = p.description_lines[:DESCRIPTION_LINES]
    best, best_score = "Other", 0
    for name, terms in fields.items():
        score = len({h.term for h in find_terms(intro, terms)})
        score += 2 * len(find_terms(" ".join(p.company_info.sectors), terms))
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 1 else "Other"
