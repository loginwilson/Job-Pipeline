"""Every pass/fail rule in criteria.yaml. Each failure records gate + matched text.

All gates are evaluated (not just the first) so the Dropped tab can show when
a posting fails several rules at once; `failures[0]` is the primary gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Criteria
from ..textmatch import find_terms, normalize, term_spans
from .normalize import Posting
from .requirements import RequirementsMatcher, RequirementsResult
from .tn import TNResult, map_tn


@dataclass
class GateFailure:
    gate: str
    matched: str


@dataclass
class Evaluation:
    posting: Posting
    failures: list[GateFailure] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    sponsorship: str = "n/a"           # available / none / unknown / n/a (non-US)
    tn: TNResult | None = None
    requirements: RequirementsResult | None = None

    @property
    def passed(self) -> bool:
        return not self.failures

    def fail(self, gate: str, matched: str) -> None:
        self.failures.append(GateFailure(gate, (matched or "").strip()[:240]))

    def flag(self, text: str) -> None:
        if text and text not in self.flags:
            self.flags.append(text)


def _rule_term(rule: str, prefix: str) -> str | None:
    return rule[len(prefix):].replace("_", " ") if rule.startswith(prefix) else None


class Gates:
    def __init__(self, criteria: Criteria, req_matcher: RequirementsMatcher | None = None):
        self.c = criteria
        self.req = req_matcher or RequirementsMatcher(criteria)

    # ------------------------------------------------------------------
    def evaluate(self, p: Posting) -> Evaluation:
        ev = Evaluation(posting=p)
        ev.requirements = self.req.match(p.required_text.split("\n") if p.required_text else [],
                                         p.preferred_text.split("\n") if p.preferred_text else [])
        for gate in (self.employment_type, self.engineering_title, self.gov_defense, self.seniority,
                     self.location, self.salary, self.experience, self.work_authorization,
                     self.technical, self.hard_gap, self.timing, self.company):
            gate(p, ev)
        return ev

    # 1. employment type ------------------------------------------------
    def employment_type(self, p: Posting, ev: Evaluation) -> None:
        et = self.c.get("employment_type", default={})
        allow, drop = et.get("allow", []), et.get("drop", [])
        t = p.employment_type
        if t in drop:
            ev.fail("employment_type", f"{t} ({p.employment_type_reason})")
        elif t == "part_time" and "part_time_permanent" in allow:
            return
        elif t not in allow and t != "unknown":
            ev.fail("employment_type", f"{t} not in allow list ({p.employment_type_reason})")

    # 2. engineering titles --------------------------------------------
    def engineering_title(self, p: Posting, ev: Evaluation) -> None:
        eng = self.c.get("roles", "drop_titles", "engineering", default={}) or {}
        word = _rule_term(str(eng.get("rule", "")), "drop_any_title_containing_")
        if word and re.search(rf"\b{re.escape(word)}", p.title, re.IGNORECASE):
            ev.fail("engineering_title", f"title contains '{word}': {p.title}")
            return
        hit = find_terms(p.title, eng.get("also", []), first_only=True)
        if hit:
            ev.fail("engineering_title", f"title term '{hit[0].term}': {p.title}")

    # 3. government / defense -------------------------------------------
    def gov_defense(self, p: Posting, ev: Evaluation) -> None:
        gd = self.c.get("exclusions", "government_defense", default={}) or {}
        fields = [p.title] + [x for x in (p.team, p.department) if x]
        hit = find_terms(fields, gd.get("title_or_team_terms", []), first_only=True)
        if hit:
            ev.fail("gov_defense", f"'{hit[0].term}' in title/team: {hit[0].line}")
        if gd.get("drop_defense_primary_companies") and p.company_info.defense_primary:
            ev.fail("gov_defense", f"{p.company} is a defense-primary company")
        for key, label in (("industries", "industry"), ("companies", "company")):
            excl = self.c.get("exclusions", key, default=[]) or []
            if key == "companies" and p.company in excl:
                ev.fail("exclusion", f"excluded {label}: {p.company}")
            if key == "industries" and set(excl) & set(p.company_info.sectors):
                ev.fail("exclusion", f"excluded {label}: {set(excl) & set(p.company_info.sectors)}")

    # 4. seniority -------------------------------------------------------
    def seniority(self, p: Posting, ev: Evaluation) -> None:
        s = self.c.get("seniority", default={}) or {}
        mt = s.get("manager_titles", {}) or {}
        keep = mt.get("keep", []) or []
        # explicit pass lists win over generic level words ("executive" in "Account Executive")
        protected = term_spans(p.title, keep + (self.c.get("roles", "sales_titles", default=[]) or []))
        hit = find_terms(p.title, s.get("drop_title_terms", []), first_only=True,
                         ignore_spans={0: protected})
        if hit:
            ev.fail("seniority", f"title term '{hit[0].term}': {p.title}")
        if not re.search(r"\bmanagers?\b", p.title, re.IGNORECASE):
            return
        if mt.get("keep_takes_precedence") and find_terms(p.title, keep, first_only=True):
            return
        dw = mt.get("drop_when", {}) or {}
        lvl = find_terms(p.title, dw.get("level_titles", []), first_only=True)
        if lvl:
            ev.fail("seniority", f"manager level title '{lvl[0].term.strip()}': {p.title}")
            return
        sectors = {normalize(x) for x in p.company_info.sectors}
        firm = [x for x in dw.get("standalone_manager_at", []) if normalize(x) in sectors]
        if firm:
            ev.fail("seniority", f"'Manager' is a career level at {firm[0]} firms: {p.title}")
            return
        duty = find_terms(p.description_lines, dw.get("duties_include", []), first_only=True)
        if duty:
            ev.fail("seniority", f"people-management duty '{duty[0].term}': {duty[0].line}")

    # 5. location --------------------------------------------------------
    def location(self, p: Posting, ev: Evaluation) -> None:
        loc = self.c.get("location", default={}) or {}
        arr = loc.get("work_arrangement", {}) or {}
        if p.location.fully_remote and "fully_remote" in arr.get("drop", []):
            ev.fail("work_arrangement", f"fully remote: {', '.join(p.locations) or p.work_arrangement}")
            return
        if not p.location.offices and loc.get("drop_everything_else", True):
            ev.fail("location", f"no tier city in: {'; '.join(p.locations) or 'none listed'}")

    # 6. salary ----------------------------------------------------------
    def salary(self, p: Posting, ev: Evaluation) -> None:
        sal = self.c.get("salary", default={}) or {}
        comp = p.comp
        if comp.ote_only:
            ev.fail("salary", f"OTE/commission only — never counts as base: {comp.matched_text}")
            return
        if not comp.known:
            if sal.get("missing_salary") == "keep_and_flag":
                ev.flag("Salary unknown")
            return
        market = {"USD": "US", "CAD": "CA"}.get(comp.currency or "", p.location.market or "US")
        m = (sal.get("markets") or {}).get(market) or {}
        mid = comp.midpoint_annual
        if mid >= m.get("pass", 0):
            return
        floor = m.get("pass_with_extra_comp", m.get("pass", 0))
        if mid >= floor and (comp.has_equity or comp.has_bonus):
            if comp.equity_unquantified and not comp.has_bonus:
                ev.flag("Equity unquantified")
            return
        why = "below extra-comp floor" if mid < floor else "needs extra comp, none found"
        ev.fail("salary", f"midpoint {comp.currency or ''} {mid:,.0f} {why} ({market} pass {m.get('pass'):,}, "
                          f"floor {floor:,}): {comp.matched_text}")

    # 7. experience ------------------------------------------------------
    def experience(self, p: Posting, ev: Evaluation) -> None:
        exp = self.c.get("experience", default={}) or {}
        for kind in ("required", "preferred"):
            cap = (exp.get(kind) or {}).get("drop_at_or_above")
            if cap is None:
                continue
            asks = [a for a in p.experience.asks if a.kind == kind and a.years >= cap]
            if asks:
                top = max(asks, key=lambda a: a.years)
                ev.fail("experience", f"{top.years:g}+ yrs {kind} (cap {cap}): {top.line}")
                return

    # 8. work authorization (US roles only) ------------------------------
    def work_authorization(self, p: Posting, ev: Evaluation) -> None:
        wa = self.c.get("work_authorization", default={}) or {}
        ev.tn = map_tn(p, self.c)
        for f in ev.tn.flags:
            if p.is_canada or f != (wa.get("tn_alignment", {}).get("scoring", {}) or {}).get("flag_if_unlikely"):
                ev.flag(f)
        if not p.is_us:
            return
        hit = find_terms(p.description_lines, wa.get("drop_terms", []), first_only=True)
        if hit:
            ev.fail("work_authorization", f"'{hit[0].term}': {hit[0].line}")
            return
        nos = wa.get("no_sponsorship", {}) or {}
        no_hit = find_terms(p.description_lines, nos.get("terms", []), first_only=True)
        if no_hit:
            ev.sponsorship = "none"
            if ev.tn.fit in ("likely", "possible"):
                ev.flag(nos.get("flag", "TN possible — verify"))
            else:
                ev.fail("work_authorization", f"no sponsorship and TN unlikely: {no_hit[0].line}")
            return
        if find_terms(p.description_lines, wa.get("bonus_terms", []), first_only=True):
            ev.sponsorship = "available"
            return
        ev.sponsorship = "unknown"
        if wa.get("silent") == "keep_and_flag":
            ev.flag("Sponsorship unknown")
        if ev.tn.fit == "unlikely":
            ev.flag(wa.get("tn_alignment", {}).get("scoring", {}).get("flag_if_unlikely", "TN unlikely"))

    # 9. technical drop terms (description) ------------------------------
    def technical(self, p: Posting, ev: Evaluation) -> None:
        terms = self.c.get("description", "technical", "drop", default=[]) or []
        hit = find_terms(p.description_lines, terms, first_only=True)
        if hit:
            ev.fail("technical", f"'{hit[0].term}': {hit[0].line}")

    # 10. requirements hard gaps -----------------------------------------
    def hard_gap(self, p: Posting, ev: Evaluation) -> None:
        if ev.requirements and ev.requirements.hard_gap:
            h = ev.requirements.hard_gap
            ev.fail("hard_gap", f"required: {h.text} [{h.label}]")

    # 11. timing ---------------------------------------------------------
    def timing(self, p: Posting, ev: Evaluation) -> None:
        rule = str(self.c.get("timing", "drop", default=""))
        if not rule.startswith("degree_required_before_start"):
            return
        grad = self.c.graduation
        t = p.timing
        start = t.earliest_start()
        if start and start.start < grad and t.degree_required:
            ev.fail("timing", f"degree required before a start before {grad[0]}-{grad[1]:02d}: {start.text}")
            return
        if t.grad_windows and not any(w.start <= grad <= w.end for w in t.grad_windows) \
                and all(w.end < grad for w in t.grad_windows):
            ev.fail("timing", f"graduation window ends before {grad[0]}-{grad[1]:02d}: {t.grad_windows[0].text}")
            return
        if start and start.start[1] == 1 and start.start < grad:
            ev.flag("January start")

    # 12. company --------------------------------------------------------
    def company(self, p: Posting, ev: Evaluation) -> None:
        info = p.company_info
        for rule in self.c.get("company", "drop_if", default=[]) or []:
            m = re.match(r"headcount_under_(\d+)", str(rule))
            if m and info.headcount is not None and info.headcount < int(m.group(1)):
                ev.fail("company", f"headcount {info.headcount} < {m.group(1)}")
            if str(rule) == (info.stage or "").replace("-", "_").lower():
                ev.fail("company", f"stage {info.stage}")
