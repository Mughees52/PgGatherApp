import os
import tempfile
from pathlib import Path

import pytest

# Point the app at throwaway storage/db before importing app modules.
_TMP = tempfile.mkdtemp(prefix="pggather-test-")
os.environ["PGGATHER_DATA_DIR"] = str(Path(_TMP) / "storage")
os.environ["PGGATHER_DB_PATH"] = str(Path(_TMP) / "test.db")

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    from app.db import init_db
    init_db()
    yield


@pytest.fixture
def sample_tsv() -> Path:
    p = FIXTURES / "sample_out.tsv"
    if not p.exists():
        pytest.skip("sample_out.tsv fixture not present (generate via a collection)")
    return p
