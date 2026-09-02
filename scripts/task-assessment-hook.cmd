@echo off
where python3 >nul 2>nul
if errorlevel 1 goto py_launcher
python3 -I -S "%~dp0task_assessment_hook.py"
exit /b %errorlevel%

:py_launcher
where py >nul 2>nul
if errorlevel 1 goto python_fallback
py -3 -I -S "%~dp0task_assessment_hook.py"
exit /b %errorlevel%

:python_fallback
python -I -S "%~dp0task_assessment_hook.py"
exit /b %errorlevel%
