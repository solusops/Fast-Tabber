@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
  echo Fast-Tabber installation FAILED. Do not report success or use the Skill until this is resolved.
  exit /b 1
)
echo Fast-Tabber installed. Restart OpenCode and use /tabs list my tabs.
