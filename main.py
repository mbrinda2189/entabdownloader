"""
entab_qbank_downloader/main.py

WHAT THIS SCRIPT DOES
----------------------
Logs into an Entab-based school portal (entab.online), navigates to the
"Student Assignment" page, filters for Question Bank ("Q.Bank") entries,
scrapes every row in the results grid, and downloads each attached file --
sorting the downloaded files into per-subject folders (e.g.
QuestionBanks/ENGLISH/, QuestionBanks/MATHEMATICS/, ...).

It keeps a small manifest (downloaded_manifest.json) of everything it has
already saved, so re-running the script will SKIP files that were already
downloaded instead of re-downloading them.

WHY IT SOMETIMES PAUSES AND ASKS YOU TO CLICK SOMETHING
---------------------------------------------------------
This script was written without being able to inspect the portal's live
HTML/JavaScript directly. Login forms and data grids differ from school to
school (and Entab deployments can be customized per school), so instead of
guessing exact element IDs and silently failing, the script:
  1. Tries a robust, generic way to find each element first.
  2. If it can't find something confidently, it opens a *visible* Chrome
     window, tells you exactly what to do, and waits for you to press
     Enter after you've done that one step by hand.
This "semi-automated" approach means the script is useful immediately,
and each manual step you do once tells us how to make it fully automatic
next time (just tell the assistant what happened and the selectors can be
hard-coded in for you).

HOW TO RUN
----------
1. Install dependencies:      pip install -r requirements.txt
2. Copy .env.example to .env and fill in your username/password.
3. Run:                       python main.py
4. Follow any on-screen prompts (date range, manual-step confirmations).

Downloaded files land in:     ./QuestionBanks/<SUBJECT NAME>/<Title>.pdf
A running log of what's been downloaded is kept in:
                               ./downloaded_manifest.json
"""

import os
import re
import sys
import json
import time
import argparse
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    NoSuchWindowException,
)
from webdriver_manager.chrome import ChromeDriverManager


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# Base URL of the school portal. Overridable via .env (ENTAB_BASE_URL) so the
# same script works for other Entab-hosted schools too.
#
# IMPORTANT: This must be the SCHOOL-SPECIFIC portal URL, found via the
# school's own website ("Web Portal Login" link) -- it looks like
# https://entab.online/<SCHOOLCODE> (e.g. https://entab.online/TVSMTN),
# NOT the generic https://entab.online/Logon/Index. The generic URL loads
# the page without the school code baked in, which breaks some of the
# page's own JavaScript and can make login silently fail.
DEFAULT_BASE_URL = "https://entab.online/TVSMTN"

# Where downloaded files get organized (one sub-folder per subject).
OUTPUT_ROOT = Path("QuestionBanks")

# Direct URL to the Student Assignment page, confirmed via manual navigation
# and dump_assignment_page_fields.py. Using this directly (rather than
# clicking sidebar icons) avoids all the flakiness of a busy, JS-heavy
# dashboard re-rendering mid-click.
DEFAULT_ASSIGNMENTS_URL = "https://entab.online/ParentPortal/ParentAssignment"

# File that remembers what's already been downloaded, so re-runs skip them.
MANIFEST_PATH = Path("downloaded_manifest.json")

# How long (seconds) to wait for slow page elements before giving up and
# asking the human for help.
WAIT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# SMALL UTILITIES
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """
    Turn an arbitrary title/subject string into something safe to use as a
    Windows/Mac/Linux file or folder name (strip characters like : / \\ * ?).
    """
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:150]  # keep filenames from getting absurdly long


def load_manifest() -> dict:
    """Load the record of already-downloaded items, or start a fresh one."""
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(manifest: dict) -> None:
    """Persist the manifest to disk after every download so progress is safe
    even if the script is interrupted partway through."""
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def already_downloaded(manifest: dict, subject: str, title: str) -> bool:
    """
    Check both the manifest AND the actual filesystem (in case the manifest
    was deleted but files remain) so we never re-download unnecessarily.
    """
    key = f"{subject}::{title}"
    if key in manifest:
        return True
    subject_folder = OUTPUT_ROOT / sanitize_filename(subject)
    if subject_folder.exists():
        safe_title = sanitize_filename(title)
        for existing_file in subject_folder.iterdir():
            if existing_file.stem.startswith(safe_title[:60]):
                return True
    return False


# Pluggable "wait for the human to confirm" function. Defaults to a plain
# console input() prompt (for command-line use). A GUI wrapper can replace
# this with a function that shows a message box instead, so the same core
# logic works unmodified from either a terminal or a graphical app.
CONFIRM_CALLBACK = lambda message: input(message)


def pause_for_manual_step(instruction: str) -> None:
    """
    Print a clear instruction, let the human perform one step by hand in the
    visible browser window, and wait for them to confirm before continuing.
    Uses CONFIRM_CALLBACK so this works the same from a console or a GUI.
    """
    print("\n" + "=" * 70)
    print("MANUAL STEP NEEDED")
    print(instruction)
    print("=" * 70)
    CONFIRM_CALLBACK("Press Enter here once you've done that in the browser window... ")


