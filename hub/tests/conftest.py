import os

# Deterministic: no LLM calls, no files on disk.
os.environ["HERBIE_LLM"] = "off"
os.environ["HERBIE_DB"] = ":memory:"
os.environ.pop("HERBIE_POI_SOURCE", None)

import pytest
from fastapi.testclient import TestClient

from herbie.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app(":memory:")) as c:
        yield c


@pytest.fixture
def run(client):
    """Load a scenario and return its proactive replies."""
    def _run(name: str) -> list[dict]:
        r = client.post(f"/api/scenario/{name}")
        assert r.status_code == 200, r.text
        return r.json()["replies"]
    return _run


@pytest.fixture
def say(client):
    def _say(text: str) -> dict:
        r = client.post("/api/say", json={"text": text})
        assert r.status_code == 200, r.text
        return r.json()["replies"][0]
    return _say


@pytest.fixture
def signal(client):
    def _signal(path: str):
        return client.get("/api/state").json()["world"]["signals"][path]
    return _signal
