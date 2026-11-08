"""The nine must-haves (criteria.yaml v2)."""
from conftest import PAY_OK


def gates_of(ev):
    return {f.gate for f in ev.failures}


def test_clean_posting_passes(evaluate):
    ev = evaluate(lines=[PAY_OK])
    assert ev.passed, ev.failures
    assert ev.visa.startswith("OPT year 1")


# 1. employment type
def test_employment_type(evaluate):
    assert "employment_type" in gates_of(evaluate(employment="Internship", lines=[PAY_OK]))
    assert "employment_type" in gates_of(evaluate(title="Analyst, Internship", lines=[PAY_OK]))
    assert "employment_type" in gates_of(evaluate(employment="Contract", lines=[PAY_OK]))
    assert "employment_type" in gates_of(evaluate(employment="Part-time", lines=[PAY_OK]))
    assert "employment_type" in gates_of(evaluate(title="Member Service Rep (Part-Time)", employment=None,
                                                  lines=[PAY_OK]))
    short = evaluate(title="Builders Fellowship", employment=None, lines=[PAY_OK, "The fellowship runs for 12 weeks."])
    assert "employment_type" in gates_of(short)
    assert evaluate(title="Contract Operations", lines=[PAY_OK]).passed   # not a contract job
    assert evaluate(title="Contract Manager", lines=[PAY_OK]).passed      # manages contracts
    assert "employment_type" in gates_of(evaluate(title="Contract Recruiter, GTM and Ops", lines=[PAY_OK]))


# 2. location
def test_location(evaluate):
    assert "location" in gates_of(evaluate(locations=["Stamford, CT"], lines=[PAY_OK]))
    assert "location" in gates_of(evaluate(locations=["Richmond, VA"], lines=[PAY_OK]))
    assert "location" in gates_of(evaluate(locations=["Remote"], workplace="remote", lines=[PAY_OK]))
    assert "location" in gates_of(evaluate(locations=["Remote - US"], workplace=None, lines=[PAY_OK]))
    nyc_remote = evaluate(locations=["Remote, New York City, NY"], workplace="remote", lines=[PAY_OK])
    assert nyc_remote.passed and "Remote role — lists your city" in nyc_remote.flags   # not solely remote
    multi = evaluate(locations=["NYC, SF, or Remote"], workplace=None, lines=[PAY_OK])
    assert multi.passed and multi.posting.location.best.region == "NYC"
    assert evaluate(locations=["Austin, TX"], lines=[PAY_OK]).passed       # tier 3 still passes


# 3. salary
def test_salary(evaluate):
    assert evaluate(lines=["Salary: $120,000 - $130,000/year"]).passed
    low = evaluate(lines=["Salary: $100,000 - $110,000/year"])
    assert low.passed and "Low base — check incentives" in low.flags
    assert "salary" in gates_of(evaluate(lines=["Salary: $80,000 - $110,000/year. Generous equity."]))
    unknown = evaluate(lines=["Great benefits."])
    assert unknown.passed and "Salary unknown" in unknown.flags
    ote = evaluate(lines=["OTE: $150,000 - $200,000 with uncapped commission."])
    assert ote.passed and "Salary unknown (OTE only)" in ote.flags
    ca = evaluate(locations=["Toronto, ON"], lines=["Salary $125,000 - $135,000 per year"])
    assert ca.passed and "Low base — check incentives" in ca.flags     # C$ between 120K and 140K


def test_salary_rarely_meets_floor(evaluate):
    for title in ("Retail Sales Associate, Full Time", "Warehouse Associate", "Cashier", "Customer Service Representative"):
        assert "salary" in gates_of(evaluate(title=title, lines=["Great benefits."])), title
    # a posted salary that meets the floor always wins
    assert evaluate(title="Customer Service Representative", lines=["Salary: $120,000 - $130,000/year"]).passed
    # plain associate / sales associate are real options
    for title in ("Associate, CFO Services", "Sales Associate, Apartments.com"):
        assert evaluate(title=title, lines=["Great benefits."]).passed, title


