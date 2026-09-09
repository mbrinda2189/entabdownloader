"""
print_pack_gui.py

WHAT THIS DOES
--------------
Provides PrintPackWindow, a Tkinter Toplevel window that turns already-
downloaded Question Bank PDFs into combined, print-ready packs grouped by
subject + exam period (Unit Test I/II/III/IV, Semester I/II), using the
school's Annual Portion PDF to know which lessons belong in which period.

This is opened from the main app window (gui_app.py) via a button; it
does NOT do any downloading itself -- it only works with PDFs that
main.py's download flow has already saved into QuestionBanks/.

THE FLOW
----------
1. SETUP -- mostly automatic. The window looks inside QuestionBanks/ for
   either subject folders directly (a single child / no sibling
   switcher) or one folder per child (siblings), and only asks which
   child if there's more than one to choose from. It also looks for the
   Annual Portion PDF that main.py's download flow now auto-downloads
   for each child (see main.py's download_annual_portion()) and uses it
   automatically -- a manual "upload a different PDF" option is still
   there as a fallback for the first run, or if the school hasn't
   posted one yet.

2. REVIEW SYLLABUS -- shows what annual_portion_parser.py extracted from
   the PDF so the parent can fix any lesson that got split oddly before
   it's used for matching (PDF table extraction is never 100% perfect --
   see annual_portion_parser.py's docstring). This gets cached next to
   the PDF (keyed by the PDF's content, not its name) so re-running
   later on an unchanged Annual Portion skips straight past this step.

3. SUBJECT-TO-FOLDER MATCHING -- runs silently in the background using a
   strict exact/contains/shared-word rule (see _resolve_subject_folder).
   A parent only sees a screen for this if a subject is genuinely
   ambiguous (more than one folder could plausibly be it) -- which
   didn't happen for the real annual portion this was built against, but
   could for a different class/school's document.

4. HOME -- six buttons, one per exam period (using whatever period names
   actually came out of the annual portion's header, not hardcoded).
   Nothing is computed until a button is clicked.

5. PERIOD -- clicking a period button matches lessons to downloaded PDFs
   for JUST that period, across every mapped subject, right then (lazy,
   not upfront for all six). If anything needs a human's eyes (an
   uncertain match), it's shown here, scoped to this one period, before
   building. A parent's choices for a period are remembered for the rest
   of this wizard session, so rebuilding the same period later (e.g.
   after downloading more Q.Bank PDFs) doesn't re-ask about lessons that
   were already sorted out, unless the PDF they'd picked no longer
   exists.
"""

import hashlib
import json
import shutil
import threading
import queue
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

import annual_portion_parser
import lesson_matcher
import print_pack_builder


# Reuse the same pastel-blue palette as gui_app.py so this window looks
# like part of the same app. Kept as a local copy (rather than imported)
# so this file has no import-time dependency on gui_app.py / Selenium.
COLOR_BG = "#FFFFFF"
COLOR_PANEL = "#F7FBFE"
COLOR_BORDER = "#B5D4F4"
COLOR_BORDER_LIGHT = "#E6F1FB"
COLOR_ACCENT = "#378ADD"
COLOR_ACCENT_HOVER = "#2C74BE"
COLOR_TEXT = "#000000"
COLOR_TEXT_MUTED = "#444441"
COLOR_DANGER = "#B23A3A"
COLOR_SUCCESS = "#1E8449"


# ---------------------------------------------------------------------------
# FOLDER / ANNUAL-PORTION DETECTION
# ---------------------------------------------------------------------------

def _looks_like_subject_folder(path: Path) -> bool:
    """A downloaded SUBJECT folder holds Question Bank PDFs directly. A
    CHILD folder (when siblings are involved) holds subject folders
    instead, not PDFs directly -- EXCEPT that it now also holds the
    auto-downloaded '_AnnualPortion.pdf' directly (see main.py's
    ANNUAL_PORTION_BASENAME), which would otherwise look exactly like a
    subject folder's single PDF and confuse this check. So that one
    specific file is excluded here: only a REAL Q.Bank PDF counts."""
    try:
        return any(
            p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith("_AnnualPortion")
            for p in path.iterdir()
        )
    except OSError:
        return False


def _detect_child_roots(qbank_search_root: Path) -> list:
    """
    Figure out whether qbank_search_root directly contains one folder
    per SUBJECT (no sibling switcher was used -- a single child) or one
    folder per CHILD, each of which itself contains subject folders
    (siblings) -- matching the two layouts main.py's
    run_download_all_siblings() can produce.

    Returns a list of "child root" Paths to choose from. A single-item
    list means there's nothing to ask the parent about; an empty list
    means qbank_search_root doesn't look like a Question Banks folder at
    all (wrong location, or nothing downloaded yet).
    """
    if not qbank_search_root.is_dir():
        return []
    subitems = [p for p in qbank_search_root.iterdir() if p.is_dir()]
    if not subitems:
        return []

    # If qbank_search_root's own subfolders directly contain PDFs,
    # qbank_search_root itself IS the child root (single child).
    if any(_looks_like_subject_folder(p) for p in subitems):
        return [qbank_search_root]

    # Otherwise, each subfolder that itself contains subject-looking
    # subfolders is one child.
    children = [
        p for p in subitems
        if any(_looks_like_subject_folder(q) for q in p.iterdir() if q.is_dir())
    ]
    return children


