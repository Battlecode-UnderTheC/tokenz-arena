# tokenz-arena: the team guide

tokenz-arena is our shared league for UNSW Battlecode dragons bots. You (or your agents)
upload a bot. The arena plays it with the **official judge** (`unswbc run --sandbox`, the
same version as the competition server) against its parent and the current top bots, and
tells you whether it's better. Every result, replay and source file is shared, so we build
on each other's best bots instead of guessing.

It also watches the competition server. It downloads every battle we play there, and it
uploads the arena's best bot when it's clearly better than our live one.

**Address:** {ARENA}

## 1. Getting in

The arena is on Tailscale only, and there's no account or password.

1. Install Tailscale and get access to the `tokenz-arena` machine. Ask syeyoung to invite
   you to the tailnet, or to share the machine with you.
2. Open {ARENA}. The Submit page shows "Submitting as *your login*". That's how the arena
   knows you: anything running on your laptop, your agents included, acts as you.

## 2. The website

| Page | What it's for |
|---|---|
| [Leaderboard]({ARENA}/) | Ranked bots: rating ± error, record, status. Recent submissions with their verdicts. |
| `/bots/<bot>` | A bot's history: every version, its note, verdict, lineage and head-to-head. |
| `/s/<id>` | One submission: its note, the verdict against its parent and the top bots, where it loses (by map, by opponent, how its dragons die, CPU), its games, a source download and **Promote**. |
| `/games/<id>` | Watch a game in the browser, using the toolkit's own replay viewer. |
| [Ideas]({ARENA}/ideas) | Search everything that's been tried, with outcomes. Check here before starting an idea. |
| [Maps]({ARENA}/maps) | Per-map stats and map previews. |
| [Queue]({ARENA}/queue) | What's playing now and what's waiting. |
| [Server]({ARENA}/server) | The competition server: our live bot, rank, battles (watchable), quota, and the auto-submit gate. |
| [Submit]({ARENA}/submit) | Upload a zip from the browser. |

## 3. The `tokenz` command

`tokenz` is one Python file with no dependencies, served by the arena itself.

```sh
curl -fsSL {ARENA}/tokenz.py -o ~/.local/bin/tokenz && chmod +x ~/.local/bin/tokenz
tokenz whoami          # check it can reach the arena and who you are
tokenz --help
```

`tokenz update` fetches the latest version later. It already knows the arena's address;
override it with `--arena URL` if you ever need to.

### Your first submission

```sh
tokenz leaderboard                       # pick a strong bot to build on, e.g. swarm-6
tokenz report swarm-6                    # where it loses
tokenz ideas lunge                       # has someone tried your idea already?
tokenz get swarm-6 ./swarm               # its source, to change
# ... change one thing in ./swarm ...
tokenz submit ./swarm --bot swarm --parent swarm-6 \
    --note "Hypothesis: lunging at adjacent heads wins trades. Change: lunge when ..." --wait
```

`submit` zips the directory, leaving out `.git`, build output, replays and the like. If the
directory is in a GitHub repo, it attaches a link to the directory at the current commit,
and warns you if you have uncommitted or unpushed changes. With `--wait` it then prints the
verdict:

```
swarm-7  [draft, done]  owner alice@github
  parent   swarm-6
  verdict  (complete)
    vs parent swarm-6: 17-2-11  60.0%  -> BEATS parent
    vs trading-swarm-1: 14-0-16  46.7%
```

If it beats its parent, `tokenz promote swarm-7` makes it ranked. It then plays everyone
and joins the leaderboard. If it doesn't beat its parent, leave it as is: its note and
numbers stay in Ideas, so nobody repeats the experiment blind.

### All commands

| Command | What it does |
|---|---|
| `tokenz submit DIR --bot NAME --note "..." [--parent ID] [--lane ranked] [--wait]` | Zip and submit (a draft by default). |
| `tokenz wait ID` · `tokenz show ID` | Wait for the verdict, or just show status and verdict. |
| `tokenz report ID` | Where it loses: opponents, maps, deaths, CPU, worst losses with watch links. |
| `tokenz promote ID` | Draft to ranked. |
| `tokenz get ID [DIR]` | Download a submission's source. |
| `tokenz leaderboard` (`lb`) | Current ranked bots. |
| `tokenz ideas [WORDS] [--bot B] [--outcome beat_parent]` | What's been tried, with outcomes. |
| `tokenz games [--sub ID] [--vs ID] [--map M] [--result L]` | Games, filtered. |
| `tokenz watch GAME` · `tokenz replay GAME` | Open a game in the browser, or download its replay. |
| `tokenz mine` · `tokenz whoami` | Your submissions; who the arena thinks you are. |
| `tokenz queue` | What's playing and waiting. |
| `tokenz server [battles\|leaderboard\|quota\|gate]` | The competition server. |
| `tokenz mcp` | How to add the arena's MCP server to your agent. |

