"""The agent-facing surface: the MCP zip helpers, /llms.txt, and the MCP tools end to end
(in-process, over streamable HTTP, no daemon)."""

import asyncio
import io
import json
import zipfile

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from arena import config, submit
from arena.api import mcp as arena_mcp
from arena.api.http import create_app
from arena.state import Arena

BOT = {"bot.toml": '[project]\nlanguage = "py"\n', "main.py": "print('hi')\n"}


@pytest.fixture
def arena(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "one.map").write_text("MAP 4 4\n")
    cfg = config.Config(raw={"maps": {"official": str(maps)}}, data_dir=tmp_path / "data")
    return Arena(cfg)


# ------------------------------------------------------------------ zip helpers
def test_build_zip_is_deterministic_and_valid():
    a = arena_mcp.build_zip(BOT)
    b = arena_mcp.build_zip(dict(reversed(list(BOT.items()))))
    assert a == b                      # same files -> same bytes -> the duplicate check works
    z = zipfile.ZipFile(io.BytesIO(a))
    assert sorted(z.namelist()) == ["bot.toml", "main.py"]
    assert z.read("main.py").decode() == BOT["main.py"]
    files, skipped = arena_mcp.unzip_text(a)
    assert files == BOT and skipped == {}
    assert arena_mcp.build_zip({"./src/x.py": "1"}) == arena_mcp.build_zip({"src/x.py": "1"})


@pytest.mark.parametrize("bad", [{}, {"../x": ""}, {"/abs": ""}, {"a/../../b": ""},
                                 {"c:x": ""}, {"dir/": ""}, {"x": b"bytes"}])
def test_build_zip_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        arena_mcp.build_zip(bad)


def test_build_zip_passes_the_arena_checks(arena):
    s, new = submit.accept(arena, blob=arena_mcp.build_zip(BOT), bot="x", owner="a")
    assert new and s.status == "checking"


def test_unzip_text_skips_binary():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bot.toml", "x")
        z.writestr("lib/blob.bin", b"\x00\x01\x02")
        z.writestr("latin.txt", b"\xff\xfe")
        z.writestr("dir/", "")
    files, skipped = arena_mcp.unzip_text(buf.getvalue())
    assert files == {"bot.toml": "x"}
    assert set(skipped) == {"lib/blob.bin", "latin.txt"}


# ------------------------------------------------------------------ /llms.txt
def test_llms_txt(arena):
    s, _ = submit.accept(arena, blob=arena_mcp.build_zip(BOT), bot="swarm", owner="alice@github")
    submit.make_current(arena, s)
    with TestClient(create_app(arena)) as c:
        r = c.get("/llms.txt", headers={"host": "arena.example.ts.net",
                                        "x-forwarded-proto": "https"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    assert "https://arena.example.ts.net/mcp" in t
    assert "curl -F file=@bot.zip -F bot=swarm" in t
    for word in ("beat_parent", "lost_to_parent", "tied_parent", "no_parent", "pending",
                 "rejected", "4 MB", "?wait=", "/promote", "/api/v1/ideas",
                 "/api/v1/games?sub=", "/api/v1/stats/maps", "/api/v1/queue", "Tailscale"):
        assert word in t, word
    for tool in arena_mcp.TOOLS:
        assert tool + "(" in t, tool
    assert "1. swarm-1 (alice@github)" in t          # the live top 3


# ------------------------------------------------------------------ MCP end to end
def test_mcp_tools_over_streamable_http(arena):
    app = create_app(arena)

    async def run():
        async with app.router.lifespan_context(app):
            # The client's HTTP goes straight into the app, as if from tailscale serve.
            transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx2.AsyncClient(transport=transport, base_url="http://arena",
                                          headers={"Tailscale-User-Login": "alice@github",
                                                   "Authorization": "Bearer worker-3"}) as hc:
                async with Client(streamable_http_client("http://arena/mcp", http_client=hc)) as c:
                    names = {t.name for t in (await c.list_tools()).tools}
                    assert names == set(arena_mcp.TOOLS)
                    r = await c.call_tool("leaderboard", {})
                    assert not r.is_error and json.loads(r.content[0].text) == []
                    r = await c.call_tool("submit", {"bot": "swarm", "note": "first", "files": BOT})
                    d = json.loads(r.content[0].text)
                    assert not r.is_error, r.content[0].text
                    assert (d["id"], d["owner"], d["agent"]) == ("swarm-1", "alice@github", "worker-3")
                    r = await c.call_tool("source", {"id": "swarm-1"})
                    assert json.loads(r.content[0].text)["files"] == BOT
                    r = await c.call_tool("ideas", {"q": "first"})
                    assert json.loads(r.content[0].text)[0]["outcome"] == "pending"
                    r = await c.call_tool("submission", {"id": "nope-1"})
                    assert r.is_error and "no submission" in r.content[0].text
                    # The bridge is installed but has no key in tests: status says so, no error.
                    r = await c.call_tool("server_status", {})
                    assert not r.is_error, r.content[0].text

    asyncio.run(run())
