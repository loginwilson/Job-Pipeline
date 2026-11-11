# Contact sheet format (Contacts tab, Nov 11)

The Contacts tab has these 16 columns, in this order, for every company:

| Col | Header | Filled by | Notes |
|---|---|---|---|
| A | Company | CSV | |
| B | Job | CSV | Posting title exactly as on the jobs tab; two postings separated by a semicolon. Never "Both". |
| C | Name | CSV | |
| D | Role / team | CSV | Title and team, as on LinkedIn |
| E | Location | CSV | e.g. "New York" |
| F | LinkedIn | CSV | Full profile URL (clickable) |
| G | How you're connected | CSV | Degree, mutuals, shared school |
| H | Priority | CSV | 1, 2 or Hold |
| I | Why | CSV | |
| J | Angle | CSV | The hook and the ask |
| K | Message | CSV | LinkedIn connection note, 300 characters max (red if over) |
| L | Status | CSV, then you | Not sent, Sent, Accepted, Replied, Meeting set, Met, No reply |
| M | Last contact | you | YYYY-MM-DD |
| N | Next step | CSV, then you | Send note, Follow up, Book coffee, Send thanks, None |
| O | Due | you / sheet | YYYY-MM-DD. When Status is Sent and Due is empty, the pipeline sets Last contact + 7 days |
| P | Notes | you | |

## Brief for the chat that builds contact sheets

---

Every contact sheet you make for me is a CSV with exactly 16 columns in this order and NO header row:

Company, Job, Name, Role / team, Location, LinkedIn, How you're connected, Priority, Why, Angle, Message, Status, Last contact, Next step, Due, Notes

One row per person; no blank rows and nothing above or below the rows.

- Company: exactly as on my jobs tab (e.g. "EliseAI").
- Job: the posting title exactly as on my jobs tab. If the person covers two postings, list both separated by a semicolon. Never write "Both".
- Name; Role / team (title and team as on LinkedIn); Location (city, e.g. "New York").
- LinkedIn: full profile URL.
- How you're connected: degree (2nd, 3rd), mutual connections, shared school.
- Priority: 1, 2 or Hold.
- Why: why this person. Angle: the hook and what I'm asking for.
- Message: the LinkedIn connection note, 300 characters or fewer. Blank for Hold.
- Status: "Not sent".
- Last contact: blank.
- Next step: "Send note" for Priority 1 and 2, "None" for Hold.
- Due: blank. Notes: blank.
- Dates, when there are any, are YYYY-MM-DD.
- Quote any field that contains a comma (standard CSV).

Name the file "Contacts - <Company>.csv".

---

## Adding a sheet

**On a laptop (most reliable):** in the Sheet, open the Contacts tab, then File → Import → Upload →
choose the CSV → Import location: "Append to current sheet" → Separator type: Comma → Import data.
It lands below the last filled row, in columns A to P, without touching existing rows.

**Hands-off:** upload the CSV to Google Drive, open it with Google Sheets (title "Contacts - <Company>")
and share it as a viewer with the pipeline's service account. Within about 15 minutes the pipeline
adds everyone not already on Contacts (same company + name is never added twice).

**On a phone or iPad:** importing isn't available in the Sheets app. Use the hands-off route, or open
the CSV in Google Sheets, copy all rows, and paste into the first empty cell in column A of Contacts.
