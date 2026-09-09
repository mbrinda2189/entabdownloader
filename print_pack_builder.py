"""
print_pack_builder.py

WHAT THIS DOES
--------------
Takes the lesson-to-PDF matches produced by lesson_matcher.py (already
confirmed by the parent for anything uncertain -- see print_pack_gui.py)
and turns them into actual print-ready files:

  1. For each subject + exam period that has at least one matched
     lesson, build ONE combined PDF: a short cover page (subject, exam
     period, which lessons are included, and -- highlighted -- which
     lessons from the annual portion had NO downloaded Question Bank to
     match) followed by every matched Question Bank PDF, in the same
     order the lessons appear in the annual portion.

  2. Write one overall text report listing every missing lesson across
     every subject and period, so the parent has a single place to check
     "did we get everything downloaded".

Output layout:

    PrintPacks/<child folder>/
        ENGLISH_UNIT_TEST_-_I.pdf
        ENGLISH_SEMESTER_-_I.pdf
        MATHS_UNIT_TEST_-_I.pdf
        ...
        MissingLessons.txt

WHY A COVER PAGE
------------------
A merged PDF with no context is hard to check at a glance -- if a lesson
is missing, the raw file gives no clue. The cover page exists purely so
whoever's photocopying/printing (or checking the pack before printing)
can immediately see: what this pack covers, what's included, and what's
missing and needs to be sorted out (re-downloaded, or was genuinely never
posted on the portal) before printing.
"""

import re
from pathlib import Path

from pypdf import PdfWriter, PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
import io


def _sanitize_filename(name: str) -> str:
    """Same rules as main.py's sanitize_filename -- kept as a local copy
    here (rather than importing main.py) so this module has no dependency
    on Selenium/the download side of the app and can be tested/reused on
    its own."""
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:120]


