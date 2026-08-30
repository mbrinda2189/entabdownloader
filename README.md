# Entab Question Bank Downloader

Logs into an Entab-based school parent/student portal, finds all "Q.Bank"
(question bank) assignment entries in a date range, downloads them, and
sorts the files into folders by subject.

```
QuestionBanks/
  ENGLISH/
  MATHEMATICS/
  SOCIAL SCIENCE/
  ...
downloaded_manifest.json   <- tracks what's already been downloaded
```

Re-running the script will **skip files it has already downloaded**.

## Setup (one-time)

1. Install Python 3.9+ and Google Chrome (any recent version — the driver
   is downloaded automatically to match it).
2. Open this folder in VS Code, open a terminal, and run:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your real username/password:
   ```
   copy .env.example .env      (Windows)
   cp .env.example .env        (Mac/Linux)
   ```
   **Never share your `.env` file or commit it to git.**

## Running it

```
python main.py
```

You'll be asked for a From Date and To Date (e.g. `01/04/2026` and
`31/03/2027` for the 2026-2027 academic year). A Chrome window will open
and the script will attempt everything automatically.

### If it pauses and asks you to do something manually

This is expected on the first run or two. The portal's exact page
structure couldn't be inspected ahead of time, so the script has
fallback checkpoints: if it can't confidently find the login box, the
menu link, the date filters, or the results table, it will tell you
exactly what to click in the visible browser window, then wait for you
to press Enter in the terminal before continuing. Everything scraped
after that point still happens automatically.

If you hit the same manual step every time, let me know what page/step
it is and I can hard-code that selector so it stops asking.

## Notes

- The script assumes "Assignment type" is already defaulted to `Q.Bank`
  on the page (as seen in the portal) — it doesn't change that dropdown.
- "Subjects" filter is left blank so all subjects are fetched in one
  pass; sorting into folders happens locally based on the "Subject Name"
  column.
- Downloaded file type defaults to `.pdf` unless the server response
  indicates otherwise.
