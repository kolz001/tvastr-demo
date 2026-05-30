from fastapi.testclient import TestClient

from tvastr.api import create_app

client = TestClient(create_app())


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "mock"


def test_remediate_replays_sample_logs():
    resp = client.post("/remediate", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["events_ingested"] > 0
    assert body["patterns_selected"] > 0
