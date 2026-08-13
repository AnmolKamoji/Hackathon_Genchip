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


@pytest.fixture(autouse=True)
def _isolate_environment():
    """Restore os.environ after every test.

    The end-to-end tests execute app.py, which calls load_dotenv() and so loads
    ANTHROPIC_API_KEY into the process. That leaked into every test that ran
    afterwards and silently changed the provider chain and the prompt budget, so
    the llm tests passed alone and failed in the suite. Ordering-dependent tests
    are worse than none, hence restoring the environment here rather than in the
    one file that happens to trigger it.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
