"""
annual_portion_parser.py

WHAT THIS DOES
--------------
Reads the school's "Annual Portion" PDF -- the syllabus grid that lists,
for each subject, which chapters/lessons are covered in each exam period
(Unit Test I, Unit Test II, Semester I, Unit Test III, Unit Test IV,
Semester II) -- and turns it into a plain Python data structure:

    {
        "periods": ["Unit Test - I", "Unit Test - II", ...],
        "subjects": {
            "ENGLISH": {
                "Unit Test - I": ["Prose: Ln-1 Nicobobinus", "Ln-2 The Ransom of Red Chief", ...],
                "Unit Test - II": [...],
                ...
            },
            "MATHS": {...},
            ...
        },
        "skipped_subjects": ["II LANGUAGE TAMIL", "II LANGUAGE HINDI", ...],
        "warnings": ["Page 4: ...", ...],
    }

This is used by print_pack_builder.py (via lesson_matcher.py) to know which
downloaded Question Bank PDFs belong together for a given subject + exam
period, so they can be merged into one print-ready file.

WHY THIS IS "BEST EFFORT", NOT A GUARANTEED-PERFECT PARSE
-----------------------------------------------------------
Annual portion PDFs are made in Word/Excel by school staff, not by us, and
the exact table layout can vary by class and by year. This parser handles
the two real quirks we found in the sample document:

  1. A single subject's syllabus can be too long to fit on one PDF page,
     so it "overflows" onto the next page as a new table row whose first
     cell (the subject name) is BLANK. We detect that and glue the
     overflow text back onto the correct subject/column instead of
     treating it as a new (broken) subject row.

  2. Each cell of the grid is a wall of text like "Prose: Ln-1
     Nicobobinus, Ln-2 The Ransom of Red Chief ..." with no consistent
     separator. We split it into individual lesson entries by looking for
     recognizable "anchors" -- chapter/lesson/theme/unit codes (Ch-1,
     Ln-2, U-7, Theme 3, Chapter 8, Unit 4) and section labels (Prose:,
     Poem:, Grammar:, Composition:, History-, Civics-, Map work:).

Because splitting free-form text like this can never be 100% exact (a
"Ch-11:..." cell might get grouped with the next item, for example), the
result of this parser is always meant to be shown to the parent for a
quick review/edit BEFORE it's used for matching -- see print_pack_gui.py.
Treat this module's output as a strong first draft, not gospel.

LANGUAGE SUBJECTS (TAMIL / HINDI) ARE SKIPPED ON PURPOSE
-----------------------------------------------------------
Those columns are typically set in a custom (non-Unicode) font in these
school PDFs, so text extraction returns scrambled characters -- not real
Tamil/Hindi text. Rather than silently mis-parsing them, we detect any
subject row whose name contains "TAMIL" or "HINDI" and skip it entirely,
recording it in "skipped_subjects" so the parent knows it was left out on
purpose (not lost by accident).
"""

import re
from pathlib import Path

import pdfplumber


# Bump this whenever split_lessons() or parse_annual_portion_pdf()'s
# extraction logic changes in a way that could produce a different
# lesson list than before (like the colon-separator fix that introduced
# this constant). print_pack_gui.py stamps this into every parsed
# result and checks it against a cached, already-reviewed annual portion
# before trusting that cache -- so a parsing fix takes effect on a
# parent's very next run instead of being silently masked by a cached
# review that was done before the fix existed. This is precisely what
# happened with the "Chapter:1" colon-separator fix: a parent who had
# already reviewed a PDF under the old (buggy) splitting kept getting
# that stale, unsplit result back every run, because the cache only
# tracked "is this the same PDF", not "is this the same parsing code".
PARSER_VERSION = 2


# ---------------------------------------------------------------------------
# LESSON-SPLITTING
# ---------------------------------------------------------------------------

