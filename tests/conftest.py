import os
import sys

# Make the src/ layout importable without an editable install.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest


@pytest.fixture
def fixture_csv() -> str:
    return os.path.join(os.path.dirname(__file__), "fixtures", "pos_sample.csv")
