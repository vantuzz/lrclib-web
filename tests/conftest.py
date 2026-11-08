import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lrclib_web.db import DB  # noqa: E402


@pytest.fixture
def music_dir(tmp_path):
    p = tmp_path / "music"
    p.mkdir()
    return str(p)


@pytest.fixture
def data_dir(tmp_path):
    p = tmp_path / "data"
    p.mkdir()
    return str(p)


@pytest.fixture
def db(data_dir):
    d = DB(os.path.join(data_dir, "t.sqlite3"))
    yield d
    d.close()
