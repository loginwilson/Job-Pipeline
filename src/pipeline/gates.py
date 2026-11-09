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
from .programs import PROGRAM_RX
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
        from ..config import CONFIG_DIR, load_yaml
        path = CONFIG_DIR / "target_companies.yaml"
        data = (load_yaml(path) or {}) if path.exists() else {}
        self.targets = {str(c.get("name", "")).strip().lower() for c in data.get("companies") or []}

    def evaluate(self, p: Posting) -> Evaluation:
        ev = Evaluation(posting=p)
        for rule in (self.real_opening, self.opt_related, self.field_fit, self.title_language, self.has_description, self.specialist_text, self.edge, self.employment_type, self.location, self.salary, self.experience,
                     self.seniority, self.work_authorization, self.technical_and_credentials, self.timing):
            rule(p, ev)
        ev.field = field_of(p, self.c)
        return ev

    # 10. a real opening (not a talent pool / general application) ----------
    def real_opening(self, p: Posting, ev: Evaluation) -> None:
        from ..sources.jsearch import _aggregator
        if _aggregator(p.company or ""):
            ev.fail("not_a_real_opening", f"'{p.company}' is a job site reposting someone else's job")
            return
        hit = find_terms(p.title, self.c.get("openings", "drop_title_terms", default=[]) or [], first_only=True)
        if hit:
            ev.fail("not_a_real_opening", f"'{hit[0].term}': {p.title}")

    # 11. related to the degree (OPT) -----------------------------------------
    def opt_related(self, p: Posting, ev: Evaluation) -> None:
        terms = self.c.get("opt_relatedness", "unrelated_title_terms", default=[]) or []
        hit = find_terms(p.title, terms, first_only=True)
        if hit:
            ev.fail("not_related_to_degree", f"'{hit[0].term}' field isn't supported by your degree (OPT): {p.title}")

    # 12. field fit (your fields only) ------------------------------------
    def has_description(self, p: Posting, ev: Evaluation) -> None:
        """A posting whose text is just a link (or empty) can't be checked against anything."""
        need = int(self.c.get("description_min_words", default=0) or 0)
        if not need or p.raw is None or not getattr(p.raw, "description_lines", None):
            return                                        # title-only first pass: decided later
        words = sum(len(ln.split()) for ln in p.description_lines if not ln.strip().startswith("http"))
        if words < need:
            ev.fail("no_description", "no job description to check (open the link to read it)")

    def specialist_text(self, p: Posting, ev: Evaluation) -> None:
        """Reads the WHOLE description: a posting packed with quant / engineering / research
        vocabulary is a specialist job whatever its title says."""
        st = self.c.get("specialist_text", default=None)
        if not st or not p.description_lines:
            return
        hits = sorted({h.term.lower() for h in find_terms(p.description_lines, st.get("terms") or [])})
        if qualifications.is_ai_builder(p.title, self.c):
            hits = [h for h in hits if h not in {"python", "api development", "ci/cd", "llms", "algorithms",
                                                  "computer science", "software engineering", "data structures",
                                                  "machine learning", "microservices", "distributed systems"}]
        if len(hits) >= int(st.get("drop_at", 4)):
            ev.fail("specialist", f"specialist job ({len(hits)} signs: {', '.join(hits[:5])})")
            return
        bt = self.c.get("banking_text", default=None)
        if bt and not (self._title_domain(p) or self._company_domain(p)):
            bank = sorted({h.term.lower() for h in find_terms(p.description_lines, bt.get("terms") or [])})
            if len(bank) >= int(bt.get("drop_at", 4)) and not self._stretch_banking(p, ev):
                ev.fail("field", f"investment banking / markets job ({len(bank)} signs: {', '.join(bank[:5])})")

    def _company_domain(self, p: Posting) -> str | None:
        """Hospitality / real estate from the company's own opening description (the title
        alone misses 'SDR | Housing' at a property-management AI company)."""
        ff = self.c.get("field_fit", default={}) or {}
        intro = p.description_lines[: int(ff.get("core_from_description_lines", 12))]
        for domain, terms in (ff.get("core_company_terms") or {}).items():
            if find_terms(intro, terms, first_only=True):
                return domain
        return None

    def _title_domain(self, p: Posting) -> str | None:
        ff = self.c.get("field_fit", default={}) or {}
        return next((f for f, terms in (ff.get("core_families") or {}).items()
                     if find_terms(p.title, terms, first_only=True)), None)

    def field_fit(self, p: Posting, ev: Evaluation) -> None:
        ff = self.c.get("field_fit", default=None)
        if not ff:
            return
        title = p.title
        # your core fields (hospitality / real estate, by title or by the company's description)
        # keep any role except legal / HR / tech / design / product ones; finance there is your
        # concentration, so it stays
        if qualifications.is_ai_builder(p.title, self.c):
            # an AI-builder title is one of your fields - unless it names another field
            # ('Forward Deployed Accountant'); 'tech' terms are expected in these titles
            for group, terms in (ff.get("unrelated_title_terms") or {}).items():
                hit = None if group == "tech" else find_terms(title, terms, first_only=True)
                if hit:
                    ev.fail("field", f"{group.replace('_', ' ')} ('{hit[0].term}'): {title}")
                    return
            ev.flag("AI builder")
            return
        title_domain = self._title_domain(p)
        company = title_domain or (self._company_domain(p) if p.description_lines else None)
        allowed = set(ff.get("core_company_allows") or []) if company else set()
        for group, terms in (ff.get("unrelated_title_terms") or {}).items():
            if group in allowed:
                continue                             # e.g. finance at a hotel / real-estate company
            hit = find_terms(title, terms, first_only=True)
            if hit:
                if group == "banking_markets" and self._stretch_banking(p, ev):
                    return
                ev.fail("field", f"{group.replace('_', ' ')} ('{hit[0].term}'): {title}")
                return
        kept = next((f for f, terms in (ff.get("families") or {}).items()
                     if find_terms(title, terms, first_only=True)), None)
        corporate = find_terms(title, (ff.get("core_company_generic_titles") or []) +
                               (ff.get("domain_role_terms") or []), first_only=True)
        if title_domain and (kept or corporate):
            return          # "Hotel Asset Management Analyst": your field AND an office role
        # Nov 9: a hotel / real-estate word alone ("Laundry Attendant - Loews Hotels",
        # "Real Estate Agent") is not enough - the role itself still has to be one of yours
        if company and find_terms(title, ff.get("core_company_generic_titles") or [], first_only=True) \
                and not any(find_terms(title, t, first_only=True) for t in (ff.get("families_off") or {}).values()):
            return                                   # a general role (Analyst, Associate...) at such a company
        if kept:
            return
        data = find_terms(title, ff.get("data_title_terms") or [], first_only=True)
        ev.fail("field", f"{'pure data role' if data else 'not one of your fields'}: {title}")

    def _stretch_banking(self, p: Posting, ev: Evaluation) -> bool:
        """Banking work on real assets (real estate, infrastructure, hospitality / franchise
        securitizations) is next to your real-estate-finance concentration: kept as a stretch.
        True = keep (flagged). With no description yet, a financing-type title waits for it."""
        sb = self.c.get("stretch_banking", default=None)
        if not sb or sb.get("enabled") is False or find_terms(p.title, sb.get("never_titles") or [], first_only=True):
            return False
        if not p.description_lines:
            # title-only first pass: read the description before deciding
            return bool(find_terms(p.title, sb.get("title_hints") or [], first_only=True))
        assets = {h.term.lower() for h in find_terms(p.description_lines, sb.get("real_asset_terms") or [])}
        if len(assets) >= int(sb.get("min_terms", 2)):
            ev.flag("Stretch: banking × real assets")
            return True
        return False

    def title_language(self, p: Posting, ev: Evaluation) -> None:
        """'Strategist (Spanish speaking)': the title itself asks for a language you don't list."""
        mine = {str(x).lower() for x in self.c.get("candidate", "languages", default=["English"]) or []}
        others = [x for x in self.c.get("other_languages", default=[]) or [] if str(x).lower() not in mine]
        hit = find_terms(p.title, others, first_only=True)
        if hit and re.search(r"speak|speaker|bilingual|fluent|native|language", p.title, re.I):
            ev.fail("credentials", f"requires {hit[0].term}: {p.title}")

    def edge(self, p: Posting, ev: Evaluation) -> None:
        """Your edge: AI applied to hospitality or real estate. Flagged and listed first."""
        ff = self.c.get("field_fit", default={}) or {}
        terms = ff.get("ai_terms") or []
        if not terms:
            return
        domain = self._title_domain(p) or (self._company_domain(p) if p.description_lines else None)
        intro = [p.title] + p.description_lines[: int(ff.get("core_from_description_lines", 12))]
        ai = bool(find_terms(intro, terms, first_only=True))
        label = ("hospitality" if domain.startswith("hospitality") else "real estate") if domain else ""
        # ranking (your words, Nov 8): hospitality / real estate preferred, an AI-forward
        # company preferred; both together first
        bank_title = find_terms(p.title, (ff.get("unrelated_title_terms") or {}).get("banking_markets") or [],
                                first_only=True)
        if domain and bank_title:
            # real-estate banking (CRE finance, ECM real estate): a reach, not a preferred-field fit
            if "Stretch: banking × real assets" not in ev.flags:
                ev.flag("Stretch: banking × real assets")
        elif domain and ai:
            ev.flag(f"Your edge: AI × {label}")
        elif domain:
            ev.flag(f"Preferred field: {label}")
        elif ai:
            ev.flag("AI-forward company")

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
            keep = sal.get("unknown_keep_flags") or []
            if (sal.get("drop_unknown") and p.description_lines          # never on the title-only first pass
                    and not any(f.startswith(k) for f in ev.flags for k in keep)
                    and not PROGRAM_RX.search(p.title or "")):
                ev.fail("salary", "no salary posted, and not a grad program or an AI-forward company")
                return
            ev.flag("Salary unknown")
            return
        markets = self.c.get("salary", "markets", default={}) or {}
        market = {"USD": "US", "CAD": "CA"}.get(comp.currency or "", p.location.market or "US")
        m = markets.get(market) or {}
        mid = comp.midpoint_annual
        floor = m.get("floor", 0)
        if m.get("program_floor") and PROGRAM_RX.search(p.title or ""):
            floor = m["program_floor"]              # grad programs: lower pay minimum (your choice, Nov 8)
        if mid < floor:
            ev.fail("salary", f"midpoint {comp.currency or ''} {mid:,.0f} below floor {floor:,}: "
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
            return
        exp = self.c.get("experience", default={}) or {}
        cap = exp.get("senior_pay_midpoint")
        mid = p.comp.midpoint_annual if p.comp.known and p.comp.period in (None, "year") else None
        if cap and mid and mid > cap and not p.experience.clean_terms \
                and not any(a.years < 2 for a in p.experience.asks) \
                and not find_terms(p.title, exp.get("entry_title_cues") or [], first_only=True) \
                and not PROGRAM_RX.search(p.title or ""):
            # Nov 9 (Anthropic GTM S&O, $190-270K, "track record... presented to senior executives"):
            # pay this high with no entry-level wording means an experienced hire
            ev.fail("experience", f"pays {p.comp.display()} with no entry-level wording: an experienced hire")
            return
        if not p.experience.asks and not p.experience.clean_terms:
            mid = find_terms(p.title, exp.get("no_years_mid_level_titles") or [], first_only=True)
            if mid and not find_terms(p.title, exp.get("entry_title_cues") or [], first_only=True):
                ev.fail("experience", f"no years stated and '{mid[0].term}' is a mid-level title: {p.title}")

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
        if not (find_terms(p.title, tech.get("keep_titles", []) or [], first_only=True)
                or qualifications.is_ai_builder(p.title, self.c)):
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
