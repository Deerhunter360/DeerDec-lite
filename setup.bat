@echo off
rem One-time install of DeerDec's Python packages.
cd /d "%~dp0"
where py >nul 2>nul && (py -m pip install -r requirements.txt & goto done)
python -m pip install -r requirements.txt
:done
echo.
echo Done. Start DeerDec with DeerDec.bat
pause
