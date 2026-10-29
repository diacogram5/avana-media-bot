import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path):
    from app.db import init_db

    conn = init_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()
