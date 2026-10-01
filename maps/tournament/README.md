# Sprint tournament maps (2026-10-01)

These are the 7 maps every Sprint tournament match used, in order: Australia (64×64), Islands (56×40), Maze (48×24), Stripes (24×12), Tower Defense (32×16), Around UNSW (64×64) and weakhold (40×15). None of them was in maps/official or maps/server. The bcweb tool (`tfetch sprint`) rebuilt them from the map text inside tournament replays, with dragon placement from our round-1 games.

Caveat: replays record every TILE bed range as `0 0`, so these files have NO real bed respawn ranges. Pearl timing in sim on these maps is therefore not faithful. Use them for layout and openings, and confirm economy claims on server replays. Future tournaments (Qualifiers, Grand Final) may use new maps again, which is why no bot may key on map identity.

**Update (strategist z4, 2026-10-01):** these files now carry bed ranges INFERRED from the 20 tournament replays of each map: approximate, but they reproduce the tournament in our sim (e.g. v11 vs arena-search-7: Tower Defense 0-4, weakhold 0-4, Stripes 4-0). Confirm economy claims on server replays.
