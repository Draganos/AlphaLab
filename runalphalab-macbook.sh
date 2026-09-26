#!/bin/bash

set -e

REPO_URL="https://github.com/Draganos/AlphaLab.git"
PROJECT_DIR="$HOME/AlphaLab"

echo ""
echo "========================================"
echo "          AlphaLab Launcher"
echo "========================================"
echo ""

# ----------------------------------------
# Check Git
# ----------------------------------------
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: Git is not installed or is not in PATH."
    echo "Please install Git and run this file again."
    exit 1
fi

# ----------------------------------------
# Find Python 3.12+
# ----------------------------------------
PYTHON=""

if command -v python3.13 >/dev/null 2>&1; then
    PYTHON="python3.13"
elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON="python3.12"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    echo "ERROR: Python 3 is not installed."
    echo "AlphaLab requires Python 3.12 or newer."
    exit 1
fi

# ----------------------------------------
# Check Python version
# ----------------------------------------
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
    echo "ERROR: AlphaLab requires Python 3.12 or newer."
    "$PYTHON" --version
    exit 1
fi

echo "Using Python:"
"$PYTHON" --version
echo ""

# ----------------------------------------
# Clone or update repository
# ----------------------------------------
if [ ! -d "$PROJECT_DIR" ]; then

    echo "AlphaLab not found."
    echo "Cloning from GitHub..."
    echo ""

    git clone "$REPO_URL" "$PROJECT_DIR"

elif [ ! -d "$PROJECT_DIR/.git" ]; then

    echo "ERROR:"
    echo "$PROJECT_DIR already exists,"
    echo "but it is not a Git repository."
    exit 1

else

    echo "AlphaLab already exists."
    echo "Updating from GitHub..."
    echo ""

    cd "$PROJECT_DIR"

    git checkout main
    git pull origin main

fi

cd "$PROJECT_DIR"

# ----------------------------------------
# Create virtual environment
# ----------------------------------------
if [ ! -f ".venv/bin/python" ]; then

    echo ""
    echo "Creating Python virtual environment..."
    echo ""

    "$PYTHON" -m venv .venv

fi

# ----------------------------------------
# Install locked dependencies
# ----------------------------------------
echo ""
echo "Installing AlphaLab dependencies..."
echo ""

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock

# ----------------------------------------
# Install AlphaLab itself
# ----------------------------------------
echo ""
echo "Installing AlphaLab..."
echo ""

.venv/bin/python -m pip install --no-deps -e .

# ----------------------------------------
# Create .env if it doesn't exist
# ----------------------------------------
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo ""
    echo "Creating .env from .env.example..."
    cp .env.example .env
fi

# ----------------------------------------
# Initialize database
# ----------------------------------------
echo ""
echo "Initializing AlphaLab database..."
echo ""

.venv/bin/python scripts/init_db.py || {
    echo ""
    echo "WARNING: Database initialization returned an error."
    echo "Continuing to dashboard..."
}

# ----------------------------------------
# Start Streamlit
# ----------------------------------------
echo ""
echo "========================================"
echo "       Starting AlphaLab Dashboard"
echo "========================================"
echo ""
echo "AlphaLab location:"
echo "$PROJECT_DIR"
echo ""
echo "Opening Streamlit..."
echo ""

.venv/bin/python -m streamlit run app/dashboard/main.py