def _find_annual_portion_file(child_root: Path):
    """
    Look for the Annual Portion file main.py's download_annual_portion()
    auto-downloads (see ANNUAL_PORTION_BASENAME in main.py) directly
    inside child_root. Returns the Path if a PDF match is found, else
    None -- a match with some OTHER extension is intentionally not
    returned here (annual_portion_parser.py only understands PDFs); the
    caller surfaces that as a warning rather than silently ignoring it.
    """
    matches = sorted(child_root.glob("_AnnualPortion*"))
    for m in matches:
        if m.is_file() and m.suffix.lower() == ".pdf":
            return m
    return None


def _file_hash(path: Path) -> str:
    """Short content hash of the annual portion PDF, used to recognize
    'this is the same PDF as last time' for the reviewed-syllabus cache
    -- keyed by content rather than filename/path, so it still works
    with the fixed '_AnnualPortion.pdf' name that gets overwritten every
    download run: if the school posts an updated document, the content
    (and therefore the hash) changes and the cache correctly misses."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def _reviewed_cache_path(pdf_path: Path) -> Path:
    return pdf_path.with_name(f"{pdf_path.stem}_{_file_hash(pdf_path)}_reviewed.json")


# ---------------------------------------------------------------------------
# SUBJECT -> FOLDER RESOLUTION
# ---------------------------------------------------------------------------

def _resolve_subject_folder(subject: str, available_folders: list):
    """
    Decide which downloaded folder (if any) a subject from the annual
    portion corresponds to. Returns a (status, value) pair:

        ("resolved", "ENGLISH")           -- confident, single match
        ("ambiguous", ["A", "B"])         -- more than one plausible folder
        ("none", None)                    -- no folder matches at all
                                              (usually means: not downloaded yet)

    IMPORTANT: this deliberately does NOT use a generic fuzzy-ratio
    score. Testing showed unrelated pairs like "GEOGRAPHY" vs "PHYSICS"
    or "HISTORY & CIVICS" vs "CHEMISTRY" can score HIGHER than the
    genuinely correct pairing on short strings, purely from coincidental
    character overlap -- a confidently-wrong guess is worse than none.
    So this only resolves automatically on an exact match, one name
    containing the other, or a shared distinctive word/stem (e.g.
    "MATHS" / "MATHEMATICS"). Anything with more than one such candidate
    is reported as "ambiguous" rather than guessed at silently.
    """
    if not available_folders:
        return ("none", None)

    subject_norm = lesson_matcher.normalize_text(subject)
    subject_words = {w for w in subject_norm.split() if len(w) >= 4}

    # 1. Exact match after normalizing.
    exact = [f for f in available_folders if lesson_matcher.normalize_text(f) == subject_norm]
    if exact:
        return ("resolved", exact[0])

    # 2. One name fully contains the other.
    contains = [
        f for f in available_folders
        if lesson_matcher.normalize_text(f)
        and (lesson_matcher.normalize_text(f) in subject_norm or subject_norm in lesson_matcher.normalize_text(f))
    ]
    if len(contains) == 1:
        return ("resolved", contains[0])
    if len(contains) > 1:
        return ("ambiguous", contains)

    # 3. A shared word, or a shared 4-letter stem for near-identical
    #    words (e.g. "MATHS" / "MATHEMATICS" both start "math").
    stem_matches = []
    for folder in available_folders:
        folder_words = {w for w in lesson_matcher.normalize_text(folder).split() if len(w) >= 4}
        if any(sw == fw or sw[:4] == fw[:4] for sw in subject_words for fw in folder_words):
            stem_matches.append(folder)
    if len(stem_matches) == 1:
        return ("resolved", stem_matches[0])
    if len(stem_matches) > 1:
        return ("ambiguous", stem_matches)

    return ("none", None)


# ---------------------------------------------------------------------------
# MISFILED-PDF DETECTION (subject-tagging mistakes made on the portal side)
# ---------------------------------------------------------------------------
#
# Confirmed real failure mode (via a parent's own portal export): whoever
# uploads a Q.Bank on the school's side can leave the "Subject" dropdown
# on whatever it was last set to, so e.g. a Physics chapter or an English
# poem can get posted with Subject Name = "Mathematics". main.py sorts
# downloads strictly by that Subject Name column (it has no way to know
# the title disagrees with it), so the mistake carries straight through
# into the downloaded folder structure -- the file ends up sitting in
# MATHEMATICS with a filename that clearly says "PHYS" or "ENG".
#
# This scans already-downloaded filenames for exactly that disagreement
# and surfaces it on the wizard's home screen, so a parent doesn't have
# to notice it by eyeballing a 60+ row portal export by hand.

# Common filler words that show up in Q.Bank filenames across every
# subject and carry no information about which subject a file belongs
# to. Excluded so they can never coincidentally "match" a folder name.
_FILENAME_STOPWORDS = {"date", "class", "question", "bank", "school", "the"}


def _folder_signal_words(folder_name: str) -> set:
    """Words (3+ letters) from a folder's name, for matching against a
    filename's words."""
    return {w for w in lesson_matcher.normalize_text(folder_name).split() if len(w) >= 3}


