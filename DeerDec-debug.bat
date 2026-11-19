@echo off
rem Same as DeerDec.bat but keeps the console open so you can read errors.
cd /d "%~dp0"
where py >nul 2>nul && (py app.py & pause & exit /b)
python app.py
pause