# An "anchor" marks the start of a new lesson entry inside a table cell's
# wall of text. We split right before each anchor. This covers:
#   - chapter/lesson/unit/theme codes: Ch-1, Ln-2, U-7, Theme 3, Chapter 8,
#     Unit 4, Chapter:1, Chapter: 1 (with or without a space/dash/colon/
#     period between the label and the number, since different schools'
#     source documents are inconsistent about that -- colon-separated
#     codes like "Chapter:1" showed up in a real document and were
#     originally missed because only hyphen/en-dash/period were accepted)
#   - section labels used instead of/alongside a code: Prose:, Poem:,
#     Grammar:, Composition:, History-, Civics-, Map work:
_ANCHOR_PATTERN = re.compile(
    r"("
    r"\b(?:Ch|Ln|U|Theme|Chapter|Unit)\b\s*[-\u2013\u2014.:]?\s*\d+"
    r"|Prose\s*:|Poem\s*:|Grammar\s*:|Composition\s*:"
    r"|History\s*[-\u2013]|Civics\s*[-\u2013]|Map work\s*:"
    r")",
    re.IGNORECASE,
)

# A fragment that is JUST a bare section label (e.g. "Prose:", "History-")
# with nothing else -- happens when an anchor is immediately followed by
# another anchor. We fold these into the next fragment instead of leaving
# them as their own (useless) lesson entry.
_LABEL_ONLY_PATTERN = re.compile(r"^[A-Za-z ]{2,15}[:\-\u2013]$")


def split_lessons(cell_text: str) -> list:
    """
    Turn one table cell's raw text (a subject's syllabus for one exam
    period) into a list of individual lesson strings.

    This is intentionally a bit "loose" (it will sometimes group two
    small items into one entry, e.g. two poems named in the same
    sentence) -- the parent reviews and can split/edit entries in the
    GUI before they're used for matching, so slight over-grouping here
    is a minor annoyance, not a correctness bug.
    """
    if not cell_text or not cell_text.strip():
        return []

    # Collapse all whitespace/newlines (PDF text extraction inserts a lot
    # of mid-sentence line breaks from how the cell wraps) into single
    # spaces, so our anchor regex isn't tripped up by stray newlines.
    text = re.sub(r"\s+", " ", cell_text).strip()

    # re.split() with a pattern made only of a capturing group around the
    # anchors keeps the anchors themselves in the output list (as their
    # own list entries), interleaved with the text between them.
    raw_pieces = _ANCHOR_PATTERN.split(text)

    # Recombine: each anchor should stay attached to the text that
    # follows it (that's the lesson title), not stand alone.
    pieces = []
    i = 0
    while i < len(raw_pieces):
        piece = raw_pieces[i].strip(" ,;")
        if _ANCHOR_PATTERN.fullmatch(piece) and i + 1 < len(raw_pieces):
            # This piece is an anchor (e.g. "Ch-1", "Prose:") -- merge it
            # with the very next piece (its title text).
            piece = f"{piece} {raw_pieces[i + 1].strip(' ,;')}".strip()
            i += 2
        else:
            i += 1
        if piece:
            pieces.append(piece)

    # Fold any leftover bare section label into the following entry
    # (covers "History-" immediately followed by "Chapter1 - ...").
    merged = []
    i = 0
    while i < len(pieces):
        p = pieces[i]
        if _LABEL_ONLY_PATTERN.match(p) and i + 1 < len(pieces):
            merged.append(f"{p} {pieces[i + 1]}")
            i += 2
        else:
            merged.append(p)
            i += 1

    # Drop tiny junk fragments (stray punctuation, single letters left
    # over from a bad split).
    return [m for m in merged if len(m) >= 3]


# ---------------------------------------------------------------------------
# SUBJECT NAME HANDLING
# ---------------------------------------------------------------------------

def _clean_subject_cell(raw: str) -> str:
    """Collapse a subject cell's text (which may span several lines, e.g.
    'II\\nLANGUAGE\\nTAMIL') into one clean, single-line string."""
    return re.sub(r"\s+", " ", (raw or "")).strip()


def _is_language_subject(subject_name: str) -> bool:
    """True for Tamil/Hindi language rows, which we intentionally skip
    (see module docstring for why)."""
    upper = subject_name.upper()
    return "TAMIL" in upper or "HINDI" in upper


# ---------------------------------------------------------------------------
# MAIN PARSER
# ---------------------------------------------------------------------------

