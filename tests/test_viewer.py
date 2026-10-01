from arena.web import viewer


def test_extract_and_page(tmp_path):
    viewer.extract(tmp_path)
    assert (tmp_path / "webview.js").stat().st_size > 100_000
    assert (tmp_path / "webview.css").exists()
    html = viewer.page("/api/v1/games/7/replay", "game 7")
    assert "acquireVsCodeApi" in html and '"/api/v1/games/7/replay"' in html
