@echo off
cd /d "%~dp0"
echo.
echo ================================================
echo   Lucid Animal Hospital - Consent Form System
echo ================================================
echo.

echo [1/4] Checking Python...
python --version
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found. Install from python.org and CHECK "Add to PATH".
    goto END
)
echo.

echo [2/4] Checking .env file...
if exist .env (
    echo .env file found.
) else (
    echo WARNING: .env file not found. AI/QR features will be disabled.
    echo To enable, copy .env.example to .env and set ANTHROPIC_API_KEY.
)
echo.

echo [3/4] Checking dependencies...
python -c "import flask, requests, qrcode" 2>nul
if errorlevel 1 (
    echo Installing packages ^(may take 1-2 minutes^)...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] pip install failed. Check internet.
        goto END
    )
) else (
    echo All packages already installed.
)
echo.

echo [4/4] Starting server...
echo ================================================
echo   Server: http://127.0.0.1:5000
echo   Press CTRL+C to stop.
echo ================================================
echo.
python app.py

:END
pause