# ---------------------------------------------------------------------------
# BROWSER SETUP
# ---------------------------------------------------------------------------

def start_browser(headless: bool = False) -> webdriver.Chrome:
    """
    Launch Chrome via Selenium. webdriver-manager automatically downloads
    the ChromeDriver version that matches the Chrome installed on this
    machine, so no manual driver setup is required.

    We run NON-headless by default on purpose: if the script needs a manual
    step, you need to actually see and interact with the browser window.
    """
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    # Reduces noisy "Chrome is being controlled by automated software" bar
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------

def login(driver: webdriver.Chrome, base_url: str, username: str, password: str) -> None:
    """
    Log into the portal.

    Strategy: rather than guessing exact field IDs (which vary by school
    deployment), find the first visible text-type input on the page (that's
    almost always the username field) and the first password-type input
    (that's the password field), then find a submit-style button near them.
    This is more robust across different Entab skins than hard-coded IDs.
    """
    print(f"Opening {base_url} ...")
    driver.get(base_url)

    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        # Wait until at least one text input is present (login form loaded).
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input")))

        username_field = driver.find_element(
            By.CSS_SELECTOR, "input[type='text'], input:not([type])"
        )
        password_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")

        username_field.clear()
        username_field.send_keys(username)
        password_field.clear()
        password_field.send_keys(password)

        # Try common ways a login button is exposed.
        login_button = None
        for selector in [
            "button[type='submit']",
            "input[type='submit']",
            "button",
        ]:
            try:
                login_button = driver.find_element(By.CSS_SELECTOR, selector)
                break
            except NoSuchElementException:
                continue

        if login_button:
            login_button.click()
        else:
            # Fall back to pressing Enter in the password field.
            from selenium.webdriver.common.keys import Keys
            password_field.send_keys(Keys.RETURN)

        # Give the page a moment to redirect after login.
        time.sleep(3)

    except (TimeoutException, NoSuchElementException):
        pause_for_manual_step(
            "Could not find the username/password fields automatically.\n"
            "Please log in manually in the browser window that just opened."
        )
        return

    # Sanity check: did we actually get past the login page?
    if "logon" in driver.current_url.lower() or "login" in driver.current_url.lower():
        pause_for_manual_step(
            "It looks like login may not have succeeded (still on a login-looking URL).\n"
            "Please check the browser, log in manually if needed, and navigate to the\n"
            "Student Assignment page before continuing."
        )


# ---------------------------------------------------------------------------
# NAVIGATE TO THE ASSIGNMENT / QUESTION BANK PAGE
# ---------------------------------------------------------------------------

def navigate_to_assignments(driver: webdriver.Chrome, assignments_url: str = None) -> None:
    """
    Get to the 'Student Assignment' page.

    If assignments_url is provided (recommended -- it's the most reliable
    option), we just navigate straight there with driver.get(), which
    sidesteps all the flakiness of clicking sidebar icons on a busy,
    JS-heavy dashboard (this is what caused the earlier
    StaleElementReferenceException: the dashboard has several elements
    containing the word "assignment" -- a heading, a description, card
    items -- and the page kept re-rendering while we searched for one).

    If no direct URL is known, we fall back to clicking the sidebar icon
    (identified by its known image path, from the actual page HTML:
    /Images/ParentPortal/Icons/Assignments-icon2.png), re-locating it
    fresh right before clicking to avoid stale references. If that still
    fails, we ask the human to click it once.
    """
    if assignments_url:
        print(f"Navigating directly to {assignments_url} ...")
        driver.get(assignments_url)
        time.sleep(2)
        return

    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    # A couple of attempts, re-finding the element fresh each time, since
    # the dashboard's own JS can re-render parts of the page between our
    # find and our click (that's what caused the staleness before).
    for attempt in range(3):
        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "img[src*='Assignments-icon'], a[href*='Assignment']")
                )
            )
            # Re-find immediately before clicking (don't reuse an old reference).
            icon = driver.find_element(By.CSS_SELECTOR, "img[src*='Assignments-icon']")
            # Click the icon's clickable parent (the <a> or <li> wrapping it), since
            # the <img> itself may not have the click handler.
            clickable = icon
            try:
                clickable = icon.find_element(By.XPATH, "./ancestor::a[1]")
            except NoSuchElementException:
                pass
            clickable.click()
            time.sleep(2)
            return
        except (TimeoutException, NoSuchElementException):
            break  # icon not found at all -- no point retrying, go to manual fallback
        except Exception:
            # Likely a StaleElementReferenceException -- just retry the loop.
            time.sleep(1)
            continue

    pause_for_manual_step(
        "Please click the 'Student Assignment' menu item in the sidebar,\n"
        "then make sure the 'Assignment' tab (not 'Submit Assignment') is selected."
    )


