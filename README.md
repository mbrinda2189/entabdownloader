# Entab Question Bank Downloader

Logs into an Entab-based school parent/student portal, finds all "Q.Bank"
(question bank) assignment entries in a date range, downloads them, sorts
the files into folders by subject, and grabs the school's **Annual
Portion** document too. A second tool, **Build Print Packs**, then
combines those downloaded files into print-ready PDFs grouped by subject
and exam period.

**What it does, in short:**
- Downloads every Question Bank PDF posted on the portal, sorted into
  one folder per subject (per child, if you have more than one).
- Auto-downloads the Annual Portion document alongside them.
- Skips anything it's already downloaded on future runs.
- Detects Question Banks the school's portal filed under the wrong
  subject, and offers a one-click fix.
- Combines everything into one print-ready PDF per subject per exam
  period (Unit Test I–IV, Semester I/II), with a cover page showing
  what's included and what's still missing.

```
QuestionBanks/
  <Child Name> - <Class>/          <- one per child, if you have more than one
    ENGLISH/
    MATHEMATICS/
    SOCIAL SCIENCE/
    ...
    _AnnualPortion.pdf             <- auto-downloaded every run, always the latest
downloaded_manifest_<child>.json   <- tracks what's already been downloaded
PrintPacks/
  <Child Name> - <Class>/
    ENGLISH_UNIT_TEST_-_I.pdf
    ENGLISH_SEMESTER_-_I.pdf
    ...
    MissingLessons.txt            <- what's still missing, per period
```

Re-running the script will **skip files it has already downloaded**.

## Setup (one-time)

1. Install Python 3.9+ and Google Chrome (any recent version — the driver
   is downloaded automatically to match it).
2. Open this folder in VS Code, open a terminal, and run:
   ```
   pip install -r requirements.txt
   ```

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

## Building print-ready packs from an Annual Portion

Every download run now also auto-fetches the school's **Annual Portion**
document (found under Assignment type = "Exam Portions", the row titled
"... ANNUAL PORTION ..."), saved as `_AnnualPortion.pdf` inside each
child's folder. If the school hasn't posted one yet, this is skipped
without affecting your Question Bank downloads.

The **"Build print packs..."** button turns downloaded Question Banks
into combined, print-ready PDFs grouped by subject and exam period (Unit
Test I–IV, Semester I/II):

1. **Child & annual portion** — mostly automatic. If you only have one
   child, this step is just a confirmation; with more than one, you pick
   from a dropdown. The auto-downloaded Annual Portion is used
   automatically, with a manual upload option as a fallback.
2. **Review the extracted syllabus** — PDF table extraction is never
   perfect, so you get a chance to fix any lesson that got split oddly.
   This is cached (keyed by the PDF's content) so re-running later on an
   unchanged Annual Portion skips straight past this step.
3. **Matching subjects to folders happens silently** — e.g. "MATHS" is
   matched to a "MATHEMATICS" folder automatically. You only see a
   screen for this if a subject is genuinely ambiguous (matches more
   than one downloaded folder).
4. **Six period buttons** — Unit Test I–IV, Semester I/II. Nothing is
   computed until you click one. Clicking a period matches lessons to
   downloaded PDFs for just that period, shows a quick confirm step only
   if something's uncertain, then builds
   `PrintPacks/<child>/<Subject>_<Period>.pdf` for every mapped subject.
   Your choices for a period are remembered for the rest of the session,
   so rebuilding it later (e.g. after downloading more Q.Bank PDFs)
   won't re-ask about lessons you already sorted out.

Each pack has a cover page listing what's included and, in red, what's
missing. `PrintPacks/<child>/MissingLessons.txt` tracks missing lessons
per period, updated independently each time you build that period.

**Note:** Tamil and Hindi are intentionally skipped — those columns are
set in a non-Unicode font in the school's PDF, so text extraction would
produce scrambled text rather than real content.

### If a subject shows as "not yet downloaded" but you know it was posted

This almost always means one of two things, both visible on the home
screen of the Build Print Packs wizard:

- **The school's portal filed it under the wrong subject.** Whoever
  uploads a Q.Bank can leave the "Subject" dropdown on whatever it was
  last set to, so e.g. a Physics chapter can get posted with Subject
  Name = "Mathematics" — the downloader has no way to know the title
  disagrees with that. If this has happened, the wizard's home screen
  shows a warning listing the misfiled file(s) with a one-click **Move**
  button per file.
- **It just hasn't been downloaded yet for this child.** Re-run the
  main downloader — it only fetches what the portal has posted as of
  that run.

If neither explains it, check the Assignment Date range you're
downloading with (`FromDate`/`ToDate`), and check the portal itself
(filter Assignment type = Q.Bank, Subject = the one in question) to
confirm it's actually been posted.

## Version history

**v1.2.0**
- Annual Portion document now downloads automatically alongside Question
  Banks (Assignment type = "Exam Portions"), always kept as the latest
  version posted.
- New: Build Print Packs — combines downloaded Question Banks into one
  print-ready PDF per subject per exam period, with a cover page and a
  missing-lessons report.
- New: detects Question Banks the portal filed under the wrong subject
  and offers a one-click fix.
- Fixed: a file whose folder was ever moved or cleaned up could get
  silently skipped forever on later runs, because the "already
  downloaded" check trusted its manifest record without confirming the
  file was still actually there.

**v1.0.0**
- Initial release: logs in, downloads Question Bank PDFs for one or more
  children (sibling switching supported), sorts them into per-subject
  folders, and skips files already downloaded on re-runs.
