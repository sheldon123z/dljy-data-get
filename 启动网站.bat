@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 依赖安装失败，请确认已安装 Python 3.10 或更高版本。
  pause
  exit /b 1
)
python run.py serve
endlocal