def _filename_signal_words(filename_stem: str) -> set:
    """Words (3+ letters, filler words excluded) from a downloaded PDF's
    filename, for matching against folder names. The 3-letter minimum
    (not 4) matters here specifically: real subject abbreviations seen
    in actual Q.Bank titles include 3-letter ones -- ENG, BIO, GEO --
    alongside 4-letter ones like PHYS/CHEM/HIST, and excluding the
    3-letter ones would silently miss exactly the kind of misfiled file
    this function exists to catch."""
    words = lesson_matcher.normalize_text(filename_stem).split()
    return {w for w in words if len(w) >= 3 and w not in _FILENAME_STOPWORDS}


def _words_match(a: set, b: set) -> bool:
    """True if any word in `a` matches any word in `b`, checked two
    ways since one rule alone can't cover both real-world patterns seen
    in actual Q.Bank titles:
      - a short abbreviation is a genuine prefix of the full word
        ('bio' -> 'biology', 'phys' -> 'physics', 'eng' -> 'english')
      - two words share a stem but diverge after it, so neither is a
        clean prefix of the other ('maths' vs 'mathematics' -- both
        start 'math' but 'maths' itself isn't a prefix of
        'mathematics', since the 5th letter differs: 's' vs 'e')
    A minimum prefix length of 3 (rule 1) / requiring both words to be
    4+ letters (rule 2) avoids coincidental matches on short fragments.
    """
    for wa in a:
        for wb in b:
            if wa == wb:
                return True
            shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
            if len(shorter) >= 3 and longer.startswith(shorter):
                return True
            if len(wa) >= 4 and len(wb) >= 4 and wa[:4] == wb[:4]:
                return True
    return False


def find_misfiled_pdfs(child_root: Path, known_subjects: list = None) -> list:
    """
    Scan every downloaded PDF across every subject folder in child_root
    and flag any whose filename's words point to a DIFFERENT subject
    than the folder it's actually sitting in.

    `known_subjects` (optional): subject names from the parsed Annual
    Portion (e.g. "PHYSICS"), considered as possible destinations IN
    ADDITION to folders that already exist on disk. This matters for a
    subject that's ENTIRELY missing from the downloads because every
    single one of its postings got mistagged under another subject on
    the portal side -- with no existing folder for a subject, there'd
    otherwise be nothing for the word-matching to point to, and the
    mismatch would go completely undetected even though it's the exact
    case a parent most needs to be told about (a subject reported as
    "not yet downloaded" that's actually sitting misfiled elsewhere).

    Returns a list of dicts: {"file": Path, "current_folder": str,
    "suggested_folder": str}. `suggested_folder` may not exist on disk
    yet -- the caller (print_pack_gui's move handler) creates it as
    needed. Deliberately does not move anything itself; the wizard's
    home screen offers a one-click "Move" button per flagged file so
    the parent confirms each move rather than having files silently
    relocated on a heuristic guess.
    """
    child_root = Path(child_root)
    subject_folders = [p for p in child_root.iterdir() if p.is_dir() and _looks_like_subject_folder(p)]
    words_by_folder = {f.name: _folder_signal_words(f.name) for f in subject_folders}

    candidate_words = dict(words_by_folder)
    if known_subjects:
        for subject in known_subjects:
            if subject not in candidate_words:
                words = _folder_signal_words(subject)
                if words:
                    candidate_words[subject] = words

    flags = []
    for folder in subject_folders:
        own_words = words_by_folder[folder.name]
        for pdf in sorted(folder.glob("*.pdf")):
            file_words = _filename_signal_words(pdf.stem)
            if not file_words:
                continue  # nothing usable in this filename either way
            if _words_match(file_words, own_words):
                continue  # filename agrees with the folder it's already in

            for other_name, other_words in candidate_words.items():
                if other_name == folder.name:
                    continue
                if _words_match(file_words, other_words):
                    flags.append({"file": pdf, "current_folder": folder.name, "suggested_folder": other_name})
                    break  # one suggestion is enough per file

    return flags


