# Job Pipeline

Pulls postings from public company job boards (Greenhouse, Lever, Ashby), applies
`config/criteria.yaml`, and (from step 2 on) keeps a Google Sheet updated.
Spec: `docs/BUILD_BRIEF.md`, `docs/ARCHITECTURE.md`, `config/criteria.yaml` (criteria wins).

**Status: steps 1, 2 and 4 done** — sources → normalize → gates → requirements match →
signals → tiers → SQLite (`data/pipeline.db`) → Google Sheet (Pipeline, Dropped, Unparsed
alerts, Funnel, Board health). Next: hourly workflow + owner setup (step 5); alert-email
reader once sample emails exist (step 3b).

## How ranking works — necessities vs. preferences

The pipeline gives you **options**; you decide what stays.

* **Necessities are strict.** Experience cap, requirements (hard gaps), location,
  salary, work authorization, employment type, seniority, timing → fail = dropped
  (see the Dropped tab, with the rule and the matched text).
* **Fit score → Tier.** Requirements match (70%), experience fit (15%) and necessity
  signals (15%: location tier, timing, salary, sponsorship/TN, technical asks) give the
  Score column and the Tier (Strong ≥ 70 with no required gaps, Worth a look ≥ 50,
  Stored otherwise). Stored postings still appear in the Sheet, just lower.
* **Preferences only reorder.** Role type, lane, description signals, sector, company,
  and `final_score.role_preference` (Sales −10, Sales-adjacent −5, analyst titles −10)
  sort postings *within* their tier. They never change a tier or hide a posting.
  Role type is a column you can filter on.

## Run

```bash
pip install -r requirements.txt
python -m src.run --print --company Palantir                 # live board
python -m src.run --print --company Palantir --fixture tests/fixtures/lever_palantir.json.gz
python -m src.run --fake-sheet data/fake_sheet.json            # full run, local stand-in Sheet
SHEET_ID=... GOOGLE_SERVICE_ACCOUNT_JSON="$(cat key.json)" python -m src.run   # real Sheet
python -m pytest
```

## Config files

| File | What it is |
|---|---|
| `config/criteria.yaml` | The rules. Every threshold, weight and term list is read from here. |
| `config/watchlist.yaml` | Companies polled — **you choose them**; the pipeline never picks companies, it judges each posting against the criteria. Optional `sectors` tags only matter for the rules that need them (see the file header). |
| `config/reference.yaml` | Not rules: place-name reference data (NYC = "New York", state codes so "Richmond, VA" isn't Richmond BC) and the fuzzy-match score. |

## Matching conventions

Term lists match whole words, case-insensitively (all-caps acronyms like `OPT`, `ITAR`
match case-sensitively), hyphens = spaces, `U.S.` = `US`, singular/plural tolerant.
Every gate is evaluated, so a dropped posting lists all the rules it failed.

The latest Palantir results are in `docs/palantir_results.txt`.