def set_date_field(driver: webdriver.Chrome, field_id: str, date_value: str) -> bool:
    """
    Set a Syncfusion e-datepicker input (like id="FromDate" / id="ToDate").

    These widgets keep their own internal JS state, so just setting
    element.value via JavaScript (like a plain HTML date input) doesn't
    reliably work -- the picker doesn't "see" the change. Instead we
    click into the field, select all + delete any existing text, type
    the date as real keystrokes, then press Tab to commit it (which is
    what a human typing into the field would do, and is what these
    Syncfusion controls expect to update their bound value).
    """
    from selenium.webdriver.common.keys import Keys

    try:
        field = driver.find_element(By.ID, field_id)
    except NoSuchElementException:
        return False

    field.click()
    time.sleep(0.3)
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys(Keys.DELETE)
    field.send_keys(date_value)
    field.send_keys(Keys.TAB)
    time.sleep(0.5)
    return True


def select_syncfusion_dropdown(driver: webdriver.Chrome, input_id: str, option_text_fragment: str) -> bool:
    """
    Select an option from a Syncfusion DropDownList control (the pattern
    used for both "Assignment type" and "Subjects" on this page: a visible
    text <input> that, when clicked, opens a floating <li> options popup
    elsewhere in the DOM).

    option_text_fragment is matched case-insensitively against each
    option's text (e.g. "q.bank" matches the "Q.Bank" option).
    """
    from selenium.webdriver.common.keys import Keys

    try:
        field = driver.find_element(By.ID, input_id)
    except NoSuchElementException:
        return False

    # The visible <input> for these Syncfusion dropdowns is actually
    # readonly with tabindex="-1" -- it's not the real click target.
    # The wrapping <div class="... e-ddl ...role="listbox"> around it is
    # what actually receives clicks and opens the popup. Find that wrapper
    # and click it instead (confirmed by the ElementClickIntercepted error
    # we hit clicking the input directly).
    try:
        clickable = field.find_element(By.XPATH, "./ancestor::div[contains(@class,'e-ddl')][1]")
    except NoSuchElementException:
        clickable = field

    try:
        clickable.click()
    except Exception:
        # Fall back to a JS-triggered click, which bypasses the "is
        # another element on top of it" check that a normal click does.
        driver.execute_script("arguments[0].click();", clickable)

    time.sleep(0.5)

    try:
        option = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//li[contains(@class,'e-list-item') and "
                f"contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{option_text_fragment.lower()}')]"
            ))
        )
    except TimeoutException:
        # Close the popup so we don't leave the UI in a weird state. The
        # input itself is readonly, so send Escape to the page body instead
        # of the input (which may not accept keystrokes).
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        return False

    option.click()
    time.sleep(0.5)
    return True


def set_assignment_type_to_qbank(driver: webdriver.Chrome) -> bool:
    """
    Set the 'Assignment type' dropdown to 'Q.Bank', using the confirmed
    real element id ("Assignmenttype") found via dump_assignment_page_fields.py.
    """
    return select_syncfusion_dropdown(driver, "Assignmenttype", "q.bank")


def set_filters(driver: webdriver.Chrome, from_date: str, to_date: str) -> None:
    """
    Set the From Date / To Date filters and Assignment Type = Q.Bank on the
    assignment grid. Subject is left blank to fetch all subjects in one
    pass (we sort by subject afterwards using the grid's 'Subject Name'
    column).
    """
    ok_from = set_date_field(driver, "FromDate", from_date)
    ok_to = set_date_field(driver, "ToDate", to_date)
    ok_type = set_assignment_type_to_qbank(driver)

    if not (ok_from and ok_to):
        pause_for_manual_step(
            f"Please manually set 'From Date' to {from_date} and 'To Date' to {to_date}\n"
            "in the filter bar."
        )
    if not ok_type:
        pause_for_manual_step(
            "Please manually set 'Assignment type' to 'Q.Bank' in the filter bar\n"
            "(this ensures we only download question banks, not homework/exam portions)."
        )

    # --- DIAGNOSTICS: confirm what actually ended up in the filter fields ---
    # This prints to the terminal and saves a screenshot so we can see
    # exactly what state the page was in, instead of guessing why a
    # filter combination might return 0 rows.
    try:
        from_val = driver.find_element(By.ID, "FromDate").get_attribute("value")
        to_val = driver.find_element(By.ID, "ToDate").get_attribute("value")
        type_val = driver.find_element(By.ID, "Assignmenttype").get_attribute("value")
        print(f"[diagnostic] FromDate field now shows: '{from_val}'")
        print(f"[diagnostic] ToDate field now shows:   '{to_val}'")
        print(f"[diagnostic] Assignment type field now shows: '{type_val}'")
    except NoSuchElementException:
        print("[diagnostic] Could not read back FromDate/ToDate/Assignmenttype field values.")

    Path("debug_screenshots").mkdir(exist_ok=True)
    screenshot_path = Path("debug_screenshots") / "after_filters.png"
    driver.save_screenshot(str(screenshot_path))
    print(f"[diagnostic] Screenshot saved: {screenshot_path.resolve()}")


# ---------------------------------------------------------------------------
# SCRAPE THE RESULTS GRID (with lazy-loaded / scrolling pagination)
# ---------------------------------------------------------------------------

