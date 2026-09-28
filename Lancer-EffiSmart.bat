@echo off
rem Lance EffiSmart en mode local, tout en Python (double-clic).
title EffiSmart
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py lancer.py
) else (
    python lancer.py
)
if errorlevel 1 (
    echo.
    echo Une erreur est survenue : copiez les messages ci-dessus pour obtenir de l aide.
    pause
)