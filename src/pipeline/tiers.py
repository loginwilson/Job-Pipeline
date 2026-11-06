"""Final score and tiers (criteria.yaml final_score + tiers).

  fit score  = requirements_match x w_req + experience_fit x w_exp
               + necessity signals x w_sig                (0-100)
  tier       = from the fit score (Strong >= 70 with no required gaps,
               Worth a look >= 50, else Stored); anchors never below
               Worth a look.
  sort score = fit score + preference points (lane, sector, company,
               role_preference). Used only to order postings within a tier,
               so liking/disliking a role type never hides an option.

Signals become 0-100 as 50 + their points (clamped). A posting whose
requirements section could not be found scores a neutral 50 on that part
and is flagged, so it is not buried for a parsing miss.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Criteria
from .gates import Evaluation
from .signals import Signals

NEUTRAL = 50.0


@dataclass
class TierResult:
    fit_score: float
    sort_score: float
    tier: str
    why: str


def _threshold(spec: dict) -> float:
    m = re.search(r"([\d.]+)", str(spec.get("final_score", "0")))
    return float(m.group(1)) if m else 0.0


def assign_tier(ev: Evaluation, sig: Signals, criteria: Criteria) -> TierResult:
    w = criteria.get("final_score", "weights", default={}) or {}
    req = ev.requirements.score if ev.requirements and ev.requirements.score is not None else NEUTRAL
    if ev.requirements and ev.requirements.score is None:
        ev.flag("Requirements not parsed")
    necessity = max(0.0, min(100.0, NEUTRAL + sig.total("necessity")))
    fit = (req * float(w.get("requirements_match", 0)) + sig.experience_fit * float(w.get("experience_fit", 0))
           + necessity * float(w.get("signals", 0)))
    fit = round(fit, 1)
    sort = round(fit + sig.total("preference"), 1)

    tiers = criteria.get("tiers", default={}) or {}
    strong, worth = tiers.get("strong", {}) or {}, tiers.get("worth_a_look", {}) or {}
    no_gaps = not (ev.requirements and ev.requirements.has_required_gap)
    if fit >= _threshold(strong) and (no_gaps or not strong.get("no_required_gaps")):
        tier = "Strong"
    elif fit >= _threshold(worth):
        tier = "Worth a look"
    else:
        tier = "Stored"
    if tier == "Stored" and ev.posting.anchor and tiers.get("anchors_always_shown"):
        tier = "Worth a look"
    if sig.role_type == "Sales-adjacent" or sig.sales_adjacent:
        ev.flag("Sales-adjacent")
    return TierResult(fit, sort, tier, _why(ev, sig, req))


def _why(ev: Evaluation, sig: Signals, req: float) -> str:
    parts = []
    r = ev.requirements
    if r and r.items:
        met = sum(1 for i in r.items if i.cls == "met")
        gaps = [i.label for i in r.items if i.cls == "gap" and i.section == "required"]
        parts.append(f"meets {met}/{len(r.items)}" + (f", gap: {', '.join(gaps[:2])}" if gaps else ""))
    elif r:
        parts.append("requirements not parsed")
    if sig.experience_notes:
        parts.append(sig.experience_notes[0])
    ranked = sorted(sig.points, key=lambda p: -abs(p.points))
    for p in ranked:
        if len(parts) >= 3:
            break
        parts.append(f"{p.name} {p.points:+g}")
    return "; ".join(parts[:3])
