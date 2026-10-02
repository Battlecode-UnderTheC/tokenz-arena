# Sprint tournament maps

The 7 maps every Sprint tournament match used: Australia (64×64), Islands (56×40), Maze (48×24),
Stripes (24×12), Tower Defense (32×16), Around UNSW (64×64) and weakhold (40×15).

These are the official files from unswbc 1.2.7 (`templates/maps`; `unsw.map` is
`around_unsw.map` here), with the real bed respawn ranges. They replace our earlier copies,
which were rebuilt from tournament replays with inferred bed ranges. The competition server
plays the same files, so on an arena with the server bridge, the `server` suite supersedes
these (same content). Games played on the old inferred copies are history: they no longer count,
and the arena plays them again (map versions, `arena/maps.py`).

Future tournaments (Qualifiers, Grand Final) may use new maps again, so no bot may key on map
identity.
