@echo off
REM build_exe.bat
REM
REM One-time build script to package gui_app.py into a single, double-
REM clickable Windows .exe that recipients can run WITHOUT installing
REM Python. Run this from the project folder, inside your activated
REM virtual environment (the same one you already use for `python main.py`).
REM
REM IMPORTANT: you MUST activate your venv first, or PyInstaller will
REM build using a different Python environment and silently leave out
REM packages (this is what caused a "No module named 'requests'" error
REM when the exe was run previously).
REM
REM Usage:
REM     venv\Scripts\activate
REM     build_exe.bat
REM
REM The finished .exe will appear in:  dist\EntabDownloader.exe
REM That single file is everything you need to send to other parents.

echo Checking that required packages are installed in THIS environment...
pip show requests >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: 'requests' is not installed in the current Python environment.
    echo Make sure you ran "venv\Scripts\activate" BEFORE running this script,
    echo then run: pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

pip install pyinstaller

REM --collect-all is used (rather than relying on PyInstaller's automatic
REM import detection) for every third-party dependency this project uses.
REM This is more verbose but far more reliable -- automatic detection is
REM what missed 'requests' the first time.
pyinstaller --onefile --windowed ^
    --name EntabDownloader ^
    --icon entabdownloader.ico ^
    --add-data "entabdownloader.ico;." ^
    --collect-all selenium ^
    --collect-all webdriver_manager ^
    --collect-all requests ^
    --collect-all dotenv ^
    --collect-all urllib3 ^
    --collect-all certifi ^
    --collect-all charset_normalizer ^
    --collect-all idna ^
    --collect-all pdfplumber ^
    --collect-all pypdf ^
    --collect-all reportlab ^
    --collect-all pdfminer ^
    --collect-all PIL ^
    --collect-all pypdfium2 ^
    gui_app.py

echo.
echo Build complete. Find your .exe at: dist\EntabDownloader.exe
pause