def scroll_grid_to_load_all_rows(driver: webdriver.Chrome, max_scrolls: int = 10) -> None:
    """
    Some grids lazily load more rows as you scroll the inner grid
    container. This repeatedly scrolls the Syncfusion grid's content area
    (class "e-gridcontent", which is the actual scrollable body wrapper --
    not the <table> itself) to the bottom until the row count stops
    increasing. In practice this grid seems to load all rows into the DOM
    at once (confirmed via diagnostics), so this mostly acts as a safety
    net rather than being strictly required.
    """
    try:
        grid_content = driver.find_element(By.CSS_SELECTOR, ".e-gridcontent, .e-content")
    except NoSuchElementException:
        return  # no known scrollable container -- assume all rows are already loaded

    last_row_count = -1
    for _ in range(max_scrolls):
        rows = driver.find_elements(By.CSS_SELECTOR, ".e-gridcontent tbody tr, .e-content tbody tr")
        if len(rows) == last_row_count:
            break  # no new rows loaded since last scroll -> we've got them all
        last_row_count = len(rows)
        driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", grid_content)
        time.sleep(1)


def scrape_rows(driver: webdriver.Chrome):
    """
    Read every row of the assignment grid and return a list of dicts:
    {title, subject, view_element} -- one per row.

    Column order (from the observed layout) is:
    S.No | Title | Assignment Date | Due Date | Subject Name | Type Name | View
    We locate columns by their header text rather than a fixed index where
    possible, so small layout changes don't break everything.
    """
    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    try:
        header_table = wait.until(EC.presence_of_element_located((By.XPATH, "//table[.//th[contains(.,'Title')]]")))
    except TimeoutException:
        pause_for_manual_step(
            "Could not find the results table. Please make sure the assignment\n"
            "grid with Q.Bank results is visible on screen."
        )
        header_table = driver.find_element(By.XPATH, "//table")

    headers = [th.text.strip().lower() for th in header_table.find_elements(By.TAG_NAME, "th")]
    print(f"[diagnostic] Table headers found: {headers}")

    def col_index(name_fragment: str, default: int) -> int:
        for i, h in enumerate(headers):
            if name_fragment in h:
                return i
        return default

    title_idx = col_index("title", 1)
    subject_idx = col_index("subject", 4)
    view_idx = col_index("view", len(headers) - 1)

    # IMPORTANT: Syncfusion grids render the header and the body as two
    # SEPARATE <table> elements (an "e-gridheader" table with the <th>
    # cells, and an "e-gridcontent" table with the actual data <tr> rows
    # and no headers at all). Reading header_table's own <tbody> returns
    # 0-1 rows even when data is visible on screen -- the real rows live
    # in that sibling content table. We find the shared grid wrapper, then
    # pick whichever *other* table inside it actually has body rows.
    # IMPORTANT: the previous ancestor search for class containing 'e-grid'
    # was matching 'e-gridheader' itself (a substring match), stopping
    # there instead of reaching the real outer grid container -- so it
    # never saw the sibling '.e-gridcontent' wrapper that holds the actual
    # data table. Query that content wrapper directly instead.
    content_table = header_table
    best_row_count = len(header_table.find_elements(By.CSS_SELECTOR, "tbody tr"))
    try:
        candidate = driver.find_element(By.CSS_SELECTOR, ".e-gridcontent table, .e-content table")
        row_count = len(candidate.find_elements(By.CSS_SELECTOR, "tbody tr"))
        print(f"[diagnostic] .e-gridcontent table found: row_count={row_count}")
        if row_count > best_row_count:
            content_table = candidate
            best_row_count = row_count
    except NoSuchElementException:
        print("[diagnostic] No .e-gridcontent table found -- falling back to header_table")

    rows = content_table.find_elements(By.CSS_SELECTOR, "tbody tr")
    print(f"[diagnostic] Raw <tr> count found in content table: {len(rows)}")
    if len(rows) == 0:
        Path("debug_screenshots").mkdir(exist_ok=True)
        screenshot_path = Path("debug_screenshots") / "zero_rows_found.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"[diagnostic] Screenshot saved: {screenshot_path.resolve()}")
        print("[diagnostic] This likely means either: the filters produced a genuinely empty")
        print("[diagnostic] result set, or the grid hadn't finished loading yet, or we matched")
        print("[diagnostic] the wrong <table> on the page.")

    results = []
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) <= max(title_idx, subject_idx, view_idx):
            continue  # skip malformed / header-ish rows
        title = cells[title_idx].text.strip()
        subject = cells[subject_idx].text.strip()
        if not title or not subject:
            continue

        # The actual clickable element is the eye icon itself
        # (<i class="fas fa-eye" onclick="modalOpen(event)">), confirmed from
        # the real page HTML. Target that specifically rather than the whole
        # <td>, since clicking exactly on the icon is more reliable than
        # hoping a click on the cell lands on the right spot.
        try:
            view_icon = cells[view_idx].find_element(By.CSS_SELECTOR, "i.fa-eye, [onclick*='modalOpen']")
        except NoSuchElementException:
            view_icon = cells[view_idx]

        results.append({"title": title, "subject": subject, "view_cell": view_icon})
    return results


# ---------------------------------------------------------------------------
# DOWNLOAD A SINGLE FILE
# ---------------------------------------------------------------------------

