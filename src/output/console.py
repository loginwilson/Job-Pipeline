"""Console report (order-of-work step 1: print results before the Sheet exists)."""
from __future__ import annotations

from collections import Counter

from ..pipeline.gates import Evaluation


def _loc(ev: Evaluation) -> str:
    p = ev.posting
    b = p.location.best
    where = f"{b.city} (tier {b.tier})" if b else "; ".join(p.locations)
    return f"{where} · {p.work_arrangement}"


def report(evals: list[Evaluation], company: str, verbose_items: bool = True, ranked: dict | None = None) -> str:
    ranked = ranked or {}
    out: list[str] = []
    kept = [e for e in evals if e.passed]
    dropped = [e for e in evals if not e.passed]
    out.append(f"{company}: {len(evals)} postings · kept {len(kept)} · dropped {len(dropped)}")
    primary = Counter(e.failures[0].gate for e in dropped)
    anyg = Counter(f.gate for e in dropped for f in {x.gate: x for x in e.failures}.values())
    out.append("Dropped by primary gate: " + ", ".join(f"{g} {n}" for g, n in primary.most_common()))
    out.append("Gate failures (any):     " + ", ".join(f"{g} {n}" for g, n in anyg.most_common()))
    out.append("")
    out.append("=" * 100)
    out.append("KEPT")
    out.append("=" * 100)
    order = {"Strong": 0, "Worth a look": 1, "Stored": 2}

    def _k(e):
        t = ranked.get(e.posting.posting_id, (None, None))[1]
        return (order.get(t.tier, 3), -t.sort_score) if t else (3, -(e.requirements.score or 0))
    for e in sorted(kept, key=_k):
        p = e.posting
        r = e.requirements
        sig, t = ranked.get(p.posting_id, (None, None))
        head = f"[{t.tier} · fit {t.fit_score:g} · {sig.role_type}] " if t else ""
        out.append(f"\n▶ {head}{p.title}  —  {_loc(e)}")
        if t:
            out.append(f"  Why: {t.why}")
        out.append(f"  {p.url}")
        out.append(f"  Salary: {p.comp.display()} · Extra comp: {p.comp.extra_comp_display}"
                   f" · Experience: {p.experience.label()} · Type: {p.employment_type}")
        tn = e.tn.label if e.tn and e.tn.label else "—"
        if e.tn and e.tn.evidence:
            tn += f" [{', '.join(e.tn.evidence)}]"
        out.append(f"  Requirements: {r.summary} · {tn}")
        out.append(f"  Match score: {r.score if r.score is not None else 'n/a'} · Flags: {', '.join(e.flags) or '—'}")
        if p.sections.unmapped_headings and not p.preferred_text:
            pass
        if verbose_items:
            for it in r.items:
                tag = {"met": "MET    ", "stretch": "STRETCH", "gap": "GAP    ", "unclear": "UNCLEAR"}[it.cls]
                out.append(f"     [{tag}] ({it.section[:4]}) {it.text[:110]}")
                for evd in it.evidence[:3]:
                    out.append(f"               · {evd[:110]}")
    out.append("")
    out.append("=" * 100)
    out.append("DROPPED (primary gate — matched text; +other gates)")
    out.append("=" * 100)
    for e in sorted(dropped, key=lambda e: (e.failures[0].gate, e.posting.title)):
        p = e.posting
        f = e.failures[0]
        others = sorted({x.gate for x in e.failures[1:]} - {f.gate})
        loc = p.locations[0] if p.locations else "?"
        out.append(f"  {p.title[:60]:60} | {loc[:18]:18} | {f.gate}: {f.matched[:110]}"
                   + (f"  (+{', '.join(others)})" if others else ""))
    return "\n".join(out)
