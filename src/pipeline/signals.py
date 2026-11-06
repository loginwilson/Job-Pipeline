"""Rule-based signals (criteria.yaml section 2) and role-type labels.

Owner's principle: NECESSITIES decide the tier; PREFERENCES only reorder.
  necessity  -> location tier, timing, salary bump / unknown salary, work
                authorization (sponsorship, no-sponsorship, TN fit),
                technical stretch/gap terms, experience fit
  preference -> lane match, description core signals, product-design
                signals, sector bonus, company upside, role_preference
                (Sales, Sales-adjacent, analyst titles)
Preference points never move a posting between tiers; they only sort it
within its tier. All point sizes come from criteria.yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Criteria
from ..textmatch import find_terms, normalize, term_regex
from .gates import Evaluation


@dataclass
class Point:
    name: str
    points: float
    kind: str          # necessity / preference


@dataclass
class Signals:
    role_type: str = "Other"
    sales_adjacent: bool = False
    lane_match: bool = False          # title matched a roles.lanes entry (not just a label)
    points: list[Point] = field(default_factory=list)
    experience_fit: float = 100.0
    experience_notes: list[str] = field(default_factory=list)

    def total(self, kind: str) -> float:
        return sum(p.points for p in self.points if p.kind == kind)

    def add(self, name: str, pts: float, kind: str) -> None:
        if pts:
            self.points.append(Point(name, pts, kind))


_PRODUCT_DESIGN = re.compile(r"\bdesign", re.I)


class SignalScorer:
    def __init__(self, criteria: Criteria):
        self.c = criteria

    # --- role type --------------------------------------------------------
    def role_type(self, ev: Evaluation) -> tuple[str, bool, bool]:
        """Label from the title (department ignored); Sales-adjacent from duties."""
        c, p = self.c, ev.posting
        roles = c.get("roles", default={}) or {}
        title = p.title
        labels = roles.get("role_type_labels", []) or []
        sales_flags = c.get("description", "sales_flags", default={}) or {}
        hits = {h.term for h in find_terms(p.description_lines, sales_flags.get("terms", []))}
        adjacent = len(hits) >= int(sales_flags.get("label_if_count_at_least", 2))

        if find_terms(title, roles.get("sales_titles", []), first_only=True):
            return "Sales", False, False
        lanes = roles.get("lanes", {}) or {}
        lane_label = {"deployment": "Deployment", "product": "Product",
                      "workflow_automation": "Workflow Automation", "strategy": "Strategy & Ops",
                      "real_estate_data": "Real Estate Data"}
        re_data = self._re_data_lane(p)
        if re_data:
            return "Real Estate Data", adjacent, True
        for lane, terms in lanes.items():
            if isinstance(terms, list) and find_terms(title, terms, first_only=True):
                label = lane_label.get(lane, lane.replace("_", " ").title())
                if label == "Product" and _PRODUCT_DESIGN.search(title) and "Product Design" in labels:
                    label = "Product Design"
                return label, adjacent, True
        expanded = c.get("discovery", "expanded_lanes", default={}) or {}
        for lane, spec in expanded.items():
            titles = (spec or {}).get("titles", []) if isinstance(spec, dict) else []
            label = next((lb for lb in labels if normalize(lb).replace("&", "and") in
                          lane.replace("_", " ").replace("and ", "")), None)
            if label == "Real Estate Data" and not re_data:
                continue  # the real-estate-data label needs the company sector (roles.lanes.real_estate_data)
            if label and find_terms(title, titles, first_only=True):
                return label, adjacent, False
        for label in labels:
            if label in ("Other", "Sales", "Sales-adjacent"):
                continue
            if term_regex(label).search(normalize(title, lower=False)):
                return label, adjacent, False
        return ("Sales-adjacent", True, False) if adjacent else ("Other", False, False)

    def _re_data_lane(self, p) -> bool:
        lane = self.c.get("roles", "lanes", "real_estate_data", default={}) or {}
        sectors = {normalize(s) for s in p.company_info.sectors}
        need = {normalize(s) for s in lane.get("requires_company_sector", [])}
        return bool(sectors & need) and bool(find_terms(p.title, lane.get("titles", []), first_only=True))

    # --- experience fit -----------------------------------------------------
    def experience_fit(self, ev: Evaluation, sig: Signals) -> None:
        exp = self.c.get("experience", default={}) or {}
        p = ev.posting
        total = 0.0
        for kind in ("required", "preferred"):
            yrs = p.experience.max_years(kind)
            if yrs is None or yrs < 1:
                continue
            table = {k: v for k, v in (exp.get(kind) or {}).items() if k != "drop_at_or_above"}
            best = None
            for key in table:
                low = float(str(key).split("-")[0])
                if low <= yrs and (best is None or low > float(str(best).split("-")[0])):
                    best = key
            pen = self.c.weight(table[best]) if best is not None else 0.0
            if pen:
                ask_lines = [a.line for a in p.experience.asks if a.kind == kind]
                offset = find_terms(ask_lines, (exp.get("field_match_offset") or {}).get("terms", []),
                                    first_only=True)
                if offset:
                    sig.experience_notes.append(f"{kind} {yrs:g} yrs offset by field match '{offset[0].term}'")
                    pen = 0.0
                else:
                    sig.experience_notes.append(f"{kind} {yrs:g} yrs ({pen:+g})")
            total += pen
        fw = exp.get("friendly_wording_bonus") or {}
        hit = find_terms(p.description_lines, fw.get("terms", []), first_only=True)
        if hit:
            total += self.c.weight(fw.get("effect"))
            sig.experience_notes.append(f"friendly wording '{hit[0].term}'")
        sig.experience_fit = max(0.0, min(100.0, 100.0 + total))

    # --- main ----------------------------------------------------------------
    def score(self, ev: Evaluation) -> Signals:
        c, p = self.c, ev.posting
        sig = Signals()
        sig.role_type, sig.sales_adjacent, sig.lane_match = self.role_type(ev)
        self.experience_fit(ev, sig)
        W = c.weight

        # necessity: location tier
        best = p.location.best
        if best:
            tier_pts = (c.get("location", "tiers", default={}) or {}).get(best.tier, {}).get("points", 0)
            sig.add(f"{best.region} tier {best.tier}", W(tier_pts), "necessity")

        # necessity: timing
        rank = c.get("timing", "rank", default={}) or {}
        start = p.timing.earliest_start()
        if start and start.start[1] == 1 and start.start < c.graduation:
            flex = find_terms(p.description_lines, c.get("timing", "flag_january_flexibility_terms", default=[]),
                              first_only=True)
            key = "january_part_time" if (p.employment_type == "part_time" or flex) else "january_full_time"
            sig.add(key.replace("_", " "), W(rank.get(key, 0)), "necessity")
        else:
            sig.add("post-grad start", W(rank.get("post_grad_full_time", 0)), "necessity")

        # necessity: salary
        sal = c.get("salary", default={}) or {}
        if p.comp.known:
            market = {"USD": "US", "CAD": "CA"}.get(p.comp.currency or "", p.location.market or "US")
            target = ((sal.get("markets") or {}).get(market) or {}).get("pass")
            if target and p.comp.midpoint_annual > target:
                sig.add("salary above target", W(sal.get("score_bump_above_target")), "necessity")
        else:
            sig.add("salary unknown", -W("mild"), "necessity")  # "ranks below confirmed passes"

        # necessity: work authorization
        wa = c.get("work_authorization", default={}) or {}
        if ev.sponsorship == "available":
            sig.add("sponsorship available", W(wa.get("bonus")), "necessity")
        elif ev.sponsorship == "none":
            sig.add("no sponsorship (TN path)", W((wa.get("no_sponsorship") or {}).get("penalty")), "necessity")
        if ev.tn and ev.tn.applies and ev.tn.points:
            sig.add(f"TN {ev.tn.fit}", ev.tn.points, "necessity")

        # necessity: technical tolerance
        tech = (c.get("description", "technical", "penalty", default={}) or {})
        terms = list(tech.get("stretch_terms", [])) + list(tech.get("gap_terms", []))
        hit = find_terms(p.description_lines, terms, first_only=True)
        if hit:
            sig.add(f"technical ask '{hit[0].term}'", W(tech.get("effect")), "necessity")

        # preference: lane, description, sector, company, role preference
        if sig.lane_match:
            sig.add(f"{sig.role_type} lane", W(c.get("roles", "lane_match_points")), "preference")
        if sig.role_type == "Real Estate Data":
            sig.add("real estate data lane", W(c.get("roles", "lanes", "real_estate_data", "bonus")), "preference")
        core = c.get("description", "core_signals", default={}) or {}
        if find_terms(p.description_lines, core.get("terms", []), first_only=True):
            sig.add("north-star duties", W(core.get("effect")), "preference")
        pd = c.get("description", "product_design_signals", default={}) or {}
        if sig.role_type == "Product Design" and find_terms(p.description_lines, pd.get("terms", []), first_only=True):
            sig.add("product design signals", W(pd.get("effect")), "preference")
        sectors = {normalize(s) for s in p.company_info.sectors} | {normalize(p.company_info.category or "")}
        for key, eff in (c.get("description", "sector_bonus", default={}) or {}).items():
            if normalize(key) in sectors:
                sig.add(f"sector {key}", W(eff), "preference")
        comp_cfg = c.get("company", default={}) or {}
        if p.anchor or p.company in (comp_cfg.get("anchors") or []):
            sig.add("anchor company", W((comp_cfg.get("scoring") or {}).get("anchor_or_strong_upside")), "preference")

        pref = c.get("final_score", "role_preference", default={}) or {}
        if sig.role_type in pref:
            sig.add(f"{sig.role_type} (less preferred)", float(pref[sig.role_type]), "preference")
        elif sig.sales_adjacent and "Sales-adjacent" in pref:
            sig.add("sales-heavy duties", float(pref["Sales-adjacent"]), "preference")
        if "analyst_titles" in pref and re.search(r"analyst", p.title, re.I):
            sig.add("analyst title", float(pref["analyst_titles"]), "preference")
        return sig
