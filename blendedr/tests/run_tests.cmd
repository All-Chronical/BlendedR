@echo off
"%~dp0..\build\bin\blender.exe" -b --factory-startup --python-exit-code 1 --python "%~dp0run_tests.py"
exit /b %ERRORLEVEL%