# 4. experience
def test_experience(evaluate):
    assert "experience" in gates_of(evaluate(lines=["## Requirements", "- 3+ years of experience in sales", PAY_OK]))
    assert "experience" in gates_of(evaluate(lines=["## Nice to have", "- 3-5 years of experience", PAY_OK]))
    assert evaluate(lines=["## Requirements", "- 2–4 years of experience in operations", PAY_OK]).passed
    assert "experience" in gates_of(evaluate(lines=["## Requirements", "- At least three years of experience", PAY_OK]))


def test_experience_misreads(evaluate):
    assert evaluate(lines=["## Requirements", "- Must be 18 years of age or older.", PAY_OK]).passed
    alt = ("- Bachelor's degree and 1+ years of recruiting experience; or associate degree and 2+ years "
           "of experience; or 3+ years of professional experience")
    ev = evaluate(lines=["## Requirements", alt, PAY_OK])
    assert ev.passed and ev.posting.experience.max_years("required") == 1


# 5. seniority
def test_seniority(evaluate):
    for title in ("Senior Analyst", "Sr. Associate", "Head of Growth", "VP, Partnerships", "Director of Ops", "Site Leader",
                  "Lead Analyst", "Team Lead, Operations", "Analyst III", "Senior Manager, Strategy"):
        assert "seniority" in gates_of(evaluate(title=title, lines=[PAY_OK])), title
    for title in ("Account Executive", "Product Manager, Growth", "Lead Generation Specialist",
                  "Onboarding Lead", "Chief of Staff", "Analyst II", "Associate Consultant",
                  "Product Designer, New Grad - 2027"):
        assert "seniority" not in gates_of(evaluate(title=title, lines=[PAY_OK])), title
    assert "seniority" in gates_of(evaluate(title="Operations Manager",
                                            lines=[PAY_OK, "You will manage a team of five."]))


# 6. work authorization
def test_work_authorization(evaluate):
    for line in ("U.S. Person Required", "Must be able to obtain a security clearance.",
                 "Active U.S. DoD Top Secret clearance", "Must be a U.S. citizen"):
        assert "work_authorization" in gates_of(evaluate(lines=[PAY_OK, line])), line
    nosp = evaluate(lines=[PAY_OK, "We are unable to sponsor visas."])
    assert nosp.passed and "No H-1B sponsorship — OPT/TN likely fine" in nosp.flags
    canada = evaluate(locations=["Toronto, ON"], lines=["Salary $150,000 - $170,000", "U.S. Person Required"])
    assert canada.passed and canada.visa == "No visa needed (Canada)"


# 7. technical
def test_technical(evaluate):
    for title in ("Software Engineer", "Forward Deployed Engineer", "Data Scientist", "Solutions Architect",
                  "Frontend Developer"):
        assert "technical" in gates_of(evaluate(title=title, lines=[PAY_OK])), title
    for title in ("GTM Engineer", "Solutions Engineer", "Sales Engineer", "Prompt Engineer"):
        assert evaluate(title=title, lines=[PAY_OK]).passed, title
    req = ["## Requirements"]
    assert "technical" in gates_of(evaluate(lines=[PAY_OK] + req + ["- Proficiency in Python"]))
    assert "technical" in gates_of(evaluate(title="GTM Engineer", lines=[PAY_OK] + req + ["- Experience writing production code"]))
    # a non-coding alternative on the same line keeps it
    assert evaluate(lines=[PAY_OK] + req + [
        "- Experience with programming, scripting or statistical packages (e.g. Python, R, Matlab, SQL)"]).passed
    assert evaluate(lines=[PAY_OK, "## Nice to have", "- Python"]).passed          # preferred only
    assert evaluate(lines=[PAY_OK] + req + ["- Familiarity with APIs and AI tools"]).passed


# 8. credentials
def test_credentials(evaluate):
    req = [PAY_OK, "## Requirements"]
    for line in ("- JD and active bar membership", "- MBA required", "- CPA license",
                 "- Bachelor's degree in Computer Science or related field"):
        assert "credentials" in gates_of(evaluate(lines=req + [line])), line
    for line in ("- Bachelor's degree required; MBA preferred",
                 "- Bachelor's degree in Business, Economics, Computer Science or related field",
                 "- Degree in Engineering or equivalent experience",
                 "- Undergraduate degree, with a preference for JD"):
        assert "credentials" not in gates_of(evaluate(lines=req + [line])), line


