"""Validates an uploaded zip, stores it as submissions/<id>/source.zip, and runs the checks.

Checks (DESIGN.md section 1): safe zip (size, no absolute paths, no "..", no links), bot.toml
at the root or one folder down with language c/cpp/python, no environment-variable reads,
then one sandboxed game against the reference bot on the smallest map.
"""

# TODO(build order 2): accept(owner, bot, zip_bytes, parent, note, lane) -> submission id
