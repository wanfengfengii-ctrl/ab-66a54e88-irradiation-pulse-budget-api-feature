import os
import tempfile

# Point the module-level app (app.main:app) at a throwaway database so imports
# never touch ./data in the repository. Must run before importing app.main.
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite:///{tempfile.mkdtemp(prefix='irr-default-')}/default.db",
)

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path}/test.db"


@pytest.fixture()
def app(db_url):
    return create_app(db_url)


@pytest.fixture()
def client(app):
    return TestClient(app)
