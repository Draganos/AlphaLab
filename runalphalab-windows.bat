@echo off
setlocal EnableExtensions

title AlphaLab

set "REPO_URL=https://github.com/Draganos/AlphaLab.git"
set "PROJECT_DIR=%USERPROFILE%\AlphaLab"

echo.
echo ========================================
echo             AlphaLab Launcher
echo ========================================
echo.

REM ----------------------------------------
REM Check Git
REM ----------------------------------------
where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git is not installed or is not in PATH.
    echo Please install Git and run this file again.
    echo.
    pause
    exit /b 1
)

REM ----------------------------------------
REM Check Python
REM ----------------------------------------
where py >nul 2>&1
if errorlevel 1 (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python is not installed or is not in PATH.
        echo AlphaLab requires Python 3.12 or newer.
        echo.
        pause
        exit /b 1
    )
    set "PYTHON=python"
) else (
    set "PYTHON=py"
)

REM ----------------------------------------
REM Check that Python is 3.12+
REM ----------------------------------------
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"
if errorlevel 1 (
    echo ERROR: AlphaLab requires Python 3.12 or newer.
    %PYTHON% --version
    echo.
    pause
    exit /b 1
)

REM ----------------------------------------
REM Clone or update repository
REM ----------------------------------------
if not exist "%PROJECT_DIR%" (
    echo AlphaLab not found.
    echo Cloning from GitHub...
    echo.

    git clone "%REPO_URL%" "%PROJECT_DIR%"

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to clone AlphaLab.
        pause
        exit /b 1
    )

) else if not exist "%PROJECT_DIR%\.git" (
    echo ERROR:
    echo "%PROJECT_DIR%" already exists,
    echo but it is not a Git repository.
    echo.
    pause
    exit /b 1
) else (
    echo AlphaLab already exists.
    echo Updating from GitHub...
    echo.

    cd /d "%PROJECT_DIR%"

    git checkout main
    if errorlevel 1 (
        echo ERROR: Could not switch to main branch.
        pause
        exit /b 1
    )

    git pull origin main
    if errorlevel 1 (
        echo ERROR: Could not update AlphaLab.
        pause
        exit /b 1
    )
)

cd /d "%PROJECT_DIR%"

REM ----------------------------------------
REM Create virtual environment
REM ----------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Creating Python virtual environment...

    %PYTHON% -m venv .venv

    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
)

REM ----------------------------------------
REM Install locked dependencies
REM ----------------------------------------
echo.
echo Installing AlphaLab dependencies...
echo.

.venv\Scripts\python.exe -m pip install --upgrade pip

.venv\Scripts\python.exe -m pip install -r requirements.lock

if errorlevel 1 (
    echo.
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

REM ----------------------------------------
REM Install AlphaLab itself
REM ----------------------------------------
echo.
echo Installing AlphaLab...
echo.

.venv\Scripts\python.exe -m pip install --no-deps -e .

if errorlevel 1 (
    echo.
    echo ERROR: AlphaLab installation failed.
    pause
    exit /b 1
)

REM ----------------------------------------
REM Create .env if it doesn't exist
REM ----------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        echo.
        echo Creating .env from .env.example...
        copy ".env.example" ".env" >nul
    )
)

REM ----------------------------------------
REM Initialize database
REM ----------------------------------------
echo.
echo Initializing AlphaLab database...
echo.

.venv\Scripts\python.exe scripts\init_db.py

if errorlevel 1 (
    echo.
    echo WARNING: Database initialization returned an error.
    echo Continuing to dashboard...
)

REM ----------------------------------------
REM Start Streamlit
REM ----------------------------------------
echo.
echo ========================================
echo        Starting AlphaLab Dashboard
echo ========================================
echo.
echo AlphaLab location:
echo %PROJECT_DIR%
echo.
echo Opening Streamlit...
echo.

.venv\Scripts\python.exe -m streamlit run app\dashboard\main.py

echo.
echo AlphaLab has stopped.
pause
