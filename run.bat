@echo off
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

if not exist ".env" (
    echo [ERROR] .env file not found! Copy .env.example to .env and add your TELEGRAM_BOT_TOKEN.
    pause
    exit /b 1
)

echo Starting Iraqi Cinema 24/7 Notifier...
python -m app.main
pause
