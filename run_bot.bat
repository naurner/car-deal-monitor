@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found in .venv
    echo Run setup first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env file not found.
    echo Copy .env.example to .env and fill in BOT_TOKEN and OWNER_TELEGRAM_ID.
    pause
    exit /b 1
)

echo If the bot is already running in another window, close it first
echo (two instances at once will conflict with each other in Telegram).
echo.
echo Starting CarDealTG...
echo Keep this window open while the bot should run. Closing it stops the bot.
echo.

".venv\Scripts\python.exe" -X utf8 main.py

echo.
echo Bot stopped. Press any key to close this window.
pause >nul
