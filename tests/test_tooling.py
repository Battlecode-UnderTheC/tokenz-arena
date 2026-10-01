"""The tokenz client (zip building, source links) and the pages that serve it and the docs."""

import importlib.util
import subprocess
import zipfile
import io
from pathlib import Path

from fastapi.testclient import TestClient

from arena import config
from arena.api.http import create_app
from arena.config import ROOT
from arena.state import Arena

spec = importlib.util.spec_from_file_location("tokenz", ROOT / "client" / "tokenz.py")
tokenz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tokenz)


def test_build_zip_skips_junk_and_is_deterministic(tmp_path):
    (tmp_path / "bot.toml").write_text('[project]\nlanguage = "py"\n')
    (tmp_path / "main.py").write_text("print(1)\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.pyc").write_bytes(b"x")
    (tmp_path / "old.replay").write_bytes(b"x")
    blob, names = tokenz.build_zip(tmp_path)
    assert sorted(names) == ["bot.toml", "main.py"]
    assert tokenz.build_zip(tmp_path)[0] == blob
    assert sorted(zipfile.ZipFile(io.BytesIO(blob)).namelist()) == ["bot.toml", "main.py"]


def test_source_link_points_at_directory_and_commit(tmp_path):
    run = lambda *c: subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True)
    run("init", "-q")
    run("remote", "add", "origin", "git@github.com:Org/bots.git")
    (tmp_path / "bots" / "swarm").mkdir(parents=True)
    (tmp_path / "bots" / "swarm" / "bot.toml").write_text("x")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    link, warn = tokenz.source_link(tmp_path / "bots" / "swarm")
    assert link == f"https://github.com/Org/bots/tree/{sha}/bots/swarm"
    assert warn and "pushed" in warn


def test_served_client_docs_and_whoami(tmp_path):
    cfg = config.Config(raw={"api": {"public_url": "https://arena.example.ts.net"}},
                        data_dir=tmp_path / "data")
    c = TestClient(create_app(Arena(cfg)))
    src = c.get("/tokenz.py").text
    assert 'DEFAULT_ARENA = "https://arena.example.ts.net"' in src
    r = c.get("/docs")
    assert r.status_code == 200 and "tokenz" in r.text and "https://arena.example.ts.net/tokenz.py" in r.text
    assert c.get("/docs/agents").status_code == 200
    assert "{ARENA}" not in c.get("/docs.md").text
    me = c.get("/api/v1/whoami", headers={"Authorization": "Bearer worker-1"}).json()
    assert me["agent"] == "worker-1"
