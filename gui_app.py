"""
gui_app.py

WHAT THIS DOES
--------------
A simple desktop window (built with Tkinter, which ships with Python --
no extra install needed) that wraps main.py's run_download_all_siblings()
function so someone with no Python/coding experience can use this tool:
type in their portal username/password and a date range, click Start,
and watch a live log of progress right in the window.

This file imports and calls the exact same logic as main.py (login,
filtering, pagination, downloading, sorting by subject, skip-if-already-
downloaded) -- nothing about how the download works is duplicated here.
This file is ONLY the user interface around it.

HOW THIS GETS TURNED INTO A DOUBLE-CLICKABLE .EXE
---------------------------------------------------
This script itself still needs Python installed to run directly. To turn
it into a single .exe that recipients can double-click with NO Python
installed at all, we use PyInstaller (see build_exe.bat / BUILD_EXE.md
in this same folder for the exact one-time command to run).

WHAT RECIPIENTS STILL NEED
---------------------------
- Google Chrome installed (any recent version -- the matching ChromeDriver
  is fetched automatically the first time the app runs, which needs an
  internet connection).
- Their own portal username and password, and the date range they want.
They do NOT need Python, pip, or any of the libraries this project uses --
PyInstaller bundles all of that into the .exe.
"""

import sys
import threading
import queue
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import main as downloader  # the actual login/scrape/download logic
from print_pack_gui import PrintPackWindow  # the "Build Print Packs" wizard window

# ---------------------------------------------------------------------------
# COLOR PALETTE -- pastel blue, white, and black text throughout.
# Kept as named constants (rather than scattered hex codes) so the whole
# look can be re-tuned from one place.
# ---------------------------------------------------------------------------
COLOR_BG = "#FFFFFF"            # main window / card background
COLOR_PANEL = "#F7FBFE"         # soft-blue tinted panel background
COLOR_BORDER = "#B5D4F4"        # pastel blue border
COLOR_BORDER_LIGHT = "#E6F1FB"  # lighter divider lines
COLOR_ACCENT = "#378ADD"        # primary button / accent blue
COLOR_ACCENT_HOVER = "#2C74BE"
COLOR_TEXT = "#000000"          # primary text -- black, per spec
COLOR_TEXT_MUTED = "#444441"    # secondary/label text
COLOR_TEXT_FOOTER = "#888780"   # quiet footer credit text

APP_NAME = "EntabDownloader"
FOOTER_CREDIT = "Brinda & Associates"


class TextRedirector:
    """
    A drop-in replacement for sys.stdout that pushes every print()'d line
    into a thread-safe queue instead of a terminal. The GUI's main thread
    polls this queue and appends new lines into the on-screen log widget.
    We need this because the download runs on a background thread (so the
    window doesn't freeze), and Tkinter widgets can only safely be updated
    from the main thread.
    """
    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue

    def write(self, text):
        if text:
            self.log_queue.put(text)

    def flush(self):
        pass  # required for file-like objects; nothing to do here


class EntabDownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("760x620")
        self.root.configure(bg=COLOR_BG)
        self.root.minsize(680, 560)

        # Titlebar/taskbar icon -- looks for entabdownloader.ico next to this
        # script when run directly, or in PyInstaller's extracted bundle
        # folder (sys._MEIPASS) when run as the built .exe. Not fatal if
        # it's missing either way.
        if hasattr(sys, "_MEIPASS"):
            icon_path = Path(sys._MEIPASS) / "entabdownloader.ico"
        else:
            icon_path = Path(__file__).resolve().parent / "entabdownloader.ico"
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except Exception:
                pass  # e.g. platform doesn't support .ico (non-Windows) -- just skip it

        self._setup_style()

        self.log_queue = queue.Queue()
        self.worker_thread = None

        # A queue-based way for the background thread to ask the main
        # thread to show a confirmation dialog (Tkinter dialogs must run
        # on the main thread, but the download runs on a worker thread).
        self.confirm_request_queue = queue.Queue()
        self.confirm_response_event = threading.Event()

        self._build_widgets()
        self.root.after(150, self._poll_log_queue)
        self.root.after(150, self._poll_confirm_queue)

    def _setup_style(self):
        """
        Configure ttk's theming to match the pastel blue / white / black
        palette. ttk widgets don't take direct color args like classic
        Tk widgets do -- styling goes through a named ttk.Style instead.
        """
        style = ttk.Style(self.root)
        # "clam" is the most style-able built-in theme across platforms;
        # the default Windows theme ignores most color overrides.
        style.theme_use("clam")

        style.configure("TFrame", background=COLOR_BG)
        style.configure("Panel.TFrame", background=COLOR_PANEL)

        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT_MUTED, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=("Segoe UI", 16, "bold"))
        style.configure("Subtitle.TLabel", background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=("Segoe UI", 10))
        style.configure("Footer.TLabel", background=COLOR_BG, foreground=COLOR_TEXT_FOOTER, font=("Segoe UI", 9))
        style.configure("SectionHeading.TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=("Segoe UI", 10, "bold"))

        style.configure(
            "TEntry",
            fieldbackground=COLOR_BG,
            foreground=COLOR_TEXT,
            bordercolor=COLOR_BORDER,
            lightcolor=COLOR_BORDER,
            darkcolor=COLOR_BORDER,
            padding=6,
        )

        style.configure(
            "Accent.TButton",
            background=COLOR_ACCENT,
            foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
            padding=(16, 9),
        )
        style.map("Accent.TButton", background=[("active", COLOR_ACCENT_HOVER), ("disabled", COLOR_BORDER)])

        style.configure(
            "Outline.TButton",
            background=COLOR_BG,
            foreground=COLOR_TEXT,
            bordercolor=COLOR_BORDER,
            font=("Segoe UI", 10),
            padding=(16, 9),
        )
        style.map("Outline.TButton", background=[("active", COLOR_PANEL)])

    def _build_widgets(self):
        outer = ttk.Frame(self.root, style="TFrame")
        outer.pack(fill="both", expand=True, padx=20, pady=16)

        # --- Header ---
        header = ttk.Frame(outer, style="TFrame")
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Question bank downloader", style="Subtitle.TLabel").pack(anchor="w")

        # --- Form panel ---
        panel = tk.Frame(outer, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER_LIGHT, highlightthickness=1)
        panel.pack(fill="x", pady=(0, 14))
        form = ttk.Frame(panel, style="Panel.TFrame")
        form.pack(fill="x", padx=16, pady=14)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="Portal username", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.username_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.username_var, width=26).grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(form, text="Portal password", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.password_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.password_var, show="*", width=26).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(form, text="From date (DD-MM-YYYY)", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(20, 0), pady=4)
        self.from_date_var = tk.StringVar(value="01-04-2026")
        ttk.Entry(form, textvariable=self.from_date_var, width=18).grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(form, text="To date (DD-MM-YYYY)", style="Panel.TLabel").grid(row=1, column=2, sticky="w", padx=(20, 0), pady=4)
        self.to_date_var = tk.StringVar(value="31-03-2027")
        ttk.Entry(form, textvariable=self.to_date_var, width=18).grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=4)

        # --- Buttons ---
        button_row = ttk.Frame(outer, style="TFrame")
        button_row.pack(fill="x", pady=(0, 12))

        self.start_button = ttk.Button(
            button_row, text="Start download", style="Accent.TButton", command=self._on_start_clicked
        )
        self.start_button.pack(side="left")

        self.open_folder_button = ttk.Button(
            button_row, text="Open downloads folder", style="Outline.TButton",
            command=self._open_output_folder, state="disabled",
        )
        self.open_folder_button.pack(side="left", padx=(10, 0))

        # "Build Print Packs" is independent of the download flow above --
        # it works on question banks that have ALREADY been downloaded, so
        # it's always clickable (not gated behind start_button's state).
        self.print_pack_button = ttk.Button(
            button_row, text="Build print packs...", style="Outline.TButton",
            command=self._open_print_pack_window,
        )
        self.print_pack_button.pack(side="left", padx=(10, 0))

        # --- Progress log ---
        ttk.Label(outer, text="Progress log", style="SectionHeading.TLabel").pack(anchor="w", pady=(0, 6))
        log_container = tk.Frame(outer, bg=COLOR_PANEL, highlightbackground=COLOR_BORDER_LIGHT, highlightthickness=1)
        log_container.pack(fill="both", expand=True, pady=(0, 12))
        self.log_widget = scrolledtext.ScrolledText(
            log_container, height=18, state="disabled", wrap="word",
            bg=COLOR_PANEL, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            relief="flat", font=("Consolas", 9), borderwidth=0, padx=10, pady=8,
        )
        self.log_widget.pack(fill="both", expand=True)

        # --- Footer ---
        footer = ttk.Frame(outer, style="TFrame")
        footer.pack(fill="x")
        ttk.Separator(footer).pack(fill="x", pady=(0, 8))
        ttk.Label(footer, text=FOOTER_CREDIT, style="Footer.TLabel").pack(anchor="center")

    # -----------------------------------------------------------------
    # Log queue polling (runs on the main thread)
    # -----------------------------------------------------------------
    def _poll_log_queue(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", text)
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log_queue)

    # -----------------------------------------------------------------
    # Confirmation-dialog bridging (background thread -> main thread)
    # -----------------------------------------------------------------
    def _poll_confirm_queue(self):
        try:
            message = self.confirm_request_queue.get_nowait()
            messagebox.showinfo("Manual step needed", message)
            self.confirm_response_event.set()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_confirm_queue)

    def _gui_confirm_callback(self, message: str):
        """
        Called from the background thread (via main.CONFIRM_CALLBACK) in
        place of input(). Blocks the background thread until the user has
        clicked OK on a message box shown by the main thread.
        """
        self.confirm_response_event.clear()
        self.confirm_request_queue.put(message)
        self.confirm_response_event.wait()

    # -----------------------------------------------------------------
    # Start button / worker thread
    # -----------------------------------------------------------------
    def _on_start_clicked(self):
        username = self.username_var.get().strip()
        password = self.password_var.get().strip()
        from_date = self.from_date_var.get().strip()
        to_date = self.to_date_var.get().strip()

        if not username or not password:
            messagebox.showwarning("Missing info", "Please enter both your username and password.")
            return
        if not from_date or not to_date:
            messagebox.showwarning("Missing info", "Please enter both a From Date and a To Date.")
            return

        self.start_button.configure(state="disabled")
        self.open_folder_button.configure(state="disabled")
        self._log(f"Starting download for user '{username}', {from_date} to {to_date} ...\n\n")

        self.worker_thread = threading.Thread(
            target=self._run_download_worker,
            args=(username, password, from_date, to_date),
            daemon=True,
        )
        self.worker_thread.start()

    def _log(self, text: str):
        self.log_queue.put(text)

    def _run_download_worker(self, username, password, from_date, to_date):
        # Redirect this thread's print() output into the log widget, and
        # route main.py's manual-step confirmations through a message box
        # instead of a console input() (there is no console here).
        original_stdout = sys.stdout
        original_confirm = downloader.CONFIRM_CALLBACK
        sys.stdout = TextRedirector(self.log_queue)
        downloader.CONFIRM_CALLBACK = self._gui_confirm_callback

        try:
            all_summaries = downloader.run_download_all_siblings(
                username=username,
                password=password,
                from_date=from_date,
                to_date=to_date,
                close_when_done=True,  # no console to wait for Enter in
            )
            self._log(f"\n\u2705 Finished for all children:\n")
            for child_name, summary in all_summaries.items():
                self._log(f"  {child_name}: {summary}\n")
        except Exception as e:
            self._log(f"\n\u274c An unexpected error occurred: {e}\n")
        finally:
            sys.stdout = original_stdout
            downloader.CONFIRM_CALLBACK = original_confirm
            self.root.after(0, self._on_worker_finished)

    def _on_worker_finished(self):
        self.start_button.configure(state="normal")
        self.open_folder_button.configure(state="normal")

    def _open_output_folder(self):
        folder = downloader.OUTPUT_ROOT.resolve()
        folder.mkdir(exist_ok=True)
        webbrowser.open(str(folder))

    # -----------------------------------------------------------------
    # "Build Print Packs" -- opens the separate wizard window that turns
    # already-downloaded Question Bank PDFs into combined, print-ready
    # packs grouped by subject + exam period, using the school's Annual
    # Portion PDF. See print_pack_gui.py for the full step-by-step flow.
    # -----------------------------------------------------------------
    def _open_print_pack_window(self):
        PrintPackWindow(self.root)


def main():
    root = tk.Tk()
    app = EntabDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()