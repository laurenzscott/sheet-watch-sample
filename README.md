# sheet-watch-sample

A table watched on a schedule. New rows are e-mailed to you, every run is logged.

This is a work sample, not a client project. It runs here in public on real data
so you can see it working.

## What it does

Every six hours a GitHub Action:

1. reads the source table once (any CSV link),
2. checks that the columns it relies on are still there,
3. compares each row's key with the keys it has already seen,
4. writes **one** e-mail with the new rows, as a short table plus the full rows
   attached as `new-rows.csv`. No new rows, no e-mail,
5. adds one line to `data/run-log.csv`, whatever happened.

The very first run only records what is already there and sends nothing, so you
are not flooded with the whole table on day one. A row is never reported twice.

## The source in this demo

The [USGS feed of earthquakes of magnitude 2.5 and above in the last 24 hours](https://earthquake.usgs.gov/earthquakes/feed/v1.0/csv.php),
a public CSV that gets new rows all day long. It stands in for what a client
would point it at: a Google Sheet published as CSV, the responses sheet of a
form, an export from a shop or a CRM.

## The live copy in this repository

- `data/last-run.txt` - the last run and the totals since the first run
- `data/run-log.csv` - one line per run: time, status, rows in the source,
  new rows, whether an e-mail went out
- `outbox/` - the e-mails, as `.eml` files you can open in any mail program
  (the newest 30 are kept)
- `data/facts.json` - the same totals, machine-readable

This public copy has no mail account connected, so it writes its e-mails to
`outbox/` instead of sending them. With a mail account connected (see below) it
sends them and still keeps the copy.

## When something goes wrong, it says so

A watch that breaks quietly is worse than none. So instead of reporting
"no new rows", it writes a **Check needed** e-mail and keeps its last good state
when:

- a column it relies on has disappeared (the sheet was rebuilt),
- the source suddenly returns no rows at all,
- the source cannot be reached after three tries.

Network errors and server errors are retried; a refusal from the source is
never retried. Every request carries an honest name.

## How a client would use this

1. You send me the CSV link of your sheet (in Google Sheets: File, Share,
   Publish to web, CSV) and tell me which column identifies a row and which
   columns you want to see in the e-mail.
2. I set it up in **your** GitHub account: free tier, no server, no monthly fee,
   no login of mine. Your data never sits on my side.
3. You add your mail account as three secrets in the repository settings:
   `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` (plus optional `SMTP_PORT`,
   default 587, and `MAIL_TO`). From then on the e-mails are sent.
4. Changing what it watches later means editing `config.toml`, nothing else.

## Files

- `config.toml` - source, key column, required columns, e-mail settings
- `watch.py` - the watch itself, standard library only
- `tests/test_watch.py` - 16 tests, run before every check, never touching the
  network: new rows, nothing new, duplicates, rows that leave and come back,
  a missing column, an empty source, an unreachable source, a refusal that is
  not retried
- `.github/workflows/watch.yml` - the schedule (every 6 hours, minute 23 UTC)
