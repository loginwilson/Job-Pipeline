# Job Pipeline

Finds public job postings in your cities, keeps only the ones that meet your criteria
(`config/criteria.yaml`), and keeps a Google Sheet updated where you decide what to do with
each one. Runs on GitHub Actions about every 15 minutes (`.github/workflows/hourly.yml`).

## Where postings come from (`config/sources.yaml`)

| Source | What it is | How often |
|---|---|---|
| **Company job boards** (~800) | Ashby, Greenhouse, Lever, Workday, Oracle, SmartRecruiters, Workable, Gem, Rippling and Comeet boards: the ones in `config/watchlist.yaml` plus every board discovered automatically | Every run; Workday / Oracle sites and boards with nothing passing for 3 days every 12 hours |
| **Google Jobs** (JSearch, SerpApi) | LinkedIn, Indeed, Glassdoor and company sites, searched by keyword × city | Within each free daily budget, spread over the day |
| **Adzuna** | Job-site search by keyword × city | 48 searches a day, spread over the day |
| **Discovery only** | YC company directory, Hacker News "Who is hiring?", SimplifyJobs new-grad list: used to find company boards, which then join the watch | Daily |
| The Muse | Off (its API is behind a bot check) | — |

**Auto-discovery.** Every company these sources surface in your cities is checked for its own
board. Found boards join the watch and show in the Board health tab.

**No duplicates.** The same job from several sources, or listed in several cities, appears
once: the company-board copy wins and keeps any decision you already made.

**Active only.** Board postings show while they're in the company's live feed; search
postings must be recent. Closed postings you never touched leave To review. Search results
(passing ones and near misses) are stored in full and re-checked against the current rules
every run, so a rule fix reaches them too.

## The rules

A posting reaches **To review** only if it passes every rule below. Anything else goes to
the Dropped tab with the rule and the exact text that triggered it.

| Rule | Kept only if… |
|---|---|
| Role type | business / strategy operations, deployment / implementation, chief of staff / founder's associate, generalist, growth / revenue / data / product operations, or a grad / rotational program. AI-builder titles (GTM engineer, automation) only when coding familiarity is enough |
| Never | acquisitions / asset management, sales, marketing, banking / finance roles, engineering, design / product, HR, legal, accounting, front-line jobs, staffing agencies. At finance firms only operations / generalist roles; at hotel companies only corporate-level roles |
| Experience | asks for 1 year at most (a range starting at 0 or 1 passes), and nothing in the title or text marks it as senior |
| Pay | any part of the posted base range reaches $100K (grad programs $90K; Canada C$100K; in the US the bottom of a range must also reach $80K, grad programs included). OTE doesn't count. No posted pay is kept only for grad programs, AI-forward companies and your core role types on startup boards |
| Location | NYC · Chicago / Florida / California / Toronto · Texas / Vancouver; not solely remote |
| Type | full-time (no internship, contract, temp, co-op, part-time) |
| Work authorization | no US citizenship / clearance requirement ("no sponsorship" is only flagged: OPT / TN usually fine); related to your degree for OPT |
| Skills | no required coding proficiency, credential you lack (MBA, JD, CPA, CS degree…), financial modelling, or other language |
| Start date | not before you graduate (May 2027); starts after Aug 1, 2027 are flagged |
| Real opening | not a talent pool / general application, and still active |

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
* **Dropped** — the newest 1,000 drops with the rule + matched text (all are kept in the
  database; `tracking.dropped_tab_max_rows`), **Board health**, **Unparsed alerts**.
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
   Unparsed alerts, Funnel, Board health). After that it runs on its own schedule.
5. **Add a company** — edit `config/watchlist.yaml`: `name`, `platform`
   (greenhouse / lever / ashby / smartrecruiters) and `slug` from the board URL
   (`jobs.lever.co/<slug>`, `boards.greenhouse.io/<slug>`, `jobs.ashbyhq.com/<slug>`,
   `jobs.smartrecruiters.com/<slug>`).
   Check it once with `python -m src.run --print --company "<name>"`.

Open the Sheet from Google Drive or the Google Sheets app on your phone — bookmark it.

**Actions minutes:** runs are free on a public repo; on a private repo they use GitHub
Actions minutes (a normal run takes a few minutes, a check-everything run about 30–45). If they run short, run the same `python -m src.run` on a small
always-on server or lower the schedule in `.github/workflows/hourly.yml`.

## Run

```bash
pip install -r requirements.txt
python -m src.run --print --company "<name>"                 # one watchlist board, live
python -m src.run --print --company "<name>" --fixture <saved API response>
python -m src.run --fake-sheet data/fake_sheet.json            # full run, local stand-in Sheet
python -m src.run --probe oracle:jpmc.fa.oraclecloud.com/CX_1001  # check one board, print a summary
SHEET_ID=... GOOGLE_SERVICE_ACCOUNT_JSON="$(cat key.json)" python -m src.run   # real Sheet, from your laptop
python -m pytest
```

**On GitHub (Actions tab):** *hourly pipeline* → *Run workflow* runs it now; tick
**check_all** to re-check every source and board immediately instead of waiting for their
daily / 12-hour schedules (takes 30–45 min). *probe a board* checks any board on GitHub's network
and prints what passes.

## Config files

| File | What it is |
|---|---|
| `config/criteria.yaml` | The rules. Every threshold and term list is read from here. |
| `config/sources.yaml` | Search keywords, cities, daily budgets and how often each source is checked. |
| `config/watchlist.yaml` | Companies polled — **you choose them**; the pipeline never picks companies, it judges each posting against the criteria. Optional `sectors` tags only matter for the rules that need them (see the file header). |
| `config/reference.yaml` | Not rules: place-name reference data (NYC = "New York", state codes so "Richmond, VA" isn't Richmond BC) and the fuzzy-match score. |

## Matching conventions

Term lists match whole words, case-insensitively (all-caps acronyms like `OPT`, `ITAR`
match case-sensitively), hyphens = spaces, `U.S.` = `US`, singular/plural tolerant.
Every gate is evaluated, so a dropped posting lists all the rules it failed.

`docs/BUILD_BRIEF.md` and `docs/ARCHITECTURE.md` describe the original v1 design (with
scoring); where they differ from this README or `config/criteria.yaml`, those win.
