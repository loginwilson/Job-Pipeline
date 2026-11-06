# Job Pipeline

Pulls postings from public company job boards (Greenhouse, Lever, Ashby), applies
`config/criteria.yaml`, and (from step 2 on) keeps a Google Sheet updated.
Spec: `docs/BUILD_BRIEF.md`, `docs/ARCHITECTURE.md`, `config/criteria.yaml` (criteria wins).

**Status:** sources → normalize → drop rules → requirements breakdown → SQLite
(`data/pipeline.db`) → Google Sheet, every hour via GitHub Actions. The alert-email
reader (step 3b) is built once sample alert emails exist.

## How it works — the pipeline gives you options, you decide

* **Drop rules are strict.** Experience cap, hard requirement gaps, location, salary,
  work authorization, employment type, seniority, timing (all in `config/criteria.yaml`).
  A posting that fails one goes to the **Dropped** tab with the rule and the exact text
  that triggered it — check it now and then for rules that are too strict.
* **Everything else lands in the Pipeline tab as facts**: requirements breakdown
  (`Met 2/3 · Stretch: SQL · Gap: 2+ yrs`), TN fit, salary, extra comp, experience ask,
  role type, flags (New, No gaps, Salary unknown, Sponsorship unknown, Closed, …).
  There is no score; you decide.
* **Decision column: Apply / Wait / Veto.** Undecided postings sort to the top (newest
  first), then Apply, Wait, Veto; closed postings sink to the bottom. Status (Drafting →
  Applied → Interviewing → Offer / Rejected / Withdrawn), Effort and Notes are yours too.
  The pipeline never overwrites your columns and never deletes a row you've touched.

## Your Sheet — one-time setup (≈15 min)

1. **Google Cloud project + service account**
   - Go to <https://console.cloud.google.com/>, create a project (any name, e.g. "job-pipeline").
   - *APIs & Services → Library* → enable **Google Sheets API**.
   - *IAM & Admin → Service Accounts → Create service account* (any name). No roles needed.
   - Open it → *Keys → Add key → Create new key → JSON*. A `.json` file downloads. Keep it private.
2. **The Sheet**
   - Create a blank Google Sheet (sheets.new) — name it e.g. "Job Pipeline". This is where you'll work.
   - *Share* it with the service account's email (looks like `name@project.iam.gserviceaccount.com`) as **Editor**.
   - Copy the Sheet ID from its URL: `https://docs.google.com/spreadsheets/d/`**`<SHEET_ID>`**`/edit`.
3. **GitHub secrets** — repo → *Settings → Secrets and variables → Actions → New repository secret*:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the whole contents of the JSON key file
   - `SHEET_ID` — the ID from step 2
   - `ALERTS_IMAP_USER`, `ALERTS_IMAP_APP_PASSWORD` — later, for job-alert emails (`docs/ALERTS_SETUP.md`)
4. **First run** — repo → *Actions → hourly pipeline → Run workflow*. When it finishes
   (≈1–2 min), the Sheet has five tabs: Pipeline, Dropped, Unparsed alerts, Funnel,
   Board health. After that it runs every hour at :17.
5. **Add a company** — edit `config/watchlist.yaml`: `name`, `platform`
   (greenhouse / lever / ashby) and `slug` from the board URL
   (`jobs.lever.co/<slug>`, `boards.greenhouse.io/<slug>`, `jobs.ashbyhq.com/<slug>`).
   Check it once with `python -m src.run --print --company "<name>"`.

Open the Sheet from Google Drive or the Google Sheets app on your phone — bookmark it.

**Actions minutes:** hourly runs on a private repo use GitHub Actions minutes (about
1–2 min per run). If they run short, run the same `python -m src.run` on a small
always-on server or lower the schedule in `.github/workflows/hourly.yml`.

## Run

```bash
pip install -r requirements.txt
python -m src.run --print --company Palantir                 # live board
python -m src.run --print --company Palantir --fixture tests/fixtures/lever_palantir.json.gz
python -m src.run --fake-sheet data/fake_sheet.json            # full run, local stand-in Sheet
SHEET_ID=... GOOGLE_SERVICE_ACCOUNT_JSON="$(cat key.json)" python -m src.run   # real Sheet, from your laptop
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

The ranking code (`src/pipeline/signals.py`, `tiers.py`) is kept but unused; only role-type
labels come from it. The latest Palantir results are in `docs/palantir_results.txt`.