def download_url_with_browser_cookies(driver: webdriver.Chrome, file_url: str, dest_path: Path) -> bool:
    """
    Download a file URL using the browser's own authenticated session
    cookies (via the `requests` library, so we don't have to fight
    Chrome's native download UI/prompt). Picks a sensible file extension
    from the response if one isn't obvious from the URL.
    """
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie["name"], cookie["value"])

    response = session.get(file_url, timeout=30)
    if response.status_code != 200 or not response.content:
        print(f"  [diagnostic] HTTP GET returned status={response.status_code}, "
              f"content_length={len(response.content) if response.content else 0}")
        return False

    content_type = response.headers.get("Content-Type", "")
    ext = ".pdf"
    if "pdf" not in content_type and "." in file_url.split("/")[-1]:
        ext = "." + file_url.split(".")[-1].split("?")[0]

    dest_path = dest_path.with_suffix(ext)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(response.content)
    return True


def close_modal(driver: webdriver.Chrome) -> None:
    """
    Close the attachment-preview modal. These modals typically respond to
    the Escape key (a very common pattern), so we try that first; we also
    try clicking an explicit close button (the '\u2715' in the top-right of
    the modal) as a backup.
    """
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains

    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
    except Exception:
        pass

    try:
        close_btn = driver.find_element(
            By.XPATH, "//*[self::button or self::span or self::i][contains(@class,'close') or text()='\u2715' or text()='\u00d7']"
        )
        if close_btn.is_displayed():
            close_btn.click()
            time.sleep(0.5)
    except NoSuchElementException:
        pass