Every command takes `--json` for scripts and agents, and `--agent LABEL` to say which of
your agents is calling.

## 4. Running agents against it

The arena is built for many agents at once. Give each worker agent:

- **The brief:** {ARENA}/docs/agents (also `docs/AGENTS.md`). It covers the loop
  (start from a strong bot, change one thing, draft, wait, promote only if it beats its
  parent) and the rules.
- **A way in**, one of:
  - **MCP**, the best option for agents that support it:
    `claude mcp add --transport http tokenz-arena {ARENA}/mcp`. The tools are `submit`
    (files or a zip), `wait`, `report`, `source`, `ideas`, `leaderboard`, `promote`, `games`,
    `server_status`, and more.
  - **`tokenz` with `--json`**, for agents with a shell.
  - **Plain HTTP:** {ARENA}/llms.txt documents the whole API on one page.
- **A label:** `--agent worker-3` (or `agent=` in MCP) shows as `you / worker-3` and lets
  you filter your own agents' work (`tokenz mine --agent-filter worker-3`).

Each person can have **8 drafts waiting** at once. All your agents share that cap, because
they share your login, and the arena returns HTTP 429 beyond it. Owners share the game
workers fairly, so one person's 30 agents don't starve everyone else.

## 5. How it judges

- **Draft:** plays its **parent** and the current **top 3**, on every official map, in
  both seats. Unranked. It usually takes a few minutes.
- **Ranked** (promoted or submitted with `--lane ranked`): plays its parent and every other
  bot's current version. It does this on the official maps and on the **server suite**,
  which is the maps and seeds the competition server has actually used. It becomes its
  bot's current version, and the previous one retires.
- **Ratings:** Bradley-Terry (Elo-style) over games between current versions, with a 95%
  error bar. Two bots within each other's error bars aren't really different. The
  head-to-head against the parent is the number to trust.
- **Hidden maps:** the top 4 also play on holdout maps that nobody sees. Only the totals are
  shown, so we can't overfit them.
- **Checks before any game:** the zip must be safe, `bot.toml` must parse, the bot must not
  read environment variables, and it must build and play one game. A bot that fails is
  `rejected` with the reason and the judge's log.

## 6. The competition server

- **Auto-submit:** when the arena's #1 is clearly better than our live bot, the arena
  uploads it to the server and activates it. That means a rating clearly above the live
  bot's, at least 55% head-to-head over 60+ games, and no worse on the hidden maps and the
  server's own maps. It also needs no errors and CPU safely under the cap. It does this at
  most once every 4 hours. The Server page shows the current candidate and exactly which
  condition holds it back. **Anyone can pause it** there.
- **Battles:** every battle we play on the server, whether autoscrim, ranked or a
  teammate's challenge, is downloaded and shows up as watchable games on the Server page.
- **Challenges:** `POST /api/v1/server/battles` (or the Server page) starts one. It checks
  the team's 60-games-an-hour quota first, and it logs who asked.

## 7. Rules and limits

- Bots must **not read environment variables** (`os.environ`, `getenv`). The competition
  forbids it and the arena rejects it.
- `bot.toml` goes at the zip's root, with `language = "py"`, `"python"`, `"c"` or `"cpp"`.
- The zip can be **4 MB** at most, and CPU stays under **100M points a turn**. `tokenz report`
  shows your headroom.
- Submissions are immutable, so a change is a new submission. Submitting identical files
  again returns the existing submission.
- Always name a `--parent` when you build on something, and write a real note: the
  hypothesis and the change. That's what makes Ideas useful.

## 8. Troubleshooting

| Problem | What to do |
|---|---|
| `can't reach the arena` | Make sure Tailscale is connected and you have access to `tokenz-arena`. |
| The arena calls you `ip:100.x.y.z` or a machine name | You're on a shared or tagged machine with no Tailscale user. Pass `--owner yourname`. |
| `rejected: reads environment variables` | Remove the `getenv` / `os.environ` read. Use a constant. |
| `rejected: didn't build or play` | Read the check log on the submission page. It's the judge's own output. Run `unswbc run <map> <bot> <bot> --sandbox` locally to reproduce it. |
| HTTP 429 | You have 8 drafts waiting. Wait for some to finish. |
| A draft has been `playing` for a long time | Check the [Queue]({ARENA}/queue). Placements of new ranked bots and other people's drafts share the workers. |

Questions or ideas for the arena itself: ask syeyoung, or open an issue in
`Battlecode-UnderTheC/tokenz-arena`.
