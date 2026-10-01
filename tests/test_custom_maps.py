"""Team map uploads, checked by the real judge (plays one short game per upload)."""

import pytest
from fastapi.testclient import TestClient

from arena import config, scheduler
from arena.api.http import create_app
from arena.config import ROOT
from arena.state import Arena

MAP = (ROOT / "maps" / "official" / "default_small.map").read_text()


@pytest.fixture
def client(tmp_path):
    cfg = config.Config(raw={"maps": {"worldgen": False}}, data_dir=tmp_path / "data")
    arena = Arena(cfg)
    return TestClient(create_app(arena)), arena


def test_upload_public_hidden_and_remove(client):
    c, arena = client
    h = {"tailscale-user-login": "alice@github"}
    r = c.post("/api/v1/maps", files={"file": ("tiny.map", MAP)}, data={"note": "test"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["suite"] == "custom" and r.json()["owner"] == "alice@github"
    assert any(p.stem == "tiny" for p in scheduler.maps(arena, "custom"))
    assert c.post("/api/v1/maps", files={"file": ("again.map", MAP)}).status_code == 400  # same bytes
    bad = c.post("/api/v1/maps", files={"file": ("bad.map", "hello\n")})
    assert bad.status_code == 400 and "MAP" in bad.json()["detail"]
    r = c.post("/api/v1/maps", files={"file": ("secret.map", MAP + "\n# v2\n")},
               data={"hidden": "true"}, headers=h)
    assert r.status_code == 201 and r.json()["suite"] == "holdout"
    listing = c.get("/api/v1/maps").json()
    assert listing["hidden_uploads"] == 1
    assert not any(m["name"] == "secret" for m in listing["maps"])
    assert any(p.stem == "secret" for p in scheduler.maps(arena, "holdout"))
    assert c.delete("/api/v1/maps/custom/tiny", headers={"tailscale-user-login": "bob@github"}).status_code == 400
    assert c.delete("/api/v1/maps/custom/tiny", headers=h).status_code == 200
    assert not scheduler.maps(arena, "custom")
    assert c.get("/maps").status_code == 200
