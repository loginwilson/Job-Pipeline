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


# 2. location
def test_location(evaluate):
    assert "location" in gates_of(evaluate(locations=["Stamford, CT"], lines=[PAY_OK]))
    assert "location" in gates_of(evaluate(locations=["Richmond, VA"], lines=[PAY_OK]))
    assert "location" in gates_of(evaluate(workplace="remote", lines=[PAY_OK]))
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


# 5. seniority
def test_seniority(evaluate):
    for title in ("Senior Analyst", "Sr. Associate", "Head of Growth", "VP, Partnerships", "Director of Ops",
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
    for title in ("Operations Associate - Join Our Talent Community", "General Application",
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
