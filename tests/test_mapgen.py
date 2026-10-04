"""Generated suites (arena/maps.py): mapgen maps added beside worldgen's, each part by its prefix."""

from arena import config, maps


def cfg(tmp_path, **m):
    return config.Config(raw={"maps": {"holdout": str(tmp_path / "holdout"), "rotating": str(tmp_path / "rot"), **m}},
                         data_dir=tmp_path / "d")


def test_rotating_mapgen_and_trick_parts(tmp_path):
    c = cfg(tmp_path, rotating_count=0, rotating_mapgen=11, rotating_trick=3)
    d = maps.ensure_rotating(c)
    names = sorted(p.name for p in d.glob("*.map"))
    assert len([n for n in names if n.startswith("mg")]) == 11 and len([n for n in names if n.startswith("mt")]) == 3
    assert all(maps.map_name(p).startswith("gen-") for p in d.glob("mg*.map"))
    families = {maps.map_name(p).split("-")[1] for p in d.glob("mg*.map")}
    assert len(families) >= 10                          # consecutive seeds cover the families
    assert all("gen-trick-" in maps.map_name(p) for p in d.glob("mt*.map"))
    first = {p.name: p.read_bytes() for p in d.glob("*.map")}
    maps.ensure_rotating(c)                              # complete: nothing is rewritten
    assert {p.name: p.read_bytes() for p in d.glob("*.map")} == first


def test_holdout_keeps_existing_maps_and_drops_a_part_set_to_zero(tmp_path):
    c = cfg(tmp_path, holdout_count=2, holdout_mapgen=0, holdout_trick=2)
    d = tmp_path / "holdout"
    d.mkdir()
    old = {f"h{i}.map": f"MAP 9 9\nEND\n# old {i}\n" for i in range(2)}
    for n, t in old.items():
        (d / n).write_text(t)
    (d.with_name("holdout.salt")).write_text("12345")
    maps.ensure_holdout(c)
    assert {p.name: p.read_text() for p in d.glob("h[0-9]*.map")} == old    # worldgen part untouched
    assert sorted(p.name for p in d.glob("ht*.map")) == ["ht2012345.map", "ht2012346.map"]
    maps.ensure_holdout(cfg(tmp_path, holdout_count=2, holdout_trick=0))
    assert not list(d.glob("ht*.map")) and len(list(d.glob("h[0-9]*.map"))) == 2
