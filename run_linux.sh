#!/usr/bin/env bash
# Installs dependencies, validates the analyzer, then starts the dashboard.
set -euo pipefail

PY="${PYTHON:-python3}"

# Prefer a local virtualenv so we never install into the system interpreter.
if [[ ! -d .venv ]]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "=== Verifying KLayout ==="
python -c "import klayout.db as db; print('KLayout OK:', db.__version__)"

echo
echo "=== Running tests ==="
python -m pytest -q

echo
echo "=== Analyzing the reference samples ==="
python analyze.py data/samples/NR2D1_1_RT_4.gds data/samples/NR2D1_2_RT_4.gds --quiet

if grep -qi microsoft /proc/version 2>/dev/null; then
  echo
  echo "NOTE: WSL2 has no AMD GPU passthrough. To use an AMD GPU for the AI"
  echo "      narrative, run Ollama on Windows (setx OLLAMA_HOST 0.0.0.0:11434)."
  echo "      The app auto-detects the Windows host gateway."
fi

echo
echo "=== Starting Streamlit ==="
streamlit run app.py
