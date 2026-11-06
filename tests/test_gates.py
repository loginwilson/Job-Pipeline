from conftest import PAY_OK


def gates_of(ev):
    return {f.gate for f in ev.failures}


def test_clean_posting_passes(evaluate):
    ev = evaluate(lines=[PAY_OK])
    assert ev.passed, ev.failures


def test_employment_type(evaluate):
    assert "employment_type" in gates_of(evaluate(employment="Internship", lines=[PAY_OK]))
    assert "employment_type" in gates_of(evaluate(title="Analyst, Internship", lines=[PAY_OK]))
    assert "employment_type" in gates_of(evaluate(employment="Contract", lines=[PAY_OK]))
    short = evaluate(title="Builders Fellowship", employment=None,
                     lines=[PAY_OK, "The fellowship runs for 12 weeks."])
    assert "employment_type" in gates_of(short)
    assert evaluate(title="Contract Operations", lines=[PAY_OK]).passed  # not a contract job


def test_salary_gate(evaluate):
    assert evaluate(lines=["Salary: $140,000 - $140,000/year"]).passed
    assert "salary" in gates_of(evaluate(lines=["Salary: $120,000 - $140,000/year"]))  # no extra comp
    assert evaluate(lines=["Salary: $120,000 - $140,000/year. Plus annual bonus."]).passed
    assert "salary" in gates_of(evaluate(lines=["Salary: $100,000 - $130,000/year. RSU grant."]))
    unknown = evaluate(lines=["Great benefits."])
    assert unknown.passed and "Salary unknown" in unknown.flags


def test_seniority(evaluate):
    assert "seniority" in gates_of(evaluate(title="Senior Analyst", lines=[PAY_OK]))
    assert "seniority" in gates_of(evaluate(title="Sr. Associate", lines=[PAY_OK]))
    assert "seniority" in gates_of(evaluate(title="Head of Growth", lines=[PAY_OK]))
    assert "seniority" not in gates_of(evaluate(title="AVP Growth Partner", lines=[PAY_OK]))  # whole words only
    assert evaluate(title="Account Executive", lines=[PAY_OK]).passed       # sales title wins
    assert evaluate(title="Product Manager, Growth", lines=[PAY_OK]).passed  # keep takes precedence
    assert "seniority" in gates_of(evaluate(title="Manager, Deal Advisory", lines=[PAY_OK]))
    assert "seniority" in gates_of(evaluate(title="Manager", lines=[PAY_OK], sectors=["consulting"]))
    assert "seniority" in gates_of(evaluate(title="Operations Manager",
                                            lines=[PAY_OK, "You will manage a team of five."]))


def test_experience(evaluate):
    assert "experience" in gates_of(evaluate(lines=["## Requirements", "- 3+ years of experience in sales", PAY_OK]))
    assert "experience" in gates_of(evaluate(lines=["## Nice to have", "- 3-5 years of experience", PAY_OK]))
    ok = evaluate(lines=["## Requirements", "- 2–4 years of experience in operations", PAY_OK])
    assert ok.passed and ok.posting.experience.max_years("required") == 2
    assert evaluate(lines=["## Requirements", "- At least one year of experience", PAY_OK]).passed
    assert "experience" in gates_of(evaluate(lines=["## Requirements", "- At least three years of experience", PAY_OK]))


def test_location(evaluate):
    assert "location" in gates_of(evaluate(locations=["Stamford, CT"], lines=[PAY_OK]))
    assert "location" in gates_of(evaluate(locations=["Richmond, VA"], lines=[PAY_OK]))
    assert "work_arrangement" in gates_of(evaluate(workplace="remote", lines=[PAY_OK]))
    multi = evaluate(locations=["NYC, SF, or Remote"], workplace=None, lines=[PAY_OK])
    assert multi.passed and multi.posting.location.best.region == "NYC"


def test_work_authorization(evaluate):
    assert "work_authorization" in gates_of(evaluate(lines=[PAY_OK, "U.S. Person Required"]))
    assert "work_authorization" in gates_of(evaluate(lines=[PAY_OK, "Must be able to obtain a security clearance."]))
    nosp = evaluate(lines=[PAY_OK, "We are unable to sponsor visas."])
    assert "work_authorization" in gates_of(nosp)  # TN unlikely for a generic role
    canada = evaluate(locations=["Toronto, ON"], lines=["Salary $170,000 - $180,000", "U.S. Person Required"])
    assert canada.passed and "No visa needed" in canada.flags
    opt = evaluate(lines=[PAY_OK, "You may opt out of marketing emails."])
    assert opt.sponsorship == "unknown"  # 'opt out' is not OPT


def test_gov_defense_and_engineering(evaluate):
    assert "gov_defense" in gates_of(evaluate(title="Deployment Strategist - US Government", lines=[PAY_OK]))
    assert "gov_defense" in gates_of(evaluate(title="Analyst", team="Public Sector", lines=[PAY_OK]))
    assert "engineering_title" in gates_of(evaluate(title="Forward Deployed Engineer", lines=[PAY_OK]))
    assert "engineering_title" in gates_of(evaluate(title="Solutions Architect", lines=[PAY_OK]))
    assert "gov_defense" in gates_of(evaluate(lines=[PAY_OK], defense_primary=True))


def test_technical_and_hard_gap(evaluate):
    assert "technical" in gates_of(evaluate(lines=[PAY_OK, "You will write production code daily."]))
    hg = evaluate(lines=[PAY_OK, "## Requirements", "- Bachelor's degree in Computer Science"])
    assert "hard_gap" in gates_of(hg)
    ok = evaluate(lines=[PAY_OK, "## Requirements", "- Bachelor's degree in Business, Economics, Computer Science or related"])
    assert "hard_gap" not in gates_of(ok)


def test_timing(evaluate):
    early = evaluate(lines=[PAY_OK, "## Requirements", "- Must have graduated between May 2025 and June 2026"])
    assert "timing" in gates_of(early)
    jan = evaluate(lines=[PAY_OK, "Start date: January 2027", "Bachelor's degree required"])
    assert "timing" in gates_of(jan)


def test_company(evaluate):
    assert "company" in gates_of(evaluate(lines=[PAY_OK], headcount=5))
