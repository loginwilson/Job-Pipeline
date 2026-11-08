"""The must-haves in criteria.yaml (v2). Each failure records rule + matched text.

Every rule is evaluated so the Dropped tab can show all the reasons a posting
was cut; failures[0] is the primary one. Nothing here scores or ranks: a
posting either passes (and Login decides) or it doesn't.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..config import Criteria
from ..textmatch import find_terms, normalize, term_spans
from . import qualifications
from .fields import field_of
from .normalize import Posting
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
    field: str = "Other"
    visa: str = ""
    tn: TNResult | None = None

    @property
    def passed(self) -> bool:
        return not self.failures

    def fail(self, gate: str, matched: str) -> None:
        self.failures.append(GateFailure(gate, (matched or "").strip()[:240]))

    def flag(self, text: str) -> None:
        if text and text not in self.flags:
            self.flags.append(text)


class Gates:
    def __init__(self, criteria: Criteria):
        self.c = criteria

    def evaluate(self, p: Posting) -> Evaluation:
        ev = Evaluation(posting=p)
        for rule in (self.real_opening, self.opt_related, self.employment_type, self.location, self.salary, self.experience,
                     self.seniority, self.work_authorization, self.technical_and_credentials, self.timing):
            rule(p, ev)
        ev.field = field_of(p, self.c)
        return ev

    # 10. a real opening (not a talent pool / general application) ----------
    def real_opening(self, p: Posting, ev: Evaluation) -> None:
        hit = find_terms(p.title, self.c.get("openings", "drop_title_terms", default=[]) or [], first_only=True)
        if hit:
            ev.fail("not_a_real_opening", f"'{hit[0].term}': {p.title}")

    # 11. related to the degree (OPT) -----------------------------------------
    def opt_related(self, p: Posting, ev: Evaluation) -> None:
        terms = self.c.get("opt_relatedness", "unrelated_title_terms", default=[]) or []
        hit = find_terms(p.title, terms, first_only=True)
        if hit:
            ev.fail("not_related_to_degree", f"'{hit[0].term}' field isn't supported by your degree (OPT): {p.title}")

    # 1. employment type --------------------------------------------------
    def employment_type(self, p: Posting, ev: Evaluation) -> None:
        et = self.c.get("employment_type", default={}) or {}
        t = p.employment_type
        if t in (et.get("drop") or []):
            ev.fail("employment_type", f"{t} ({p.employment_type_reason})")
        elif t not in (et.get("allow") or []) and t != "unknown":
            ev.fail("employment_type", f"{t} not allowed ({p.employment_type_reason})")

    # 2. location ----------------------------------------------------------
    def location(self, p: Posting, ev: Evaluation) -> None:
        loc = self.c.get("location", default={}) or {}
        if p.location.fully_remote and loc.get("drop_fully_remote", True):
            ev.fail("location", f"fully remote: {', '.join(p.locations) or p.work_arrangement}")
        elif not p.location.offices:
            ev.fail("location", f"no listed city in: {'; '.join(p.locations) or 'none listed'}")
        elif p.location.arrangement == "remote":
            ev.flag(loc.get("remote_with_city_flag", "Remote role — lists your city"))

    # 3. salary -------------------------------------------------------------
    def salary(self, p: Posting, ev: Evaluation) -> None:
        comp = p.comp
        if comp.ote_only:
            ev.flag("Salary unknown (OTE only)")
            return
        if not comp.known:
            sal = self.c.get("salary", default={}) or {}
            floor = ((sal.get("markets") or {}).get(p.location.market or "US") or {}).get("floor")
            hit = find_terms(p.title, sal.get("rarely_meets_floor_titles", []) or [], first_only=True)
            if hit:
                ev.fail("salary", f"no salary posted; '{hit[0].term}' roles rarely pay the {floor:,} floor: {p.title}")
                return
            store = sal.get("store_roles") or {}
            st = find_terms(p.title, store.get("titles", []) or [], first_only=True)
            if st:
                where = find_terms([p.title] + p.description_lines, store.get("store_terms", []) or [], first_only=True)
                if where:
                    ev.fail("salary", f"no salary posted; store/branch role ('{st[0].term}', '{where[0].term}'): {p.title}")
                    return
            basis = (p.raw.raw.get("pay_basis") if p.raw and isinstance(p.raw.raw, dict) else None)
            if basis == "hour" and sal.get("hourly_without_amount_is_below_floor"):
                ev.fail("salary", f"paid hourly, no amount posted (below the {floor:,} floor): {p.title}")
                return
            ev.flag("Salary unknown")
            return
        markets = self.c.get("salary", "markets", default={}) or {}
        market = {"USD": "US", "CAD": "CA"}.get(comp.currency or "", p.location.market or "US")
        m = markets.get(market) or {}
        mid = comp.midpoint_annual
        if mid < m.get("floor", 0):
            ev.fail("salary", f"midpoint {comp.currency or ''} {mid:,.0f} below floor {m.get('floor'):,}: "
                              f"{comp.matched_text}")
        elif mid < m.get("comfortable", 0):
            ev.flag("Low base — check incentives")

    # 4. experience ----------------------------------------------------------
    def experience(self, p: Posting, ev: Evaluation) -> None:
        cap = self.c.get("experience", "drop_at_or_above")
        asks = [a for a in p.experience.asks if cap is not None and a.years >= cap]
        if asks:
            top = max(asks, key=lambda a: a.years)
            ev.fail("experience", f"{top.years:g}+ yrs {top.kind}: {top.line}")
            return
        vague = find_terms(p.description_lines, self.c.get("experience", "unstated_senior_terms", default=[]) or [],
                           first_only=True)
        if vague:
            ev.fail("experience", f"'{vague[0].term}' (several years implied): {vague[0].line}")

    # 5. seniority -----------------------------------------------------------
    def seniority(self, p: Posting, ev: Evaluation) -> None:
        s = self.c.get("seniority", default={}) or {}
        mt = s.get("manager_titles", {}) or {}
        keep = mt.get("keep", []) or []
        title = p.title
        protected = term_spans(title, list(s.get("keep_titles_with", []) or []) + keep)
        hit = find_terms(title, s.get("drop_title_terms", []), first_only=True, ignore_spans={0: protected})
        if hit:
            ev.fail("seniority", f"title term '{hit[0].term}': {title}")
            return
        nums = [re.escape(str(n)) for n in s.get("drop_level_numerals", []) or []]
        if nums and (re.search(rf"(?:\s|,|-|^)(?:{'|'.join(nums)})\s*(?:$|[,(]|\s[-–—]\s)", title)
                     or re.search(rf"\blevel\s*(?:{'|'.join(nums)})\b", title, re.I)):
            ev.fail("seniority", f"level numeral: {title}")
            return
        if s.get("senior_title_in_description"):
            core = re.sub(r"\s*[\(,–-].*$", "", title).strip()
            if len(core.split()) >= 2:
                rx = re.compile(r"(?:^|[.!?:]\s+)[-\s]*(?:the\s+|as\s+(?:an?|the|our)\s+|this\s+)?(?:senior|sr\.?)\s+(?:[A-Za-z&/]+\s+){0,2}" + re.escape(core) + r"\b", re.I)
                for line in p.description_lines:
                    m = rx.search(line)
                    if m:
                        ev.fail("seniority", f"description calls it '{m.group(0)}': {line.strip()[:160]}")
                        return
        lead = s.get("lead", {}) or {}
        if lead.get("drop_when_level") and re.search(r"\blead\b", title, re.I) \
                and not find_terms(title, lead.get("keep_phrases", []), first_only=True):
            ev.fail("seniority", f"'Lead' as a level: {title}")
            return
        if not re.search(r"\bmanagers?\b", title, re.I):
            return
        if mt.get("keep_takes_precedence") and find_terms(title, keep, first_only=True):
            return
        dw = mt.get("drop_when", {}) or {}
        start = next((t for t in dw.get("title_starts_with", []) or [] if title.lower().startswith(t.lower())), None)
        if start:
            ev.fail("seniority", f"manager level title '{start.strip()}': {title}")
            return
        lvl = find_terms(title, dw.get("level_titles", []), first_only=True)
        if lvl:
            ev.fail("seniority", f"manager level title '{lvl[0].term.strip()}': {title}")
            return
        duty = find_terms(p.description_lines, dw.get("duties_include", []), first_only=True)
        if duty:
            ev.fail("seniority", f"people-management duty '{duty[0].term}': {duty[0].line}")

    # 6. work authorization -------------------------------------------------
    def work_authorization(self, p: Posting, ev: Evaluation) -> None:
        wa = self.c.get("work_authorization", default={}) or {}
        ev.tn = map_tn(p, self.c)
        if p.is_canada:
            ev.visa = "No visa needed (Canada)"
            return
        if not p.is_us:
            return
        hit = find_terms(p.description_lines, wa.get("drop_terms", []), first_only=True)
        if hit:
            ev.fail("work_authorization", f"'{hit[0].term}': {hit[0].line}")
        parts = ["OPT year 1", ev.tn.label.replace("TN:", "TN later:") if ev.tn.label else "TN later: unclear"]
        nos = wa.get("no_sponsorship", {}) or {}
        if find_terms(p.description_lines, nos.get("terms", []), first_only=True):
            ev.flag(nos.get("flag", "No sponsorship"))
        elif find_terms(p.description_lines, wa.get("sponsorship_available_terms", []), first_only=True):
            parts.append("sponsorship mentioned")
        for f in ev.tn.flags:
            if "verify" in f.lower() or "before degree" in f.lower():
                ev.flag(f)
        ev.visa = " · ".join(parts)

    # 7 + 8. technical asks and credentials ----------------------------------
    def technical_and_credentials(self, p: Posting, ev: Evaluation) -> None:
        tech = self.c.get("technical", default={}) or {}
        title_hit = None
        if not find_terms(p.title, tech.get("keep_titles", []) or [], first_only=True):
            for term in tech.get("drop_titles", []) or []:
                if re.search(rf"\b{re.escape(term)}", p.title, re.I):
                    title_hit = term
                    break
        if title_hit:
            ev.fail("technical", f"title contains '{title_hit}': {p.title}")
        cred = find_terms(p.title, self.c.get("credentials", "drop_title_terms", default=[]) or [], first_only=True)
        if cred:
            ev.fail("credentials", f"title asks for '{cred[0].term}': {p.title}")
        for h in qualifications.check(p, self.c):
            ev.fail(h.rule, f"{h.reason}: {h.line}")

    # 9. start date -----------------------------------------------------------
    def timing(self, p: Posting, ev: Evaluation) -> None:
        tm = self.c.get("timing", default={}) or {}
        grad = self.c.graduation
        t = p.timing
        hit = find_terms(p.title, tm.get("drop_title_terms", []) or [], first_only=True)
        if hit:
            ev.fail("start_date", f"title says '{hit[0].term}': {p.title}")
            return
        start = t.earliest_start()
        if tm.get("drop_start_before_graduation") and start and start.start < grad:
            ev.fail("start_date", f"starts before graduation ({grad[0]}-{grad[1]:02d}): {start.text}")
            return
        if t.grad_windows and all(w.end < grad for w in t.grad_windows):
            ev.fail("start_date", f"for graduates before {grad[0]}-{grad[1]:02d}: {t.grad_windows[0].text}")
            return
        latest = self.c.latest_start
        if latest and start and date(start.start[0], start.start[1], 1) > date(*latest):
            ev.flag(tm.get("flag_start_after", "Starts after preferred date"))