# 9. start date
def test_start_date(evaluate):
    assert "start_date" in gates_of(evaluate(lines=[PAY_OK, "Start date: January 2027"]))
    early = evaluate(lines=[PAY_OK, "## Requirements", "- Must have graduated between May 2025 and June 2026"])
    assert "start_date" in gates_of(early)
    assert evaluate(lines=[PAY_OK, "## Requirements", "- Must be graduating in Spring 2027"]).passed
    late = evaluate(lines=[PAY_OK, "Program start date: September 2027"])
    assert late.passed and "Starts after Aug 1 — check OPT timing" in late.flags
    assert "Starts after Aug 1 — check OPT timing" not in evaluate(lines=[PAY_OK, "Start date: June 2027"]).flags


# 10. a real opening
def test_real_opening(evaluate):
    for title in ("Wildcard", "Wild Card", "Don’t See a Perfect Role? Apply Anyway!", "Former Founder",
                  "Operations Associate - Join Our Talent Community", "General Application",
                  "Future Opportunities: Analyst", "Expression of Interest - Strategy"):
        assert "not_a_real_opening" in {f.gate for f in evaluate(title=title, lines=[PAY_OK]).failures}, title
    assert evaluate(title="Operations Associate", lines=[PAY_OK]).passed


# 11. related to the degree (OPT)
def test_opt_relatedness(evaluate):
    for title in ("Registered Nurse", "Corporate Counsel", "Associate Attorney", "Clinical Research Coordinator",
                  "Math Teacher", "HVAC Technician"):
        assert "not_related_to_degree" in {f.gate for f in evaluate(title=title, lines=[PAY_OK]).failures}, title
    for title in ("Hotel Operations Associate", "Real Estate Analyst", "Account Executive", "Deployment Strategist",
                  "Counselor Recruitment Coordinator, Hospitality"):
        assert "not_related_to_degree" not in {f.gate for f in evaluate(title=title, lines=[PAY_OK]).failures}, title


def test_field_column(evaluate):
    assert evaluate(title="Acquisitions Associate, Commercial Real Estate", lines=[PAY_OK]).field == "Real Estate / Proptech"
    assert evaluate(title="Hotel Operations Associate", lines=[PAY_OK]).field == "Hospitality / Travel"
    assert evaluate(title="Office Coordinator", lines=[PAY_OK]).field == "Other"


def _p(criteria, title, lines, pay="The base salary range is $120,000 - $130,000."):
    from src.config import Company
    from src.pipeline.locations import LocationMatcher
    from src.pipeline.normalize import normalize_posting
    from src.sources.base import RawPosting
    raw = RawPosting(platform="lever", slug="x", native_id="1", title=title, url="https://x/1",
                     locations=["New York, NY"], workplace_type="hybrid", employment_type="Full-time",
                     description_lines=lines + [pay])
    return normalize_posting(raw, Company("X", "lever", "x"), criteria, LocationMatcher(criteria))


