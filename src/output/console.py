"""Console report: what passed the must-haves (with its facts) and what was dropped and why."""
from __future__ import annotations

from collections import Counter

from ..pipeline.gates import Evaluation


def _loc(ev: Evaluation) -> str:
    p = ev.posting
    b = p.location.best
    where = f"{b.city} (tier {b.tier})" if b else "; ".join(p.locations)
    return f"{where} · {p.work_arrangement}"


def report(evals: list[Evaluation], company: str, verbose: bool = True) -> str:
    out: list[str] = []
    kept = [e for e in evals if e.passed]
    dropped = [e for e in evals if not e.passed]
    out.append(f"{company}: {len(evals)} postings · passed {len(kept)} · dropped {len(dropped)}")
    primary = Counter(e.failures[0].gate for e in dropped)
    out.append("Dropped by first rule failed: " + ", ".join(f"{g} {n}" for g, n in primary.most_common()))
    out.append("")
    out.append("PASSED — for you to decide (Veto / Apply / All-in)")
    out.append("=" * 100)
    for e in sorted(kept, key=lambda e: (e.posting.location.best.tier if e.posting.location.best else 9,
                                         e.posting.title)):
        p = e.posting
        out.append(f"\n▶ {p.title}  —  {_loc(e)}")
        out.append(f"  {p.url}")
        out.append(f"  Field: {e.field} · Salary: {p.comp.display()} · Extra comp: "
                   f"{', '.join(p.comp.extra_terms) or '—'} · Experience: {p.experience.label()}")
        out.append(f"  Visa: {e.visa or '—'} · Flags: {', '.join(e.flags) or '—'}")
    if verbose:
        out.append("")
        out.append("DROPPED (first rule — matched text; +other rules)")
        out.append("=" * 100)
        for e in sorted(dropped, key=lambda e: (e.failures[0].gate, e.posting.title)):
            p = e.posting
            f = e.failures[0]
            others = sorted({x.gate for x in e.failures[1:]} - {f.gate})
            loc = p.locations[0] if p.locations else "?"
            out.append(f"  {p.title[:60]:60} | {loc[:18]:18} | {f.gate}: {f.matched[:110]}"
                       + (f"  (+{', '.join(others)})" if others else ""))
    return "\n".join(out)
