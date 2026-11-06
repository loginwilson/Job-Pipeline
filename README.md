# Job Pipeline

Pulls postings from public company job boards (Greenhouse, Lever, Ashby), applies
`config/criteria.yaml`, and (from step 2 on) keeps a Google Sheet updated.
Spec: `docs/BUILD_BRIEF.md`, `docs/ARCHITECTURE.md`, `config/criteria.yaml` (criteria wins).

**Status: steps 1–2 done** — sources → normalize → gates → requirements match → SQLite
(`data/pipeline.db`) → Google Sheet `Pipeline` tab. Signals/tiers, the other tabs and the
hourly workflow come next; full setup steps for the owner are added in step 5.

Until step 4, the Sheet's Tier, Role type and Why columns are blank and Score shows the
requirements-match score.

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

The latest Palantir results are in `docs/palantir_step1_results.txt`.
