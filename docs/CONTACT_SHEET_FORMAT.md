# Contact sheet format

Paste the block below into the chat that builds contact sheets, so every sheet it makes drops
straight into the Contacts tab.

---

Every contact sheet you make for me must be a CSV with exactly these 13 columns, in this order,
with this header row:

Company, Name, Title, Covers, LinkedIn, Connection, Wave, Why, Angle, Write-up, Note sent, Follow up, Status

One row per person. No other columns, no blank rows, no notes above or below the table.

- Company: the company name exactly as it appears on my Apply tab (for example "EliseAI").
- Name: full name.
- Title: their current title, as on LinkedIn.
- Covers: which posting the person relates to. Use the job title as it appears on my Apply tab
  (for example "Growth Strategy Analyst"), or "Both" / "All" when they relate to every posting I have
  at that company.
- LinkedIn: the full profile URL.
- Connection: degree and mutuals, for example "2nd. Mutual: Seth Samowitz".
- Wave: 1 (message first), 2 (message only if wave 1 is quiet after a week), or Hold (on file, do not
  message).
- Why: one or two sentences on why this person.
- Angle: what to open with and what to ask.
- Write-up: the LinkedIn connection note, under 300 characters. Only for waves 1 and 2; leave it blank
  for Hold.
- Note sent: leave blank. I fill it in when I send the note.
- Follow up: leave blank. The sheet works it out (Note sent + 7 days).
- Status: "Not sent" for waves 1 and 2, "On file" for Hold. Later values are Sent, Accepted, Replied.

Name the file "Contacts - <Company>.csv".

---

## Getting it into the Sheet

Either way works; the result is the same.

1. **Paste:** open the CSV, copy every row except the header, click the first empty cell in column A
   of the Contacts tab and paste. Follow up and Job link fill themselves in.
2. **Hands-off:** upload the CSV to Google Drive, open it with Google Sheets (keep the title
   "Contacts - <Company>"), and share it with the pipeline's service account as a viewer. Within about
   15 minutes the pipeline adds every person who isn't already on Contacts. Sharing it again later,
   or re-running, never adds anyone twice, and it never overwrites what you've typed.
