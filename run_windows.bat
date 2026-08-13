@echo off
REM Installs dependencies, validates the analyzer, then starts the dashboard.
setlocal

python -m pip install -r requirements.txt || goto :error

echo.
echo === Verifying KLayout ===
python -c "import klayout.db as db; print('KLayout OK:', db.__version__)" || goto :error

echo.
echo === Running tests ===
python -m pytest -q || goto :error

echo.
echo === Analyzing the reference samples ===
python analyze.py data/samples/NR2D1_1_RT_4.gds data/samples/NR2D1_2_RT_4.gds --quiet || goto :error

echo.
echo === Starting Streamlit ===
streamlit run app.py
goto :eof

:error
echo.
echo Setup failed. Fix the error above before starting the dashboard.
exit /b 1
