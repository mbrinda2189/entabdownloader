"""
lesson_matcher.py

WHAT THIS DOES
--------------
Takes the lesson list for one subject + exam period (produced by
annual_portion_parser.py) and the actual downloaded Question Bank PDF
files sitting in that subject's folder, and figures out which PDF (if
any) corresponds to each lesson.

A downloaded PDF's filename is whatever the portal's assignment "Title"
was (sanitized for the filesystem, see main.py's sanitize_filename), so
it won't be word-for-word identical to the lesson text in the annual
portion. For example the annual portion says "Ln-1 Nicobobinus" but the
portal's Q.Bank title might be "Nicobobinus - Question Bank" or "Lesson 1
Nicobobinus QB".

MATCHING STRATEGY (in priority order)
--------------------------------------
1. CODE MATCH (highest confidence): both the lesson text and the PDF
   filename contain the same chapter/lesson/unit/theme code (e.g. both
   contain "Ln-1" / "Ln1" / "Lesson 1" -- we normalize all of these to
   the same token "ln1" before comparing). This is the strongest signal
   because these codes are usually typed consistently by whoever set up
   the portal, even when the surrounding wording differs.

2. FUZZY TEXT MATCH (fallback, used when no code match is found on
   either side, or the codes don't agree): compare the lesson text and
   the filename as plain text using Python's built-in difflib, which
   needs no extra dependency. This catches cases like a lesson named
   "The Ransom of Red Chief" matching a PDF titled "Ransom of the Red
   Chief QB" even though neither has a usable code.

Every match gets a confidence score. Confident matches (code match, or a
strong fuzzy score) are applied automatically. Weaker matches are marked
"uncertain" and are meant to be shown to the parent for a quick confirm/
fix in the GUI (see print_pack_gui.py) -- we deliberately do NOT silently
guess on borderline cases, since a wrong auto-match would put the wrong
PDF into a printed pack.
"""

import difflib
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# CODE TOKEN EXTRACTION
# ---------------------------------------------------------------------------

# Matches chapter/lesson/unit/theme codes in either the annual portion's
# wording ("Ch-1", "Ln-2", "U-7", "Theme 3", "Chapter 8", "Chapter:1") or
# however a portal's Q.Bank title might spell the same thing out
# ("Lesson 1", "Chapter-8", "Unit 4"). Captures the label and the number
# separately so they can be normalized together (e.g. "Ch" and "Chapter"
# both -> "ch"). The separator between label and number is optional and
# can be a space, hyphen, en/em-dash, period, or colon -- different
# schools' documents are inconsistent about this (a colon-separated
# "Chapter:1" style was missed until a real document surfaced it).
_CODE_PATTERN = re.compile(
    r"\b(Ch|Chapter|Ln|Lesson|U|Unit|Theme)\b\s*[-\u2013\u2014.:]?\s*(\d+)",
    re.IGNORECASE,
)

# Different words that mean the same kind of code get folded to one
# canonical label before comparing, so "Chapter 8" and "Ch-8" both
# normalize to "ch8", and "Lesson 1" and "Ln-1" both normalize to "ln1".
_LABEL_ALIASES = {
    "ch": "ch", "chapter": "ch",
    "ln": "ln", "lesson": "ln",
    "u": "u", "unit": "u",
    "theme": "theme",
}


def extract_code_token(text: str):
    """
    Find the first chapter/lesson/unit/theme code in `text` and return it
    as a normalized string like "ch8", "ln1", "u7", "theme3". Returns
    None if no such code appears in the text at all.
    """
    if not text:
        return None
    match = _CODE_PATTERN.search(text)
    if not match:
        return None
    label = _LABEL_ALIASES.get(match.group(1).lower(), match.group(1).lower())
    number = match.group(2)
    return f"{label}{number}"


def normalize_text(text: str) -> str:
    """Lowercase and strip punctuation/extra whitespace, for fuzzy text
    comparison (so "Ransom of Red Chief!" and "ransom-of-red-chief"
    compare as equal)."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# CONFIDENCE THRESHOLDS
# ---------------------------------------------------------------------------

# Fuzzy-match ratio (0.0-1.0, from difflib.SequenceMatcher) at or above
# this is treated as confident enough to auto-apply without asking the
# parent to confirm it.
FUZZY_CONFIDENT_THRESHOLD = 0.55

# Below this, we don't even offer it as a low-confidence suggestion --
# the lesson is just reported as unmatched (no candidate worth showing).
FUZZY_MINIMUM_THRESHOLD = 0.28


def match_one_lesson(lesson_text: str, candidate_pdfs: list) -> dict:
    """
    Find the best-matching PDF (a list of pathlib.Path objects, all
    already downloaded for this subject) for a single lesson string.

    Returns a dict:
        {
            "lesson": lesson_text,
            "match": Path or None,
            "score": float (0.0-1.0),
            "method": "code" | "fuzzy" | "none",
            "status": "confident" | "uncertain" | "missing",
        }
    """
    lesson_code = extract_code_token(lesson_text)

    # --- Pass 1: code match (strongest signal) ---
    if lesson_code:
        for pdf_path in candidate_pdfs:
            if extract_code_token(pdf_path.stem) == lesson_code:
                return {
                    "lesson": lesson_text,
                    "match": pdf_path,
                    "score": 1.0,
                    "method": "code",
                    "status": "confident",
                }

    # --- Pass 2: fuzzy text match (fallback) ---
    lesson_norm = normalize_text(lesson_text)
    best_pdf = None
    best_score = 0.0
    for pdf_path in candidate_pdfs:
        score = difflib.SequenceMatcher(None, lesson_norm, normalize_text(pdf_path.stem)).ratio()
        if score > best_score:
            best_score = score
            best_pdf = pdf_path

    if best_pdf is not None and best_score >= FUZZY_CONFIDENT_THRESHOLD:
        return {
            "lesson": lesson_text,
            "match": best_pdf,
            "score": best_score,
            "method": "fuzzy",
            "status": "confident",
        }
    if best_pdf is not None and best_score >= FUZZY_MINIMUM_THRESHOLD:
        return {
            "lesson": lesson_text,
            "match": best_pdf,
            "score": best_score,
            "method": "fuzzy",
            "status": "uncertain",
        }

    return {
        "lesson": lesson_text,
        "match": None,
        "score": 0.0,
        "method": "none",
        "status": "missing",
    }


def match_lessons(lessons: list, subject_folder) -> list:
    """
    Match every lesson in `lessons` (a list of lesson strings for one
    subject + exam period) against the actual PDF files sitting in
    `subject_folder` (a Path to that subject's downloaded-QBank folder).

    Returns a list of match-result dicts (see match_one_lesson), one per
    lesson, in the same order as `lessons` -- this order is what
    print_pack_builder.py uses to decide the page order in the merged
    print pack.
    """
    subject_folder = Path(subject_folder)
    if not subject_folder.exists():
        # No downloads at all for this subject -- every lesson is missing.
        return [
            {"lesson": lesson, "match": None, "score": 0.0, "method": "none", "status": "missing"}
            for lesson in lessons
        ]

    candidate_pdfs = sorted(p for p in subject_folder.iterdir() if p.suffix.lower() == ".pdf")
    return [match_one_lesson(lesson, candidate_pdfs) for lesson in lessons]