def parse_annual_portion_pdf(pdf_path) -> dict:
    """
    Parse an annual portion PDF into the structure described at the top of
    this file. Returns a dict with keys: "periods", "subjects",
    "skipped_subjects", "warnings".

    This function never raises for a merely-messy PDF -- if a page's table
    can't be found or a row looks malformed, it's recorded in "warnings"
    and parsing continues, so the parent still gets a usable (if partial)
    result to review rather than a hard crash.
    """
    pdf_path = Path(pdf_path)
    periods = []
    # subject_name -> {period_name: raw_cell_text}
    raw_subject_cells = {}
    # Preserves the order subjects first appear in, so the reviewed table
    # reads top-to-bottom the same way the original PDF does.
    subject_order = []
    skipped_subjects = []
    warnings = []

    last_subject_name = None  # for gluing page-break continuation rows

    with pdfplumber.open(str(pdf_path)) as pdf:
        header_seen = False

        for page_index, page in enumerate(pdf.pages):
            table = page.extract_table()

            if not table:
                warnings.append(
                    f"Page {page_index + 1}: no table could be found on this "
                    f"page -- any subject rows on it were NOT picked up. "
                    f"You may need to add that subject's lessons by hand."
                )
                continue

            for row in table:
                # Defensive: a malformed row shorter than expected.
                if not row or len(row) < 2:
                    continue

                first_cell = _clean_subject_cell(row[0])

                # The very first real row of the whole document is the
                # header row (SUBJECTS | UNIT TEST - I | ...). We only
                # expect to see it once, on page 1.
                if not header_seen and first_cell.upper().startswith("SUBJECT"):
                    periods = [_clean_subject_cell(c) for c in row[1:]]
                    header_seen = True
                    continue

                if not header_seen:
                    # We hit data before ever finding a header row -- fall
                    # back to generic period names so parsing can still
                    # proceed, and flag it clearly for the parent.
                    periods = [f"Period {i + 1}" for i in range(len(row) - 1)]
                    header_seen = True
                    warnings.append(
                        "Could not find the 'SUBJECTS' header row -- using "
                        "generic period names (Period 1, Period 2, ...). "
                        "Please check the reviewed table and rename periods "
                        "if needed."
                    )

                if first_cell == "":
                    # Blank subject cell = this row is the OVERFLOW
                    # continuation of the previous subject (its syllabus
                    # text ran onto a new PDF page). Glue each column's
                    # text onto that subject instead of starting a new one.
                    if last_subject_name is None:
                        warnings.append(
                            f"Page {page_index + 1}: found a continuation row "
                            f"with no subject to attach it to -- it was skipped."
                        )
                        continue
                    target = raw_subject_cells[last_subject_name]
                    for col_index, period_name in enumerate(periods, start=1):
                        if col_index < len(row) and row[col_index]:
                            existing = target.get(period_name, "")
                            target[period_name] = (existing + " " + row[col_index]).strip()
                    continue

                # A normal new-subject row.
                last_subject_name = first_cell
                if first_cell not in raw_subject_cells:
                    raw_subject_cells[first_cell] = {}
                    subject_order.append(first_cell)

                for col_index, period_name in enumerate(periods, start=1):
                    if col_index < len(row) and row[col_index]:
                        existing = raw_subject_cells[first_cell].get(period_name, "")
                        raw_subject_cells[first_cell][period_name] = (
                            existing + " " + row[col_index]
                        ).strip()

    # Now split every subject's raw cell text into individual lessons,
    # skipping Tamil/Hindi language rows entirely.
    subjects = {}
    for subject_name in subject_order:
        if _is_language_subject(subject_name):
            skipped_subjects.append(subject_name)
            continue

        subjects[subject_name] = {}
        for period_name in periods:
            raw_text = raw_subject_cells[subject_name].get(period_name, "")
            subjects[subject_name][period_name] = split_lessons(raw_text)

    if not subjects:
        warnings.append(
            "No usable subjects were found in this PDF at all -- the table "
            "layout may be too different from what this tool expects. "
            "You'll need to enter subjects/lessons manually."
        )

    return {
        "periods": periods,
        "subjects": subjects,
        "skipped_subjects": skipped_subjects,
        "warnings": warnings,
        "_parser_version": PARSER_VERSION,
    }