def _make_cover_page(subject: str, period: str, included: list, missing: list) -> bytes:
    """
    Build a single-page PDF (as raw bytes) summarizing this print pack:
    subject/period title, the list of included lessons, and -- clearly
    called out -- any lessons from the annual portion that had no
    matching downloaded PDF.

    Returns raw PDF bytes so the caller can feed them straight into
    pypdf without touching a temp file.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 20 * mm
    y = height - margin

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, y, f"{subject} — {period}")
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"{len(included)} lesson(s) included, {len(missing)} missing")
    y -= 10 * mm

    def draw_wrapped(text, font_size=10, indent=0, color=(0, 0, 0)):
        """Very simple word-wrap so long lesson titles don't run off the
        page edge -- good enough for the short phrases used here."""
        nonlocal y
        c.setFont("Helvetica", font_size)
        c.setFillColorRGB(*color)
        max_chars = 95
        words = text.split()
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) > max_chars:
                c.drawString(margin + indent, y, line)
                y -= 5.5 * mm
                line = word
                if y < margin:
                    c.showPage()
                    y = height - margin
                    c.setFont("Helvetica", font_size)
            else:
                line = candidate
        if line:
            c.drawString(margin + indent, y, line)
            y -= 5.5 * mm
        c.setFillColorRGB(0, 0, 0)

    if included:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, "Included:")
        y -= 6 * mm
        for lesson in included:
            draw_wrapped(f"\u2022 {lesson}", indent=3 * mm)
            if y < margin:
                c.showPage()
                y = height - margin

    if missing:
        y -= 4 * mm
        if y < margin:
            c.showPage()
            y = height - margin
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(0.7, 0, 0)
        c.drawString(margin, y, "MISSING (no downloaded Question Bank found):")
        c.setFillColorRGB(0, 0, 0)
        y -= 6 * mm
        for lesson in missing:
            draw_wrapped(f"\u2022 {lesson}", indent=3 * mm, color=(0.7, 0, 0))
            if y < margin:
                c.showPage()
                y = height - margin

    c.showPage()
    c.save()
    return buffer.getvalue()


def build_print_pack(subject: str, period: str, match_results: list, output_dir) -> dict:
    """
    Build one combined, print-ready PDF for a single subject + exam
    period, from that period's list of match-result dicts (see
    lesson_matcher.match_one_lesson for the shape of each dict).

    Writes the file to `output_dir` and returns a summary dict:
        {
            "output_path": Path or None (None if nothing to build),
            "included_count": int,
            "missing": [lesson strings with no match],
        }

    If there is nothing at all to include (every lesson missing), no PDF
    is written -- there'd be nothing but a cover page, which isn't useful
    to print -- but the missing list is still returned so it can go into
    the overall report.
    """
    output_dir = Path(output_dir)
    included_lessons = []
    missing_lessons = []
    # Keep the ordered, de-duplicated list of PDFs to merge -- ordered by
    # first appearance so the print pack follows the annual portion's own
    # lesson order, and de-duplicated so a PDF that happens to match two
    # lesson entries (e.g. a combined worksheet) isn't printed twice.
    pdfs_to_merge = []
    seen_paths = set()

    for result in match_results:
        if result["match"] is not None:
            included_lessons.append(result["lesson"])
            if result["match"] not in seen_paths:
                pdfs_to_merge.append(result["match"])
                seen_paths.add(result["match"])
        else:
            missing_lessons.append(result["lesson"])

    if not pdfs_to_merge:
        return {"output_path": None, "included_count": 0, "missing": missing_lessons}

    writer = PdfWriter()

    # Cover page first.
    cover_bytes = _make_cover_page(subject, period, included_lessons, missing_lessons)
    cover_reader = PdfReader(io.BytesIO(cover_bytes))
    for page in cover_reader.pages:
        writer.add_page(page)

    # Then every matched Question Bank PDF, each in full (a Q.Bank file
    # can itself be more than one page).
    for pdf_path in pdfs_to_merge:
        try:
            reader = PdfReader(str(pdf_path))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as e:
            # A single corrupt/unreadable download shouldn't sink the
            # whole print pack -- skip it, but make sure it's visible in
            # the summary so the parent knows to re-download that one.
            missing_lessons.append(f"{pdf_path.name} (file could not be read: {e})")

    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{_sanitize_filename(subject)}_{_sanitize_filename(period)}.pdf"
    output_path = output_dir / file_name
    with open(output_path, "wb") as f:
        writer.write(f)

    return {
        "output_path": output_path,
        "included_count": len(included_lessons),
        "missing": missing_lessons,
    }


def build_all_print_packs(all_matches: dict, output_dir) -> dict:
    """
    Run build_print_pack for every subject + period in `all_matches`.

    `all_matches` shape:
        {
            "ENGLISH": {"Unit Test - I": [match_result, ...], "Semester - I": [...], ...},
            "MATHS": {...},
            ...
        }

    Kept for standalone/scripted use (build everything in one go). The
    GUI wizard (print_pack_gui.py) normally uses build_print_packs_for_period()
    instead, since it builds one period at a time on demand -- see that
    function's docstring for why.

    Returns a dict of per-subject-per-period build summaries, same shape
    as `all_matches` but with build_print_pack's summary dict as the leaf
    value instead of a list of match results.
    """
    output_dir = Path(output_dir)
    results = {}

    # Reorganize by period first, so each period can go through the same
    # single-period build + report-update path the GUI uses -- this keeps
    # there being exactly one way MissingLessons.txt gets written, rather
    # than two slightly different report formats depending on which
    # function was used to build.
    by_period = {}
    for subject, periods in all_matches.items():
        for period, match_results in periods.items():
            by_period.setdefault(period, {})[subject] = match_results

    for period, subject_matches in by_period.items():
        period_results = build_print_packs_for_period(period, subject_matches, output_dir)
        for subject, summary in period_results.items():
            results.setdefault(subject, {})[period] = summary

    return results


def build_print_packs_for_period(period: str, subject_matches: dict, output_dir) -> dict:
    """
    Build print packs for every subject, but for ONE exam period only.

    `subject_matches` shape: {subject: [match_result, ...], ...} -- all
    for the same period.

    This is the function the GUI wizard calls when the parent clicks one
    of the six period buttons: since matching + review happens lazily
    per period (only when that period's button is clicked), building
    also happens per period, rather than requiring every period to be
    matched/reviewed/built together in one batch.

    Returns {subject: build_print_pack's summary dict}.
    """
    output_dir = Path(output_dir)
    results = {}
    period_missing_lines = []

    for subject, match_results in subject_matches.items():
        summary = build_print_pack(subject, period, match_results, output_dir)
        results[subject] = summary

        if summary["missing"]:
            period_missing_lines.append(f"\n{subject}")
            for lesson in summary["missing"]:
                period_missing_lines.append(f"  - {lesson}")

        if summary["output_path"]:
            print(f"Built: {summary['output_path'].name}  "
                  f"({summary['included_count']} included, {len(summary['missing'])} missing)")
        else:
            print(f"Skipped {subject} / {period}: nothing matched, no PDF built "
                  f"({len(summary['missing'])} lesson(s) missing).")

    _update_missing_report(output_dir, period, period_missing_lines)
    return results


def _update_missing_report(output_dir: Path, period: str, period_missing_lines: list) -> None:
    """
    Update MissingLessons.txt with the current missing-lesson list for
    ONE period, without disturbing what's recorded for any other period.

    This matters because periods are built independently, potentially
    days or weeks apart (a parent might build Unit Test I in June and
    Semester I in September) -- rebuilding one period should refresh
    that period's section of the report, not wipe out what's already
    known about the others. The file is organized into "### <period>"
    sections; this function reads whatever sections already exist,
    replaces (or adds) the one for `period`, and rewrites the file.
    """
    output_dir = Path(output_dir)
    report_path = output_dir / "MissingLessons.txt"
    sections = {}  # period_name -> list of raw lines (no header)

    if report_path.exists():
        current_period = None
        for line in report_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("### "):
                current_period = line[4:].strip()
                sections[current_period] = []
            elif current_period is not None:
                sections[current_period].append(line)

    sections[period] = period_missing_lines

    output_dir.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        for period_name, lines in sections.items():
            f.write(f"### {period_name}\n")
            content_lines = [l for l in lines if l.strip()]
            if content_lines:
                f.write("\n".join(content_lines) + "\n")
            else:
                f.write("Nothing missing -- every lesson had a matching download.\n")
            f.write("\n")