class PrintPackWindow(tk.Toplevel):
    """The wizard window. Each step's UI is built into self.container,
    which is cleared and rebuilt when moving between steps -- simpler
    and more robust than trying to show/hide a stack of pre-built
    frames."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Build Print Packs")
        self.geometry("860x660")
        self.minsize(720, 520)
        self.configure(bg=COLOR_BG)

        # --- state carried between steps ---
        self.qbank_search_root: Path = Path.cwd() / "QuestionBanks"
        self.child_root: Path | None = None
        self.child_label: str = ""
        self.pdf_path: Path | None = None
        self.parsed: dict | None = None
        self.subject_folder_map: dict = {}       # subject -> Path or None
        self._ambiguous_subjects: dict = {}       # subject -> [candidate folder names]
        self._unresolved_subjects: list = []      # subjects with no downloaded folder at all

        # Per-period state, built up lazily as periods are visited/built.
        self.period_built_paths: dict = {}        # period -> output_dir (once built)
        self._period_override_cache: dict = {}    # period -> {(subject, lesson): filename or None}

        self._setup_style()

        header = ttk.Frame(self, style="TFrame")
        header.pack(fill="x", padx=16, pady=(12, 0))
        self.step_label = ttk.Label(header, text="", style="Title.TLabel")
        self.step_label.pack(anchor="w")

        self.container = ttk.Frame(self, style="TFrame")
        self.container.pack(fill="both", expand=True, padx=16, pady=12)

        self._show_step_setup()

    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Panel.TFrame", background=COLOR_PANEL)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT_MUTED, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=("Segoe UI", 14, "bold"))
        style.configure("Danger.TLabel", background=COLOR_BG, foreground=COLOR_DANGER, font=("Segoe UI", 9))
        style.configure("Success.TLabel", background=COLOR_BG, foreground=COLOR_SUCCESS, font=("Segoe UI", 9, "bold"))
        style.configure("Link.TLabel", background=COLOR_BG, foreground=COLOR_ACCENT, font=("Segoe UI", 9, "underline"))
        style.configure("Accent.TButton", background=COLOR_ACCENT, foreground="#FFFFFF",
                         font=("Segoe UI", 10, "bold"), borderwidth=0, padding=(14, 8))
        style.map("Accent.TButton", background=[("active", COLOR_ACCENT_HOVER)])
        style.configure("Outline.TButton", background=COLOR_BG, foreground=COLOR_TEXT,
                         bordercolor=COLOR_BORDER, font=("Segoe UI", 10), padding=(14, 8))
        style.configure("Period.TButton", background=COLOR_PANEL, foreground=COLOR_TEXT,
                         bordercolor=COLOR_BORDER, font=("Segoe UI", 11, "bold"), padding=(18, 22))
        style.map("Period.TButton", background=[("active", COLOR_BORDER_LIGHT)])

    def _clear_container(self):
        for child in self.container.winfo_children():
            child.destroy()

    # =====================================================================
    # STEP -- SETUP: auto-detect child + annual portion
    # =====================================================================

    def _show_step_setup(self):
        self.step_label.configure(text="Step 1 — Child & annual portion")
        self._clear_container()

        candidates = _detect_child_roots(self.qbank_search_root)

        location_row = ttk.Frame(self.container, style="TFrame")
        location_row.pack(fill="x", pady=(0, 14))
        ttk.Label(location_row, text=f"Looking in: {self.qbank_search_root}", style="TLabel").pack(side="left")
        ttk.Button(location_row, text="Change folder...", style="Outline.TButton",
                   command=self._browse_qbank_search_root).pack(side="left", padx=(10, 0))

        if not candidates:
            ttk.Label(
                self.container,
                text="No downloaded Question Banks found in that folder. Use \"Change folder...\" "
                     "above to point at the right one (the folder that contains ENGLISH, MATHS, "
                     "etc. -- or a folder of per-child folders, if you have more than one child).",
                style="Danger.TLabel", wraplength=780, justify="left",
            ).pack(anchor="w", pady=(0, 14))
            self.child_root = None
        elif len(candidates) == 1:
            self.child_root = candidates[0]
            self.child_label = self.child_root.name
            ttk.Label(self.container, text=f"Using: {self.child_root}", style="TLabel").pack(anchor="w", pady=(0, 14))
        else:
            ttk.Label(self.container, text="More than one child was found -- which one?",
                       style="TLabel").pack(anchor="w", pady=(0, 6))
            self.child_choice_var = tk.StringVar(
                value=self.child_root.name if self.child_root in candidates else candidates[0].name
            )
            combo = ttk.Combobox(self.container, textvariable=self.child_choice_var,
                                  values=[c.name for c in candidates], state="readonly", width=40)
            combo.pack(anchor="w", pady=(0, 14))
            self._child_candidates = {c.name: c for c in candidates}
            combo.bind("<<ComboboxSelected>>",
                       lambda e: self._on_child_selected(self._child_candidates[self.child_choice_var.get()]))
            self.child_root = self._child_candidates[self.child_choice_var.get()]
            self.child_label = self.child_root.name

        # --- Annual portion: auto-detected, with a manual fallback ---
        ttk.Separator(self.container).pack(fill="x", pady=(4, 14))

        self.pdf_status_label = ttk.Label(self.container, text="", style="TLabel", wraplength=780, justify="left")
        self.pdf_status_label.pack(anchor="w", pady=(0, 6))

        pdf_row = ttk.Frame(self.container, style="TFrame")
        pdf_row.pack(fill="x", pady=(0, 14))
        self.pdf_var = tk.StringVar(value="")
        self.pdf_entry = ttk.Entry(pdf_row, textvariable=self.pdf_var)
        self.pdf_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(pdf_row, text="Browse...", style="Outline.TButton",
                   command=self._browse_pdf).pack(side="left", padx=(8, 0))

        self._refresh_pdf_detection()

        button_row = ttk.Frame(self.container, style="TFrame")
        button_row.pack(anchor="w", pady=(6, 0))
        ttk.Button(button_row, text="Next: Review syllabus", style="Accent.TButton",
                   command=self._on_setup_next).pack(side="left")

    def _on_child_selected(self, new_root: Path):
        self.child_root = new_root
        self.child_label = new_root.name
        self._refresh_pdf_detection()

    def _browse_qbank_search_root(self):
        folder = filedialog.askdirectory(title="Select your QuestionBanks folder")
        if folder:
            self.qbank_search_root = Path(folder)
            self.child_root = None
            self._show_step_setup()

    def _refresh_pdf_detection(self):
        """Look for the auto-downloaded Annual Portion inside the
        currently selected child_root and update the status label + the
        entry field accordingly. Called on setup and whenever the
        selected child changes."""
        if self.child_root is None:
            self.pdf_status_label.configure(text="")
            return

        found = _find_annual_portion_file(self.child_root)
        if found:
            self.pdf_path = found
            self.pdf_var.set(str(found))
            self.pdf_status_label.configure(
                text=f"Using the auto-downloaded Annual Portion for {self.child_label}. "
                     f"If this looks wrong or out of date, browse for a different PDF below.",
                style="TLabel",
            )
        else:
            self.pdf_path = None
            self.pdf_var.set("")
            self.pdf_status_label.configure(
                text=f"No auto-downloaded Annual Portion found yet for {self.child_label} "
                     f"(the school may not have posted one, or you haven't re-run the "
                     f"downloader since this feature was added). Upload the PDF manually below.",
                style="Danger.TLabel",
            )

    def _browse_pdf(self):
        path = filedialog.askopenfilename(title="Select the Annual Portion PDF",
                                           filetypes=[("PDF files", "*.pdf")])
        if path:
            self.pdf_var.set(path)
            self.pdf_path = Path(path)
            self.pdf_status_label.configure(text="Using a manually selected PDF.", style="TLabel")

    def _on_setup_next(self):
        if self.child_root is None:
            messagebox.showwarning("No folder selected", "Please choose a valid Question Banks folder first.")
            return
        pdf_text = self.pdf_var.get().strip()
        if not pdf_text or not Path(pdf_text).is_file():
            messagebox.showwarning("Missing PDF", "Please select the Annual Portion PDF (auto-detected or browsed).")
            return
        self.pdf_path = Path(pdf_text)

        # Offer the cached reviewed version if one exists for this exact
        # PDF content (see _reviewed_cache_path -- keyed by a hash of the
        # file's content, so an updated Annual Portion correctly misses
        # the cache and gets re-parsed/re-reviewed) AND was produced by
        # the CURRENT parsing logic. A cache from before a parsing fix
        # (like the "Chapter:1" colon-separator fix) must NOT be reused
        # silently -- that would keep serving the old, wrongly-split
        # result forever even after the code that produced it is gone.
        cache_path = _reviewed_cache_path(self.pdf_path)
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if cached.get("_parser_version") == annual_portion_parser.PARSER_VERSION:
                    self.parsed = cached
                    self._resolve_all_subject_mappings()
                    return
                # else: cache predates a parsing fix -- fall through and re-parse fresh
            except Exception:
                pass  # fall through and re-parse from scratch

        try:
            self.parsed = annual_portion_parser.parse_annual_portion_pdf(self.pdf_path)
        except Exception as e:
            messagebox.showerror("Couldn't read PDF", f"Failed to parse the annual portion PDF:\n{e}")
            return

        self._show_step_review()

    # =====================================================================
    # STEP -- REVIEW: let the parent fix the extracted syllabus text
    # =====================================================================

    def _show_step_review(self):
        self.step_label.configure(text="Step 2 — Review the extracted syllabus")
        self._clear_container()

        info_bits = []
        if self.parsed["skipped_subjects"]:
            info_bits.append(
                "Skipped (language subjects, not handled by this tool): "
                + ", ".join(self.parsed["skipped_subjects"])
            )
        if self.parsed["warnings"]:
            info_bits.append("Warnings:\n" + "\n".join(f"  - {w}" for w in self.parsed["warnings"]))
        if info_bits:
            ttk.Label(self.container, text="\n".join(info_bits), style="Danger.TLabel",
                       justify="left", wraplength=780).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            self.container,
            text="Click a subject/period on the left to see and edit its lessons on the right "
                 "(one lesson per line). Fix anything that got split oddly before continuing.",
            style="TLabel", wraplength=780, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        body = ttk.Frame(self.container, style="TFrame")
        body.pack(fill="both", expand=True)

        tree_frame = ttk.Frame(body, style="TFrame")
        tree_frame.pack(side="left", fill="y", padx=(0, 10))
        self.review_tree = ttk.Treeview(tree_frame, columns=("count",), show="tree headings", height=20)
        self.review_tree.heading("#0", text="Subject / Period")
        self.review_tree.heading("count", text="Lessons")
        self.review_tree.column("count", width=60, anchor="center")
        self.review_tree.pack(side="left", fill="y")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.review_tree.yview)
        tree_scroll.pack(side="left", fill="y")
        self.review_tree.configure(yscrollcommand=tree_scroll.set)

        self._review_tree_items = {}
        for subject, periods in self.parsed["subjects"].items():
            subject_item = self.review_tree.insert("", "end", text=subject, open=False)
            for period, lessons in periods.items():
                item = self.review_tree.insert(subject_item, "end", text=period, values=(len(lessons),))
                self._review_tree_items[item] = (subject, period)
        self.review_tree.bind("<<TreeviewSelect>>", self._on_review_select)

        right = ttk.Frame(body, style="TFrame")
        right.pack(side="left", fill="both", expand=True)
        self.review_editor_label = ttk.Label(right, text="Select a subject/period on the left.", style="TLabel")
        self.review_editor_label.pack(anchor="w")
        self.review_text = scrolledtext.ScrolledText(right, wrap="word", font=("Segoe UI", 10), height=20)
        self.review_text.pack(fill="both", expand=True, pady=(4, 0))
        self._current_review_key = None

        button_row = ttk.Frame(self.container, style="TFrame")
        button_row.pack(fill="x", pady=(10, 0))
        ttk.Button(button_row, text="Back", style="Outline.TButton",
                   command=self._show_step_setup).pack(side="left")
        ttk.Button(button_row, text="Next", style="Accent.TButton",
                   command=self._on_review_next).pack(side="left", padx=(8, 0))

    def _on_review_select(self, event):
        selection = self.review_tree.selection()
        if not selection:
            return
        item = selection[0]
        if item not in self._review_tree_items:
            return

        self._save_current_review_edits()

        subject, period = self._review_tree_items[item]
        self._current_review_key = (subject, period)
        self.review_editor_label.configure(text=f"{subject} — {period}  (one lesson per line)")
        self.review_text.delete("1.0", "end")
        self.review_text.insert("1.0", "\n".join(self.parsed["subjects"][subject][period]))

    def _save_current_review_edits(self):
        if self._current_review_key is None:
            return
        subject, period = self._current_review_key
        raw = self.review_text.get("1.0", "end")
        lessons = [line.strip() for line in raw.splitlines() if line.strip()]
        self.parsed["subjects"][subject][period] = lessons
        for item, key in self._review_tree_items.items():
            if key == self._current_review_key:
                self.review_tree.set(item, "count", len(lessons))

    def _on_review_next(self):
        self._save_current_review_edits()

        try:
            cache_path = _reviewed_cache_path(self.pdf_path)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(self.parsed, f, indent=2, ensure_ascii=False)
        except Exception:
            pass  # caching is a convenience, not essential -- don't block on it

        self._resolve_all_subject_mappings()

    # =====================================================================
    # SUBJECT -> FOLDER MAPPING (silent unless genuinely ambiguous)
    # =====================================================================

    def _resolve_all_subject_mappings(self):
        available_folders = sorted(p.name for p in self.child_root.iterdir() if p.is_dir())

        self.subject_folder_map = {}
        self._ambiguous_subjects = {}
        self._unresolved_subjects = []

        for subject in self.parsed["subjects"]:
            status, value = _resolve_subject_folder(subject, available_folders)
            if status == "resolved":
                self.subject_folder_map[subject] = self.child_root / value
            elif status == "ambiguous":
                self._ambiguous_subjects[subject] = value
                self.subject_folder_map[subject] = None
            else:
                self._unresolved_subjects.append(subject)
                self.subject_folder_map[subject] = None

        if self._ambiguous_subjects:
            self._show_step_ambiguous_mapping()
        else:
            self._show_step_home()

    def _show_step_ambiguous_mapping(self):
        """
        Only ever shown when a subject has MORE THAN ONE plausible
        downloaded folder (see _resolve_subject_folder) -- most annual
        portions won't trigger this screen at all.
        """
        self.step_label.configure(text="A couple of subjects need a quick check")
        self._clear_container()

        ttk.Label(
            self.container,
            text="These subjects could match more than one downloaded folder -- pick the "
                 "right one for each (or \"-- skip --\" if you don't want a pack for it).",
            style="TLabel", wraplength=780, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        self._ambiguous_vars = {}
        for subject, candidates in self._ambiguous_subjects.items():
            row = ttk.Frame(self.container, style="TFrame")
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=subject, style="TLabel", width=28).pack(side="left")
            var = tk.StringVar(value="-- skip --")
            combo = ttk.Combobox(row, textvariable=var, values=["-- skip --"] + candidates,
                                  state="readonly", width=30)
            combo.pack(side="left", padx=(8, 0))
            self._ambiguous_vars[subject] = var

        button_row = ttk.Frame(self.container, style="TFrame")
        button_row.pack(fill="x", pady=(14, 0))
        ttk.Button(button_row, text="Back", style="Outline.TButton",
                   command=self._show_step_review).pack(side="left")
        ttk.Button(button_row, text="Next", style="Accent.TButton",
                   command=self._on_ambiguous_next).pack(side="left", padx=(8, 0))

    def _on_ambiguous_next(self):
        for subject, var in self._ambiguous_vars.items():
            value = var.get()
            self.subject_folder_map[subject] = None if value == "-- skip --" else self.child_root / value
        self._show_step_home()

    # =====================================================================
    # HOME -- six period buttons
    # =====================================================================

    def _show_step_home(self):
        self.step_label.configure(text=f"{self.child_label}")
        self._clear_container()

        top_row = ttk.Frame(self.container, style="TFrame")
        top_row.pack(fill="x", pady=(0, 4))
        ttk.Label(top_row, text=f"Annual portion: {self.pdf_path.name}", style="TLabel").pack(side="left")
        ttk.Button(top_row, text="Change child / annual portion...", style="Outline.TButton",
                   command=self._show_step_setup).pack(side="left", padx=(10, 0))

        if self._unresolved_subjects:
            ttk.Label(
                self.container,
                text="Not yet downloaded, so left out for now: " + ", ".join(self._unresolved_subjects),
                style="Danger.TLabel", wraplength=800, justify="left",
            ).pack(anchor="w", pady=(6, 0))

        self._misfiled_flags = find_misfiled_pdfs(self.child_root, known_subjects=list(self.parsed["subjects"].keys()))
        if self._misfiled_flags:
            warn_frame = tk.Frame(self.container, bg=COLOR_PANEL,
                                   highlightbackground=COLOR_BORDER_LIGHT, highlightthickness=1)
            warn_frame.pack(fill="x", pady=(10, 0))
            ttk.Label(
                warn_frame,
                text=f"{len(self._misfiled_flags)} file(s) look like they might be filed under the wrong "
                     "subject (this can happen when the school's portal has the wrong subject selected "
                     "at upload time). Move them if the suggestion looks right:",
                style="Panel.TLabel", wraplength=800, justify="left",
            ).pack(anchor="w", padx=10, pady=(8, 4))
            for flag in self._misfiled_flags:
                row = ttk.Frame(warn_frame, style="Panel.TFrame")
                row.pack(fill="x", padx=10, pady=(0, 6))
                ttk.Label(
                    row, text=f"{flag['file'].name}   [{flag['current_folder']} \u2192 {flag['suggested_folder']}]",
                    style="Panel.TLabel", wraplength=620, justify="left",
                ).pack(side="left")
                ttk.Button(row, text=f"Move to {flag['suggested_folder']}", style="Outline.TButton",
                           command=lambda f=flag: self._move_misfiled_file(f)).pack(side="right")

        ttk.Label(
            self.container,
            text="Pick a period to build its print packs. Nothing is built until you click one.",
            style="TLabel",
        ).pack(anchor="w", pady=(10, 14))

        grid = ttk.Frame(self.container, style="TFrame")
        grid.pack(anchor="w")
        periods = self.parsed["periods"]
        columns = 3
        for i, period in enumerate(periods):
            label = period
            if period in self.period_built_paths:
                label = f"{period}\n\u2713 Built"
            btn = ttk.Button(grid, text=label, style="Period.TButton", width=22,
                              command=lambda p=period: self._show_step_period(p))
            btn.grid(row=i // columns, column=i % columns, padx=8, pady=8, sticky="nsew")

    def _move_misfiled_file(self, flag: dict):
        """Move a flagged file into its suggested folder (one-click fix
        from the home screen's warning panel), then recompute everything
        from scratch -- a newly-created subject folder (e.g. PHYSICS
        appearing for the first time) may resolve a subject that was
        previously reported as "not yet downloaded", and any cached
        period overrides may reference the file's old path, so a full
        recompute is safer than trying to patch state in place."""
        source = flag["file"]
        dest_folder = self.child_root / flag["suggested_folder"]
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest = dest_folder / source.name
        try:
            shutil.move(str(source), str(dest))
        except Exception as e:
            messagebox.showerror("Couldn't move file", f"Failed to move {source.name}:\n{e}")
            return

        self._period_override_cache = {}
        self._resolve_all_subject_mappings()

    # =====================================================================
    # PERIOD -- lazy match + (if needed) confirm + build, for ONE period
    # =====================================================================

    def _show_step_period(self, period: str):
        self.step_label.configure(text=period)
        self._clear_container()
        self._current_period = period

        mapped_subjects = {s: f for s, f in self.subject_folder_map.items() if f is not None}

        # Compute matches for this period only, right now (lazy) -- and
        # apply any override the parent already chose for this exact
        # period earlier in this session, as long as the file they chose
        # still exists (a fresh download run could have removed/renamed
        # it, in which case we fall back to fresh matching instead of
        # trusting a stale choice).
        period_matches = {}
        overrides = self._period_override_cache.get(period, {})
        for subject, folder in mapped_subjects.items():
            lessons = self.parsed["subjects"][subject][period]
            results = lesson_matcher.match_lessons(lessons, folder)
            for result in results:
                key = (subject, result["lesson"])
                if key in overrides:
                    chosen_name = overrides[key]
                    if chosen_name is None:
                        result["match"] = None
                        result["status"] = "missing"
                    else:
                        candidate_path = folder / chosen_name
                        if candidate_path.exists():
                            result["match"] = candidate_path
                            result["status"] = "confident"
                            result["method"] = "manual"
                            result["score"] = 1.0
                        # else: stale override, fall through to the fresh match already computed
            period_matches[subject] = results

        self._current_period_matches = period_matches

        needs_review = [
            (subject, result)
            for subject, results in period_matches.items()
            for result in results
            if result["status"] != "confident"
        ]

        if needs_review:
            self._render_period_confirm(period, needs_review)
        else:
            self._render_period_ready(period)

    def _render_period_confirm(self, period: str, needs_review: list):
        ttk.Label(
            self.container,
            text=f"{len(needs_review)} lesson(s) in {period} need a quick check. Pick the correct "
                 "PDF for each, or leave \"No match\" if it genuinely wasn't downloaded.",
            style="TLabel", wraplength=800, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        canvas_frame = ttk.Frame(self.container, style="TFrame")
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, bg=COLOR_BG, highlightthickness=0)
        scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        rows_frame = ttk.Frame(canvas, style="TFrame")
        rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

        self._confirm_vars = []
        for subject, result in needs_review:
            folder = self.subject_folder_map[subject]
            pdf_choices = ["No match"] + sorted(p.name for p in folder.iterdir() if p.suffix.lower() == ".pdf")

            row = ttk.Frame(rows_frame, style="Panel.TFrame")
            row.pack(fill="x", pady=3, padx=2)
            ttk.Label(row, text=f"[{subject}]  {result['lesson']}", style="Panel.TLabel",
                       wraplength=430, justify="left").pack(side="left", padx=(6, 10), pady=6)

            current_value = result["match"].name if result["match"] else "No match"
            var = tk.StringVar(value=current_value)
            combo = ttk.Combobox(row, textvariable=var, values=pdf_choices, state="readonly", width=38)
            combo.pack(side="left", padx=(0, 6), pady=6)
            if result["status"] == "uncertain":
                ttk.Label(row, text=f"(suggested, {result['score']:.0%} sure)", style="Panel.TLabel").pack(side="left")

            self._confirm_vars.append((subject, result, var, folder))

        button_row = ttk.Frame(self.container, style="TFrame")
        button_row.pack(fill="x", pady=(10, 0))
        ttk.Button(button_row, text="Back to periods", style="Outline.TButton",
                   command=self._show_step_home).pack(side="left")
        ttk.Button(button_row, text=f"Build {period}", style="Accent.TButton",
                   command=self._on_confirm_and_build).pack(side="left", padx=(8, 0))

    def _render_period_ready(self, period: str):
        ttk.Label(
            self.container,
            text=f"Every lesson in {period} matched confidently -- nothing needs review.",
            style="TLabel",
        ).pack(anchor="w", pady=(0, 14))

        button_row = ttk.Frame(self.container, style="TFrame")
        button_row.pack(fill="x")
        ttk.Button(button_row, text="Back to periods", style="Outline.TButton",
                   command=self._show_step_home).pack(side="left")
        ttk.Button(button_row, text=f"Build {period}", style="Accent.TButton",
                   command=self._on_confirm_and_build).pack(side="left", padx=(8, 0))

    def _on_confirm_and_build(self):
        period = self._current_period

        # Apply whatever the parent chose (if this period needed review)
        # back into the actual match-result dicts, and remember the
        # choice for the rest of this session so rebuilding this period
        # later doesn't re-ask about the same lessons.
        if hasattr(self, "_confirm_vars"):
            cache = self._period_override_cache.setdefault(period, {})
            for subject, result, var, folder in self._confirm_vars:
                choice = var.get()
                key = (subject, result["lesson"])
                if choice == "No match":
                    result["match"] = None
                    result["status"] = "missing"
                    cache[key] = None
                else:
                    result["match"] = folder / choice
                    result["status"] = "confident"
                    result["method"] = "manual"
                    result["score"] = 1.0
                    cache[key] = choice
            del self._confirm_vars

        self._show_step_building(period)

    # =====================================================================
    # BUILD -- runs print_pack_builder for this one period, with a log
    # =====================================================================

    def _show_step_building(self, period: str):
        self.step_label.configure(text=f"Building {period}...")
        self._clear_container()

        self.output_dir = self.qbank_search_root.parent / "PrintPacks" / self.child_label

        ttk.Label(self.container, text=f"Output folder: {self.output_dir}", style="TLabel").pack(
            anchor="w", pady=(0, 8)
        )

        self.build_log = scrolledtext.ScrolledText(
            self.container, wrap="word", state="disabled", font=("Consolas", 9), height=20
        )
        self.build_log.pack(fill="both", expand=True)

        self.build_button_row = ttk.Frame(self.container, style="TFrame")
        self.build_button_row.pack(fill="x", pady=(10, 0))
        self.back_button = ttk.Button(self.build_button_row, text="Back to periods", style="Outline.TButton",
                                       state="disabled", command=self._show_step_home)
        self.back_button.pack(side="left")
        self.open_folder_button = ttk.Button(self.build_button_row, text="Open output folder",
                                              style="Outline.TButton", state="disabled",
                                              command=self._open_output_folder)
        self.open_folder_button.pack(side="left", padx=(8, 0))

        self._log_queue = queue.Queue()
        self.after(150, self._poll_build_log)

        thread = threading.Thread(target=self._run_build_worker, args=(period,), daemon=True)
        thread.start()

    def _log(self, text: str):
        self._log_queue.put(text)

    def _poll_build_log(self):
        try:
            while True:
                text = self._log_queue.get_nowait()
                self.build_log.configure(state="normal")
                self.build_log.insert("end", text + "\n")
                self.build_log.see("end")
                self.build_log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll_build_log)

    def _run_build_worker(self, period: str):
        import sys

        class _Redirector:
            def __init__(self, put_fn):
                self.put_fn = put_fn

            def write(self, text):
                if text and text.strip():
                    self.put_fn(text.rstrip("\n"))

            def flush(self):
                pass

        original_stdout = sys.stdout
        sys.stdout = _Redirector(self._log)
        try:
            print_pack_builder.build_print_packs_for_period(period, self._current_period_matches, self.output_dir)
            self.period_built_paths[period] = self.output_dir
            self._log("\nDone.")
        except Exception as e:
            self._log(f"\nAn error occurred while building this period: {e}")
        finally:
            sys.stdout = original_stdout
            self.after(0, self._on_build_finished)

    def _on_build_finished(self):
        self.back_button.configure(state="normal")
        self.open_folder_button.configure(state="normal")

    def _open_output_folder(self):
        import webbrowser
        self.output_dir.mkdir(parents=True, exist_ok=True)
        webbrowser.open(str(self.output_dir.resolve()))