"""Put the project root on sys.path so a bare `pytest` works, not just `python -m pytest`.

`python -m pytest` happens to work because Python adds the current directory to
sys.path, but the plain `pytest` entry point does not, and the tests import
top-level packages (`analyzer`, `ai`).
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Everything the code reads to choose a provider, a model or an effort level. Any of
# these arriving from the developer's .env changes what the tests measure.
CONFIG_VARS = (
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_EFFORT",
    "OPENAI_API_KEY", "OPENAI_MODEL",
    "OLLAMA_MODEL", "OLLAMA_HOST", "OLLAMA_THINK", "OLLAMA_KEEP_ALIVE",
)


@pytest.fixture(autouse=True)
def _isolate_environment():
    """Give every test the same empty configuration, then restore the process.

    Two separate leaks made these tests ordering-dependent. app.py calls
    load_dotenv() when the end-to-end tests execute it, and tools/factcheck.py calls
    it at *import* time, which is collection - before this fixture can snapshot
    anything. Restoring afterwards was therefore not enough: the .env values were
    already in the snapshot, and ANTHROPIC_MODEL=claude-sonnet-5 silently replaced
    the default the model-selection tests assert on.

    So clear the configuration as well as restoring it. A test that wants a key or a
    model sets it explicitly with monkeypatch, which is the only way it should ever
    be true - a suite whose result depends on the developer's .env is not a suite.
    """
    saved = dict(os.environ)
    for name in CONFIG_VARS:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
