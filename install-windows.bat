@echo off
rem Double-click installer for Windows: installs /godmode for Claude Desktop (Code tab) and the claude CLI.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
  echo.
  echo Installation failed. See the messages above.
) else (
  echo.
  echo All done. Quit Claude Desktop completely ^(File ^> Exit or tray icon ^> Quit^), reopen it, open the Code tab and type /godmode
)
pause
