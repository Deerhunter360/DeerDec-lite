@echo off
rem Hands off to DeerDec.vbs (no window). If you see a console, use DeerDec.vbs directly.
cd /d "%~dp0"
start "" wscript.exe "%~dp0DeerDec.vbs"
exit