def download_all_attachments(driver: webdriver.Chrome, view_icon, dest_base_path: Path, debug_label: str = "row") -> list:
    """
    Click the 'View' (eye) icon for a row, which opens a modal showing one
    or more PDF attachment thumbnails (with '<' '>' arrows to page through
    multiple attachments). Clicking a thumbnail opens the actual file in a
    new browser tab -- we capture that tab's URL and download it with
    requests (reusing the browser's cookies), then move to the next
    attachment if there is one.

    Returns a tuple: (list of Paths successfully saved, no_attachment flag).
    no_attachment is True when the assignment genuinely has nothing to
    download (not a bug -- some Q.Bank entries are just a title/description
    with no file attached).
    """
    saved_paths = []

    try:
        view_icon.click()
    except Exception as e:
        print(f"  [diagnostic] Clicking the 'View' (eye) icon itself raised an exception: {e}")
        # Try a JS-triggered click as a fallback, same fix that resolved a
        # near-identical issue with the pagination "next page" button --
        # a real click can be intercepted by an overlapping element even
        # when the target itself is technically present and enabled.
        try:
            driver.execute_script("arguments[0].click();", view_icon)
            print("  [diagnostic] JS click fallback succeeded, continuing.")
        except Exception as e2:
            print(f"  [diagnostic] JS click fallback also failed: {e2}")
            Path("debug_screenshots").mkdir(exist_ok=True)
            safe_label = sanitize_filename(debug_label)
            shot_path = Path("debug_screenshots") / f"view_click_failed_{safe_label}.png"
            driver.save_screenshot(str(shot_path))
            print(f"  [diagnostic] Screenshot saved: {shot_path}")
            return saved_paths, False

    # Wait for the modal to actually render, rather than trusting a fixed
    # sleep -- some rows seem to take longer to open than others.
    try:
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Description')]"))
        )
    except TimeoutException:
        pass  # fall through and let the thumbnail search below fail/report if the modal never showed up
    time.sleep(0.5)

    # Find the thumbnail inside the now-open modal. We match specifically
    # on <img src*="pdf"> or a font-icon with class "fa-file-pdf" -- these
    # correctly identified real attachments in testing.
    #
    # We deliberately do NOT use broader fallback selectors like
    # "img[src*='image']" or "[class*='pdf']" here. Two separate false
    # positives came from selectors like that:
    #   1. "[class*='pdf']" matched the grid toolbar's "PDF Export" button
    #      (class="... e-pdfexport ...") sitting behind the modal.
    #   2. "img[src*='image']" matched unrelated site icons/logos, since
    #      this site's own static assets live under a folder literally
    #      named "/Images/..." -- which contains the substring "image".
    # Both looked like real thumbnails to a loose selector but weren't,
    # causing ElementClickIntercepted errors or clicks that don't open a
    # new tab. Sticking to the narrow, confirmed-working selectors means
    # "no thumbnail found" reliably means "no attachment exists" rather
    # than "we matched something on the page that isn't the thumbnail".
    max_attachments = 10  # safety cap in case a carousel loops indefinitely
    seen_urls = set()
    no_attachment = False

    for attempt_num in range(max_attachments):
        thumbnail = None
        matched_selector = None
        for selector in ["img[src*='pdf' i]", "i.fa-file-pdf"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed():
                    thumbnail = el
                    matched_selector = selector
                    break
            except NoSuchElementException:
                continue

        if thumbnail is None:
            if attempt_num == 0:
                # This assignment genuinely has no attached file -- not a
                # bug, just nothing to download. Log it clearly as such
                # rather than as a failure, and skip the screenshot (we
                # already know what this looks like).
                no_attachment = True
                print("  [info] No attachment found for this assignment (nothing to download).")
            break  # no (more) thumbnails found in the modal

        print(f"  [diagnostic] Thumbnail found via selector '{matched_selector}'")

        original_windows = driver.window_handles
        try:
            thumbnail.click()
        except Exception as e:
            print(f"  [diagnostic] Clicking thumbnail raised an exception: {e}")
            Path("debug_screenshots").mkdir(exist_ok=True)
            safe_label = sanitize_filename(debug_label)
            shot_path = Path("debug_screenshots") / f"click_failed_{safe_label}.png"
            driver.save_screenshot(str(shot_path))
            print(f"  [diagnostic] Screenshot saved: {shot_path}")
            break

        time.sleep(1.5)

        # The click should have opened the real file in a new tab.
        new_windows = [w for w in driver.window_handles if w not in original_windows]
        file_url = None
        if new_windows:
            driver.switch_to.window(new_windows[0])
            file_url = driver.current_url
            print(f"  [diagnostic] New tab opened with URL: {file_url}")
            driver.close()
            driver.switch_to.window(original_windows[0])
        else:
            print("  [diagnostic] Click did NOT open a new tab -- saving screenshot of current state.")
            Path("debug_screenshots").mkdir(exist_ok=True)
            safe_label = sanitize_filename(debug_label)
            shot_path = Path("debug_screenshots") / f"no_new_tab_{safe_label}.png"
            driver.save_screenshot(str(shot_path))
            print(f"  [diagnostic] Screenshot saved: {shot_path}")

        if file_url and file_url not in seen_urls:
            seen_urls.add(file_url)
            index_suffix = f"_{len(saved_paths) + 1}" if len(saved_paths) > 0 else ""
            dest_path = dest_base_path.parent / (dest_base_path.name + index_suffix)
            if download_url_with_browser_cookies(driver, file_url, dest_path):
                saved_paths.append(dest_path)
            else:
                print(f"  [diagnostic] download_url_with_browser_cookies() failed for: {file_url}")

        # Try to move to the next attachment, if this assignment has more
        # than one (the '<' '>' navigation seen in the modal screenshot).
        try:
            next_arrow = driver.find_element(
                By.XPATH, "//*[self::button or self::span or self::i][contains(@class,'next') or text()='>']"
            )
            if next_arrow.is_displayed() and next_arrow.is_enabled():
                next_arrow.click()
                time.sleep(1)
                continue
        except NoSuchElementException:
            pass
        break  # no next arrow (or only one attachment) -- we're done with this row

    close_modal(driver)
    return saved_paths, no_attachment


def go_to_next_page(driver: webdriver.Chrome) -> bool:
    """
    Click the grid's "next page" pager control, if one exists and isn't
    disabled (i.e. we're not already on the last page).

    Returns True if we successfully moved to a next page, False if there
    isn't one (we're on the last page, or no pager was found at all).
    """
    try:
        next_btn = driver.find_element(
            By.CSS_SELECTOR,
            ".e-nextpage, [title='Go to next page'], [aria-label='Go to next page']"
        )
    except NoSuchElementException:
        print("[diagnostic] go_to_next_page: no next-page button found in the DOM at all.")
        return False

    classes = next_btn.get_attribute("class") or ""
    is_disabled = (
        "e-disable" in classes
        or next_btn.get_attribute("aria-disabled") == "true"
    )
    print(f"[diagnostic] go_to_next_page: found button, class='{classes}', is_disabled={is_disabled}")
    if is_disabled:
        return False

    try:
        next_btn.click()
    except Exception as e:
        print(f"[diagnostic] go_to_next_page: normal click failed ({e}); trying JS click.")
        driver.execute_script("arguments[0].click();", next_btn)

    # Wait for the new page's data to load (reuse the same polling
    # approach used after changing filters).
    for _ in range(15):
        row_counts = [len(t.find_elements(By.CSS_SELECTOR, "tbody tr")) for t in driver.find_elements(By.TAG_NAME, "table")]
        if any(c > 1 for c in row_counts):
            break
        time.sleep(1)
    time.sleep(0.5)
    return True


# ---------------------------------------------------------------------------
# SIBLING SWITCHING
# ---------------------------------------------------------------------------

def get_sibling_list(driver: webdriver.Chrome) -> list:
    """
    Opens the 'Sibling' dropdown in the top nav and reads every child's
    name, class/section, and internal switch-token (the argument the
    site's own switchAccount() JS function expects).

    Confirmed real structure (via diagnostic dump): each child is an
    <li class="chip ... chiptooltip" onclick="switchAccount('TOKEN')">
    whose visible text is two lines: the child's name, then their
    class/section (e.g. "ATHENA XAVIER MARSHALL" / "VI - SAPPHIRE").

    Returns a list of dicts: {"name", "class_section", "token", "folder_name"}.
    Returns an empty list if no dropdown/chips are found (e.g. a
    single-child account with no sibling switcher at all).
    """
    try:
        trigger = driver.find_element(By.CSS_SELECTOR, ".dropdown-wrappernew")
    except NoSuchElementException:
        return []

    try:
        trigger.click()
    except Exception:
        driver.execute_script("arguments[0].click();", trigger)
    time.sleep(1)

    chips = driver.find_elements(By.CSS_SELECTOR, "li.chip")
    siblings = []
    for chip in chips:
        onclick = chip.get_attribute("onclick") or ""
        match = re.search(r"switchAccount\('([^']+)'\)", onclick)
        if not match:
            continue
        token = match.group(1)
        lines = [line.strip() for line in chip.text.strip().split("\n") if line.strip()]
        name = lines[0] if lines else "Unknown"
        class_section = lines[1] if len(lines) > 1 else ""
        folder_name = sanitize_filename(f"{name} - {class_section}" if class_section else name)
        siblings.append({"name": name, "class_section": class_section, "token": token, "folder_name": folder_name})

    # Close the dropdown again so it doesn't interfere with subsequent clicks.
    try:
        driver.find_element(By.TAG_NAME, "body").click()
    except Exception:
        pass

    return siblings


def switch_to_sibling(driver: webdriver.Chrome, token: str) -> None:
    """
    Switch the active child using the site's own switchAccount() JS
    function directly (called via execute_script) -- more reliable than
    clicking the chip in the UI, since we already have the exact token
    the site itself uses internally.
    """
    driver.execute_script("switchAccount(arguments[0]);", token)
    time.sleep(2)


# ---------------------------------------------------------------------------
# MAIN DRIVER LOGIC
# ---------------------------------------------------------------------------

def download_for_current_context(driver: webdriver.Chrome, from_date: str, to_date: str,
                                   assignments_url: str) -> dict:
    """
    Runs the filter -> paginate -> download flow for whichever child is
    CURRENTLY active in the browser session (i.e. assumes login, and any
    sibling-switching, has already happened). Uses the module-level
    OUTPUT_ROOT / MANIFEST_PATH at call time, so a caller looping over
    multiple siblings can point these at a different per-child folder
    before each call.

    Returns a dict with this run's summary counts.
    """
    manifest = load_manifest()
    summary = {"downloaded": 0, "skipped": 0, "no_attachment": 0, "failed": 0}

    navigate_to_assignments(driver, assignments_url=assignments_url)
    set_filters(driver, from_date, to_date)

    # Wait for the grid to actually finish its AJAX refresh after the
    # filter change, rather than trusting a fixed sleep. We poll for
    # any <table> on the page having more than 1 body row (1 is the
    # "no records" placeholder row count we've seen while loading).
    print("[diagnostic] Waiting for grid data to load...")
    for _ in range(15):
        row_counts = [len(t.find_elements(By.CSS_SELECTOR, "tbody tr")) for t in driver.find_elements(By.TAG_NAME, "table")]
        if any(c > 1 for c in row_counts):
            break
        time.sleep(1)
    print(f"[diagnostic] Row counts across all <table> elements just before scraping: {row_counts}")

    # Try to find the scrollable grid container to load all lazy rows
    # (a safety net -- in practice this grid uses numbered pagination,
    # handled by the page loop below, rather than infinite scroll).
    scroll_grid_to_load_all_rows(driver)

    page_num = 1
    while True:
        rows = scrape_rows(driver)
        print(f"\n--- Page {page_num}: found {len(rows)} question bank entries ---\n")

        for row in rows:
            subject = row["subject"]
            title = row["title"]

            if already_downloaded(manifest, subject, title):
                print(f"SKIP (already downloaded): [{subject}] {title}")
                summary["skipped"] += 1
                continue

            subject_folder = OUTPUT_ROOT / sanitize_filename(subject)
            dest_path = subject_folder / sanitize_filename(title)

            print(f"Downloading: [{subject}] {title} ...")
            saved_paths, no_attachment = download_all_attachments(
                driver, row["view_cell"], dest_path,
                debug_label=f"p{page_num}_{subject}_{title}",
            )

            if saved_paths:
                manifest[f"{subject}::{title}"] = {"subject": subject, "title": title, "files": [str(p) for p in saved_paths]}
                save_manifest(manifest)
                summary["downloaded"] += 1
                print(f"  -> saved {len(saved_paths)} file(s).")
            elif no_attachment:
                # Not a failure -- this assignment genuinely has no file
                # attached. Record it in the manifest too so we don't
                # keep re-checking it (and re-opening its modal) on
                # every future run.
                manifest[f"{subject}::{title}"] = {"subject": subject, "title": title, "files": [], "no_attachment": True}
                save_manifest(manifest)
                summary["no_attachment"] += 1
                print("  -> no attachment (nothing to download).")
            else:
                summary["failed"] += 1
                print("  -> FAILED (couldn't determine or fetch the file URL).")

        if not go_to_next_page(driver):
            break
        page_num += 1

    print("\n" + "-" * 50)
    print(f"Done. Downloaded: {summary['downloaded']} | Skipped (already had): {summary['skipped']} | "
          f"No attachment: {summary['no_attachment']} | Failed: {summary['failed']}")
    print(f"Files are organized under: {OUTPUT_ROOT.resolve()}")

    return summary


def run_download_all_siblings(username: str, password: str, from_date: str, to_date: str,
                               base_url: str = None, assignments_url: str = None,
                               headless: bool = False, close_when_done: bool = False) -> dict:
    """
    Logs in once, then automatically downloads question banks for EVERY
    child linked to this account (via the "Sibling" switcher), keeping
    each child's files and skip-tracking completely separate:

      QuestionBanks/<Child Name - Class>/<Subject>/...
      downloaded_manifest_<child name - class>.json

    If no sibling switcher is found at all (a single-child account), it
    just downloads for whichever child is already active -- same
    behavior as before, no special-casing needed by the caller.

    This is the entry point both the command-line script and the GUI app
    call. Returns a dict of {child_name: summary_dict}.
    """
    global OUTPUT_ROOT, MANIFEST_PATH

    base_url = base_url or DEFAULT_BASE_URL
    assignments_url = assignments_url or DEFAULT_ASSIGNMENTS_URL
    original_output_root = OUTPUT_ROOT
    original_manifest_path = MANIFEST_PATH

    driver = start_browser(headless=headless)
    all_summaries = {}

    try:
        login(driver, base_url, username, password)
        siblings = get_sibling_list(driver)

        if not siblings:
            print("[info] No sibling switcher found -- downloading for the currently active account only.")
            all_summaries["current"] = download_for_current_context(driver, from_date, to_date, assignments_url)
        else:
            names = ", ".join(f"{s['name']} ({s['class_section']})" for s in siblings)
            print(f"Found {len(siblings)} child(ren): {names}")

            for sibling in siblings:
                print("\n" + "=" * 70)
                print(f"Switching to: {sibling['name']} ({sibling['class_section']})")
                print("=" * 70)
                switch_to_sibling(driver, sibling["token"])

                OUTPUT_ROOT = original_output_root / sibling["folder_name"]
                MANIFEST_PATH = Path(f"downloaded_manifest_{sibling['folder_name']}.json")

                all_summaries[sibling["name"]] = download_for_current_context(driver, from_date, to_date, assignments_url)

        print("\n" + "#" * 70)
        print("ALL CHILDREN DONE")
        for name, summary in all_summaries.items():
            print(f"  {name}: {summary}")
        print("#" * 70)

    finally:
        OUTPUT_ROOT = original_output_root
        MANIFEST_PATH = original_manifest_path
        if not close_when_done:
            CONFIRM_CALLBACK("\nPress Enter to close the browser...")
        driver.quit()

    return all_summaries


# Kept for backward compatibility / single-context use (e.g. a script that
# only ever wants the currently active child, without touching siblings).
def run_download(username: str, password: str, from_date: str, to_date: str,
                  base_url: str = None, assignments_url: str = None,
                  headless: bool = False, close_when_done: bool = False) -> dict:
    """Log in and download for the currently active account only (no sibling switching)."""
    base_url = base_url or DEFAULT_BASE_URL
    assignments_url = assignments_url or DEFAULT_ASSIGNMENTS_URL

    driver = start_browser(headless=headless)
    try:
        login(driver, base_url, username, password)
        return download_for_current_context(driver, from_date, to_date, assignments_url)
    finally:
        if not close_when_done:
            CONFIRM_CALLBACK("\nPress Enter to close the browser...")
        driver.quit()


def main():
    """Command-line entry point: parses args/console prompts, then calls run_download()."""
    parser = argparse.ArgumentParser(description="Download and sort Entab portal question banks by subject.")
    parser.add_argument("--from-date", help="Start date for the assignment filter, DD-MM-YYYY e.g. 01-04-2026")
    parser.add_argument("--to-date", help="End date for the assignment filter, DD-MM-YYYY e.g. 31-03-2027")
    parser.add_argument("--assignments-url", help="Direct URL to the Student Assignment page, if known (more reliable than clicking the sidebar)")
    parser.add_argument("--headless", action="store_true", help="Run Chrome without a visible window (not recommended on first run)")
    args = parser.parse_args()

    # Load credentials and base URL from .env (never hard-coded in this file).
    load_dotenv()
    username = os.getenv("ENTAB_USERNAME")
    password = os.getenv("ENTAB_PASSWORD")
    base_url = os.getenv("ENTAB_BASE_URL", DEFAULT_BASE_URL)
    assignments_url = args.assignments_url or os.getenv("ENTAB_ASSIGNMENTS_URL", DEFAULT_ASSIGNMENTS_URL)

    if not username or not password:
        sys.exit("ERROR: Set ENTAB_USERNAME and ENTAB_PASSWORD in a .env file (see .env.example).")

    # Ask for the date range interactively if not supplied on the command line,
    # so the same script works for any academic year / any school's calendar.
    # NOTE: the site's own date fields display DD-MM-YYYY (e.g. 01-04-2026),
    # so we ask for and type dates in that same format to match what the
    # Syncfusion date-picker expects.
    from_date = args.from_date or input("Enter From Date (DD-MM-YYYY, e.g. 01-04-2026): ").strip()
    to_date = args.to_date or input("Enter To Date (DD-MM-YYYY, e.g. 31-03-2027): ").strip()

    run_download_all_siblings(
        username=username, password=password,
        from_date=from_date, to_date=to_date,
        base_url=base_url, assignments_url=assignments_url,
        headless=args.headless, close_when_done=False,
    )


if __name__ == "__main__":
    main()