def test_unstated_senior_experience_drops(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    ev = g.evaluate(_p(criteria, "Marketing Associate", ["## Requirements", "- Extensive experience in agile product ownership"]))
    assert not ev.passed and ev.failures[0].gate == "experience"
    ok = g.evaluate(_p(criteria, "Research Assistant", ["## Requirements", "- Substantial hands-on hardware experience from labs"]))
    assert "experience" not in [f.gate for f in ok.failures]


def test_description_naming_a_senior_title(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    ev = g.evaluate(_p(criteria, "Experience Owner", ["The Sr Marketing Experience Owner is self-motivated."]))
    assert "seniority" in [f.gate for f in ev.failures]
    ok = g.evaluate(_p(criteria, "Financial Analyst", ["You will report to the Senior Financial Analyst."]))
    assert "seniority" not in [f.gate for f in ok.failures]


def test_manager_comma_only_at_title_start(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    assert "seniority" in [f.gate for f in g.evaluate(_p(criteria, "Manager, Strategy", [])).failures]
    for t in ("Program Manager, Product", "Growth Marketing Manager, Events"):
        assert "seniority" not in [f.gate for f in g.evaluate(_p(criteria, t, [])).failures]


def test_negated_requirement_does_not_drop(criteria):
    from src.pipeline.gates import Gates
    lines = ["## Requirements", "- You should work closely with engineers, but prior software engineering experience is not required."]
    ev = Gates(criteria).evaluate(_p(criteria, "Product Manager", lines))
    assert "technical" not in [f.gate for f in ev.failures]
    lines = ["## Requirements", "- You don't need a law degree to succeed here."]
    ev = Gates(criteria).evaluate(_p(criteria, "Operations Associate", lines))
    assert "credentials" not in [f.gate for f in ev.failures]


def test_title_level_credential_and_start_terms(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    fails = lambda t: {f.gate for f in g.evaluate(_p(criteria, t, [])).failures}
    assert "credentials" in fails("Quantitative Researcher, PhD")
    assert "seniority" in fails("Experienced/Lateral - Trading Assistant")
    assert "seniority" in fails("Technician 3, Manufacturing Equipment Maintenance")
    assert "start_date" in fails("Graduate Leadership Program - Media Marketing (January Start)")
    assert "not_a_real_opening" in fails("Interested in future People & Culture opportunities?")
    assert not fails("Graduate Leadership Program - Media Marketing (June Start)")
    assert not fails("Recent Graduate - Trading Assistant (NYC)")


def test_title_abbreviations(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    fails = lambda t, pay="Salary: $120,000 - $130,000/year": {f.gate for f in g.evaluate(_p(criteria, t, [], pay=pay)).failures}
    assert "seniority" in fails("FVP, Product Management - Borrow")
    assert "seniority" in fails("SMTS Device Modeling & Characterization")
    assert "technical" in fails("Eng., Distrib Tech")
    assert "salary" in fails("Closer (Residential Mortgage)", pay="")
    assert not fails("English Content Associate")


def test_bank_internships_and_manager_levels(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    fails = lambda t: {f.gate for f in g.evaluate(_p(criteria, t, [])).failures}
    assert "employment_type" in fails("2027 Markets Summer Analyst Program - Research")
    assert "seniority" in fails("Manager-Data Science (New York, NY)")
    assert "seniority" in fails("Managers, Data Analytics")
    assert not fails("Summer 2027 - Associate Product Manager (New Grad)")


def test_ambiguous_city_names_need_their_region(criteria):
    from src.pipeline.locations import LocationMatcher
    m = LocationMatcher(criteria)
    city = lambda s, country=None: [o.city for o in m.match([s], country).offices]
    assert city("Richmond") == [] and city("US-VA-Richmond") == [] and city("Surrey, United Kingdom") == []
    assert city("Vancouver, WA") == [] and city("Oakland") == []
    assert city("Surrey, BC") == ["Surrey"] and city("CA-BC-Vancouver") == ["Vancouver"]
    assert city("Vancouver", "CA") == ["Vancouver"] and city("Oakland, CA") == ["Oakland"]
    assert city("Toronto") == ["Toronto"] and city("New York, NY") == ["NYC"]


def test_store_roles_without_pay(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    store = g.evaluate(_p(criteria, "Full Time - Sales Associate - Hardware", ["Help customers on the sales floor."], pay=""))
    assert "salary" in {f.gate for f in store.failures}
    saas = g.evaluate(_p(criteria, "Sales Associate", ["Sell our software to retail brands."], pay=""))
    assert "salary" not in {f.gate for f in saas.failures}


def test_low_pay_title_with_posted_pay_is_read_in_full(criteria):
    from src.config import Company
    from src.pipeline.gates import Gates
    from src.pipeline.locations import LocationMatcher
    from src.run import evaluate_one
    from src.sources.base import RawPosting
    raw = RawPosting(platform="lever", slug="x", native_id="1", title="Special Inspector", url="u",
                     locations=["New York, NY"], workplace_type="onsite", employment_type="Full-time",
                     description_lines=["Pay: $60.00 - $70.00 per hour"])
    ev = evaluate_one(raw, Company("X", "lever", "x"), criteria, Gates(criteria), LocationMatcher(criteria))
    assert "salary" not in {f.gate for f in ev.failures}          # $60-70/hr ≈ $135K meets the floor
