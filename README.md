# Job Pipeline

Collects every public job posting it can find in your cities, drops the ones that fail your
must-haves (`config/criteria.yaml`), and keeps a Google Sheet updated where you decide what
to do with each one. Runs on GitHub Actions twice an hour (`.github/workflows/hourly.yml`).

## Where postings come from (`config/sources.yaml`)

| Source | What it is | How often |
|---|---|---|
| **Company job boards** | Greenhouse, Lever, Ashby, SmartRecruiters and Workday boards — the ones in `config/watchlist.yaml` plus every board discovered automatically | Every run; Workday sites and boards with nothing passing for 3 days every 6 hours |
| **JSearch** (Google for Jobs) | LinkedIn, Indeed, Glassdoor and company sites, searched by keyword group × city | Within the daily request budget (`requests_per_day`; free plan = 6/day). Needs the `JSEARCH_API_KEY` secret |
| **The Muse** | Entry-level postings in all your cities (≤30 days old) | Daily |
| **Hacker News "Who is hiring?"** | Discovery only: job-board links in posts that mention your cities | Daily |

**Auto-discovery.** Every company these sources surface in your cities is checked for its own
board (apply links first, then name lookups, confirmed by the same job being listed there).
Found boards join the watch and show in the Board health tab. The same job from several
sources appears once — the company-board copy wins and keeps any decision you already made.

**Active only.** Board postings show while they're in the company's live feed; search
postings must be recent and, when we watch that company's board, listed on it. Closed
postings you never touched leave To review.

## How it works — every option comes in, you decide

The pipeline collects as many postings as it can and drops only the ones you
couldn't or wouldn't take. **No scoring, no ranking by preference.**

**Must-haves** (`config/criteria.yaml` — a posting fails any one → Dropped tab, with the
rule and the exact text that triggered it):

| # | Dropped if… |
|---|---|
| 1 | not full-time (internship, contract, temp, co-op, short fellowship, part-time) |
| 2 | not in NYC · Chicago / Florida / California / Toronto · Texas / BC, or **solely** remote |
| 3 | salary midpoint under $100K USD / C$120K (between that and $120K / C$140K → kept, flagged *Low base — check incentives*). No salary posted → kept and flagged, unless it's a role that essentially never pays the floor (retail, warehouse, driver, call center…) or hourly with no amount |
| 4 | asks 3+ years of experience (required or preferred) |
| 5 | senior title (Senior, Sr, Staff, Principal, Director, Head of, VP, Chief, Level III+, Lead-as-a-level, people manager) |
| 6 | US job requiring US citizenship, security clearance or U.S.-person status ("no sponsorship" is only flagged — OPT/TN usually fine) |
| 7 | engineer/developer title (except GTM, Solutions, Sales, Prompt Engineer), or a **required** line demanding coding with no non-coding alternative |
| 8 | a **required** credential you lack (MBA, JD, Master's, PhD, CPA, bar, RN, PE, CS/engineering-type degree) |
| 9 | starts before you graduate (May 2027); starts after Aug 1, 2027 are flagged for OPT timing |
| 10 | not a real opening ("talent community", "general application", …), or no longer active |
| 11 | a field your degree can't support for OPT (clinical, legal practice, teaching, trades, lab science…) |

**You decide** with three columns, each answering one question:

| Column | Values | Means |
|---|---|---|
| **Decision** | Pending (default) · Apply · Pass | Do I want it? |
| **Interest** | 1–5 (on Apply) | How much? 1–2 backup, 3 solid, 4–5 all-in (tailored application + networking) |
| **Status** | Not started → Drafting → Applied → Networking → Interviewing → Offer, or Rejected / Withdrawn / No response | Where it stands |

Plus **Notes**. The pipeline stamps **Applied on** the first time it sees Status = Applied,
and flags **Follow up** when an Applied / Networking application hasn't moved for 10 days
(`tracking` in `criteria.yaml`).

**Tabs:**
* **To review** — every new posting that passed the must-haves (Decision = Pending), newest
  first. Your inbox: clear it.
* **My applications** — Decision = Apply (setting Interest or Status also counts), sorted by
  Interest 5 → 1, active statuses before ended ones. All-in (4–5) rows are green; Follow up
  rows are yellow.
* **Passed** — your archive; set Decision back to Apply any time.
* **Dropped** (with the rule + matched text), **Board health**, **Unparsed alerts**.
* **Funnel** — is it working? Latest run by source (seen → new → passed → new options, top
  reasons dropped), coverage (boards watched, boards discovered this week, JSearch searches
  used vs. budget and keyword×city pairs covered, last Muse sweep, career sites we can't read
  yet), and a history row per run.

A row moves to its tab on the next hourly run after you change Decision. Your columns are
also saved in the database, so they survive even if a tab is deleted, and the pipeline never
overwrites them or deletes a row you've touched. Facts on every row: Field, Location, Salary,
Extra comp, Experience ask, Visa (OPT year 1 · likely TN category later), Flags (New, Apply
soon, Low base — check incentives, Salary unknown, No H-1B sponsorship, Closed, …), Posted,
First seen, Source.

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
   (≈1–2 min), the Sheet has its tabs (To review, My applications, Passed, Dropped,
   Unparsed alerts, Funnel, Board health). After that it runs every hour at :17.
5. **Add a company** — edit `config/watchlist.yaml`: `name`, `platform`
   (greenhouse / lever / ashby / smartrecruiters) and `slug` from the board URL
   (`jobs.lever.co/<slug>`, `boards.greenhouse.io/<slug>`, `jobs.ashbyhq.com/<slug>`,
   `jobs.smartrecruiters.com/<slug>`).
   Check it once with `python -m src.run --print --company "<name>"`.

Open the Sheet from Google Drive or the Google Sheets app on your phone — bookmark it.

**Actions minutes:** hourly runs on a private repo use GitHub Actions minutes (about
1–2 min per run). If they run short, run the same `python -m src.run` on a small
always-on server or lower the schedule in `.github/workflows/hourly.yml`.

## Run

```bash
pip install -r requirements.txt
python -m src.run --print --company "<name>"                 # one watchlist board, live
python -m src.run --print --company "<name>" --fixture <saved API response>
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

`docs/BUILD_BRIEF.md` and `docs/ARCHITECTURE.md` describe the original v1 design (with
scoring); where they differ from this README or `config/criteria.yaml`, those win.
