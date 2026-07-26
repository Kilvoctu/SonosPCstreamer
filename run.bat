@echo off
SETLOCAL

IF NOT EXIST ".venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing requirements...
pip install -r requirements.txt

echo Running main.py...
python main.py

ENDLOCAL
pause
