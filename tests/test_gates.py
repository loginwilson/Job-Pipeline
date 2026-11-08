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
    # 0–1 years only since Nov 8: a required 2 drops, a "1–2 years" range (minimum 1) passes
    assert "experience" in gates_of(evaluate(lines=["## Requirements", "- 2–4 years of experience in operations", PAY_OK]))
    assert evaluate(lines=["## Requirements", "- 1–2 years of experience in operations", PAY_OK]).passed
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
    # sales / solutions / GTM engineers need a technical background: dropped since the Nov 8 audit
    for title in ("GTM Engineer", "Solutions Engineer", "Sales Engineer", "Prompt Engineer"):
        assert "technical" in gates_of(evaluate(title=title, lines=[PAY_OK])), title
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


def test_audit_round4_titles(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    fails = lambda t: {f.gate for f in g.evaluate(_p(criteria, t, [])).failures}
    assert "seniority" in fails("Assoc Partner, Consult Partner - NY State, Agencies")
    assert "seniority" in fails("Vice Dean of Finance and Administration")
    assert "seniority" in fails("Client Service Analyst IV – Bilingual English/Portuguese")
    assert "employment_type" in fails("Operations Underwriting Associate(12 month contract)")
    assert "employment_type" in fails("Patient Care Assistant, Per Diem")
    assert "not_related_to_degree" in fails("RN Case Manager")
    assert "technical" in fails("Computational Scientist, Structural & Thermal")
    assert "technical" in fails("Ingénieur(e) mécanique turbine-alternateur")
    assert "technical" in fails("GTM Engineer") and "technical" in fails("Graduate Sales Engineer")


def test_audit_round5_titles(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    fails = lambda t, pay="": {f.gate for f in g.evaluate(_p(criteria, t, [], pay=pay)).failures}
    assert "not_related_to_degree" in fails("Certified Surgical Technologist I - 4th Floor")
    assert "not_related_to_degree" in fails("Advanced Practice Provider, Complex Gyn")
    assert "salary" in fails("Food Service Worker") and "salary" in fails("Athletic Aide I")
    assert "salary" not in fails("Finance Operator", pay="Salary: $120,000 - $140,000/year")


def test_audit_round6_assistants_and_drivers(criteria):
    from src.pipeline.gates import Gates
    g = Gates(criteria)
    fails = lambda t, pay="": {f.gate for f in g.evaluate(_p(criteria, t, [], pay=pay)).failures}
    for t in ("Operations Assistant", "Assistant Account Executive", "Driver (Dallas-Fort Worth)", "Research Assistant"):
        assert "salary" in fails(t), t
    assert "not_related_to_degree" in fails("Pathologists' Assistant")
    assert "salary" not in fails("Executive Assistant") and "salary" not in fails("Trading Assistant")
    assert not fails("Research Assistant", pay="Salary: $110,000 - $130,000/year")   # posted pay wins


def test_field_fit_keeps_your_fields_only(full_criteria):
    from src.pipeline.gates import Gates
    g = Gates(full_criteria)
    fails = lambda t, lines=(): {f.gate for f in g.evaluate(_p(full_criteria, t, list(lines))).failures}
    for t in ("Hotel Revenue Management Analyst", "Real Estate Acquisitions Analyst", "Business Operations Associate",
              "Strategy Consulting Analyst", "CMBS and Real Estate Finance Analyst", "Chief of Staff",
              "Deployment Strategist", "Revenue Operations Associate"):
        assert "field" not in fails(t), t
    # sales / marketing / consulting titles were turned off Nov 8 (still kept at a hospitality or
    # real-estate company); "Business Development Associate" is sales, not real estate development
    for t in ("Sales Development Representative", "Marketing Coordinator", "Business Development Associate"):
        assert "field" in fails(t), t
    assert "field" not in fails("Sales Development Representative",
                                ["We build AI agents for property management and apartment operators."])
    for t in ("Investment Banking Analyst", "Compliance Operations Analyst", "Sales Trader", "Tax Associate",
              "Financial Analyst", "Associate Product Manager", "Data Analyst", "Recruiting Coordinator",
              "Underwriting Associate", "Trade Support Associate"):
        assert "field" in fails(t), t
    # a hospitality company's generic title counts
    assert "field" not in fails("Analyst", ["Marriott is a global hotel company."])


def test_no_years_mid_level_titles(full_criteria):
    from src.pipeline.gates import Gates
    g = Gates(full_criteria)
    fails = lambda t, lines=(): {f.gate for f in g.evaluate(_p(full_criteria, t, list(lines))).failures}
    assert "experience" in fails("Business Operations Manager")
    assert "experience" in fails("Account Executive")
    assert "experience" not in fails("Associate Account Executive")
    assert "experience" not in fails("Account Executive", ["## Requirements", "- 1+ years of sales experience"])


def test_quantitative_degree_requirements(evaluate):
    from conftest import PAY_OK
    gates = lambda req: {f.gate for f in evaluate(lines=["## Requirements", req, PAY_OK]).failures}
    for req in ("- Bachelor’s degree (Advanced degree preferred) in a quantitative field, such as Mathematics, "
                "Finance, Economics, Statistics, Physics, or Computer Science.",
                "- Bachelor's degree in Finance, Mathematics, Statistics, or Computer Science",
                "- BS in a STEM field"):
        assert "credentials" in gates(req), req
    for req in ("- Bachelor's degree in Business, Finance, Economics, or a related field",
                "- Bachelor's degree in Hospitality, Mathematics, or Statistics",
                "- Bachelor's degree in a quantitative field preferred",
                "- Bachelor's degree in Finance or Economics"):
        assert "credentials" not in gates(req), req


def test_capitals_and_ampersand_headings_are_read(evaluate):
    """Goldman-style postings: 'SKILLS & EXPERIENCE WE'RE LOOKING FOR' in capitals, no colon."""
    from conftest import PAY_OK
    from src.pipeline.html_text import html_to_lines, plain_to_lines
    body = ("YOUR IMPACT\nWe are seeking a strategist.\nSKILLS & EXPERIENCE WE'RE LOOKING FOR\n"
            "Fluent programming in Python, Matlab, R or a related language\n0-3 years of related work experience")
    html = "".join(f"<p>{ln}</p>" for ln in body.split("\n"))
    for lines in (plain_to_lines(body), html_to_lines(html)):
        assert "## SKILLS & EXPERIENCE WE'RE LOOKING FOR" in lines
        assert "technical" in {f.gate for f in evaluate(lines=lines + [PAY_OK]).failures}
    # an ordinary all-caps acronym line is not mistaken for a heading
    assert plain_to_lines("USA") == ["USA"]


def test_what_you_need_heading_and_unlabelled_requirements(evaluate):
    from conftest import PAY_OK
    gates = lambda lines: {f.gate for f in evaluate(lines=lines + [PAY_OK]).failures}
    assert "experience" in gates(["## What You Need", "- 3+ years in operations"])
    # no recognised heading at all: bullets outside duties/benefits still count as requirements
    assert "technical" in gates(["## The Team", "- Fluent programming in Python and C++ required"])
    # duties under "What You'll Do" are not requirements
    assert "technical" not in gates(["## What You'll Do", "- Partner with engineers who write Python"])


def test_whole_posting_specialist_vocabulary(evaluate):
    from conftest import PAY_OK
    quant = ["Conduct quantitative research and implement strategy backtests.",
             "Apply machine learning and asset pricing models; Matlab a plus."]
    assert "specialist" in {f.gate for f in evaluate(lines=quant + [PAY_OK]).failures}
    ops = ["Own vendor operations and build dashboards in Excel and SQL.", "Partner with sales and finance."]
    assert "specialist" not in {f.gate for f in evaluate(lines=ops + [PAY_OK]).failures}


def test_audit_nov8_reading_rules(evaluate, full_criteria):
    """Misses found reading every passing posting on Nov 8."""
    from conftest import PAY_OK
    from src.pipeline.gates import Gates
    gates = lambda lines, **kw: {f.gate for f in evaluate(lines=lines + [PAY_OK], **kw).failures}
    # language fluency
    assert "credentials" in gates(["## Requirements", "- Fluent written and spoken English and Mandarin"])
    assert "credentials" not in gates(["## Requirements", "- Spanish fluency is a plus"])
    # "a technical background" / design skills
    assert "technical" in gates(["## Requirements", "- A technical background"])
    assert "technical" in gates(["## Requirements", "- Video editing and graphic design experience required"])
    # headings seen in the audit
    assert "experience" in gates(["## What We Are Looking For", "4+ years in Sales Operations"])
    assert "experience" in gates(["## You may be a fit if", "You have 4+ years of experience in sales"])
    # years written in Portuguese; "followed by" adds up
    assert "experience" in gates(["## Requisitos", "- 4-8 anos em facilities"])
    assert "experience" in gates(["## Requirements", "- 2+ years in investment banking, followed by 2+ years in BizOps"])
    # 'Construction Management' is not your degree
    assert "credentials" in gates(["## Requirements", "- Bachelor's degree in Civil Engineering, Construction Management, or Architecture"])
    # graduation timing
    assert "start_date" in gates([], title="Incoming GTM Associate - December 2026 Graduates")
    # a link with no description, under the full rules
    g = Gates(full_criteria)
    from tests.test_gates import _p
    assert "no_description" in {f.gate for f in g.evaluate(_p(full_criteria, "Founding Sales", ["https://icon.com/careers"], pay="")).failures}


def test_edge_ai_in_hospitality_and_real_estate(full_criteria):
    from src.pipeline.gates import Gates
    g = Gates(full_criteria)
    eliseai = ["EliseAI builds AI agents for property management, helping apartment operators and renters.",
               "## What you'll do", "- Book meetings with leasing teams"] + ["Words about the role."] * 12
    ev = g.evaluate(_p(full_criteria, "Sales Development Representative", eliseai))
    assert ev.passed and "Your edge: AI × real estate" in ev.flags, ev.failures
    hotel = ["We are an AI-powered revenue platform for hotels and resorts."] + ["Words about the role."] * 12
    ev = g.evaluate(_p(full_criteria, "Analyst", hotel))
    assert ev.passed and "Your edge: AI × hospitality" in ev.flags, ev.failures
    # finance at a real-estate company is your concentration; HR / product there still drop
    reit = ["We are a REIT that owns multifamily apartments."] + ["Words about the role."] * 12
    assert "field" not in {f.gate for f in g.evaluate(_p(full_criteria, "Financial Analyst", reit)).failures}
    assert "field" in {f.gate for f in g.evaluate(_p(full_criteria, "Recruiting Coordinator", reit)).failures}
    assert "field" in {f.gate for f in g.evaluate(_p(full_criteria, "Associate Product Manager | Housing", reit)).failures}
    # no AI: kept, but no edge flag
    ev = g.evaluate(_p(full_criteria, "Analyst", reit))
    assert not any(f.startswith("Your edge") for f in ev.flags)


def test_investment_banking_jobs_with_strategy_titles(full_criteria):
    """JPMorgan 'Strategic Financing Solutions – Associate' passed on 'strategic' in the title."""
    from src.pipeline.gates import Gates
    g = Gates(full_criteria)
    body = ["The team sits between Banking and Sales and Trading in Capital Markets.",
            "Analyze Digital Infrastructure ABS and Whole Business Securitizations.",
            "Work with rating agencies and trading desks; prior investment banking front office role."] + ["More words."] * 10
    # banking on real assets (infrastructure ABS, whole business securitizations) is a stretch:
    # kept and flagged (your choice, Nov 8)
    for title in ("Strategic Financing Solutions – Associate", "Strategy Associate"):
        ev = g.evaluate(_p(full_criteria, title, body))
        assert ev.passed and "Stretch: banking × real assets" in ev.flags, (title, ev.failures)
    # other banking still drops: no real assets, or a trading / research / wealth title
    plain = ["The team sits between Banking and Sales and Trading in Capital Markets.",
             "Analyze leveraged finance and syndicated loans for technology clients.",
             "Work with rating agencies and trading desks; prior investment banking front office role."] + ["More words."] * 10
    for title in ("Strategic Financing Solutions – Associate", "Strategy Associate"):
        assert "field" in {f.gate for f in g.evaluate(_p(full_criteria, title, plain)).failures}, title
    assert "field" in {f.gate for f in g.evaluate(_p(full_criteria, "Equity Research Associate", body)).failures}
    # the same vocabulary at a real-estate company is your concentration (CMBS / RE finance)
    re_body = ["We are a commercial real estate lender."] + body
    assert "field" not in {f.gate for f in g.evaluate(_p(full_criteria, "Strategy Associate", re_body)).failures}
