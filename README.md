# vMix Bible engine

Live Reina-Valera 1960 (RVR1960) Spanish Bible projection for vMix, driven by
a local FastAPI server, a SQLite verse index, and a small natural-language
parser backed by Claude Haiku.

It exists so a media operator can say or type **"dame Juan uno del uno al
diez"** mid-service, see the first verse on the projector instantly, and
advance one verse at a time from a phone.

---

## Architecture

```
                                                                          (any LAN client)
                                                                                  │
                                                                                  ▼
                                                                          ┌──────────────┐
                                                                          │ vMix Web     │
                                                                          │ Browser Input│  ← http://127.0.0.1:8080/present
                                                                          └──────┬───────┘
                                                                                 │ renders
              ┌──────────────────────────┐                                       ▼
              │ /search   (web textbox)  │──┐                            ┌──────────────────┐
              │ /control  (operator UI)  │──┤                            │ present.html     │
              │  CLI:  verse --present   │──┤   HTTP POST    ┌──────────►│ (transparent fade)│
              │        verse --next      │──┤  /verse        │   WS push └──────────────────┘
              │        verse --prev      │──┘  /next /prev ┌─┴────────────────┐
              └──────────────────────────┘     /goto /clear│ FastAPI engine   │
                                                           │  127.0.0.1:8080  │
                                                           │  (server/app.py) │
                                                           └─┬───────┬────────┘
                                                             │       │
                                              POST /search   │       │ subprocess
                                              ┌──────────────┘       └────────────┐
                                              ▼                                   ▼
                                       ┌──────────────┐                  ┌──────────────────┐
                                       │ parser.py    │   regex miss?    │ bin/verse        │
                                       │ Haiku via    │ ───────────────► │ SQLite + FTS5    │
                                       │ Anthropic API│                  │ (bible-rvr1960.db)│
                                       │ (OAuth from  │                  └──────────────────┘
                                       │  Claude Code)│
                                       └──────────────┘
```

Three entry points all converge on the same playlist state held in the
FastAPI process:

- **`/present`** — transparent 1920×1080 page that vMix reads as a Web Browser
  Input. Shows one verse at a time with fade transitions.
- **`/control`** — operator page (phone, second screen, laptop) with
  Next / Prev / Clear and keyboard arrows.
- **`/search`** — textarea you type Spanish or English into. Clean references
  resolve in ~70 ms (regex path); dictation / topic phrases use Haiku and
  return in ~1 s.

A "Project to vMix" toggle on `/search` pushes the result to the playlist;
otherwise the page just returns the verse text.

---

## Quick start on a fresh laptop

Tested on Oracle Linux 9 with Python 3.9. Anything POSIX with Python ≥3.9
should work; on Windows use WSL.

### 1. Clone and install deps

```bash
git clone <this repo> bible-api
cd bible-api

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Provide your own Anthropic credentials

The natural-language parser (`/search` AI path) calls Anthropic's API. **No
credentials live in this repo** — the server picks up whichever of the two
following sources the operator running it has set up. Use either one:

**Option A — Claude.ai OAuth (recommended if you already use Claude Code).**
The token is stored at `~/.claude/.credentials.json` (chmod 0600) and refreshed
by the Claude Code daemon. The server reads `Path.home()` on every call so it
automatically uses whoever is running it — there's nothing to configure.

```bash
# Install Claude Code: https://claude.ai/code
claude login        # interactive — pick "Claude.ai subscription"

# Verify:
jq -r '.claudeAiOauth | keys[]' ~/.claude/.credentials.json
# → accessToken, expiresAt, rateLimitTier, refreshToken, scopes, subscriptionType
```

**Option B — `ANTHROPIC_API_KEY`** (a console.anthropic.com API key).
Anything set in the env wins over OAuth:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # NEVER commit this — put it in your shell rc
./bin/serve
```

If neither is present, `/search` returns a clear error pointing here. The
regex/CLI fast path still works without any credentials — it just can't parse
Spanish dictation like "Juan uno del uno al diez".

### 3. Start the engine

```bash
./bin/serve
#   present:  http://127.0.0.1:8080/present
#   control:  http://127.0.0.1:8080/control
#   search:   http://127.0.0.1:8080/search
```

Leave it running. Optionally daemonize via systemd, screen, or tmux.

### 4. Point vMix at it

On the vMix machine:

1. **Add** → **Input** → **Web Browser**
2. **URL**: `http://<server-ip>:8080/present`
3. **Width**: 1920, **Height**: 1080
4. **Transparency**: ✔
5. (Optional) bind the input to a Hotkey or a Trigger so it's one click to
   show/hide.

If vMix is on a different machine, change `bin/serve`'s default `HOST` to
`0.0.0.0` (or set `HOST=0.0.0.0 ./bin/serve`). Bind only to your trusted
production LAN — the server has no auth.

### 5. Optional: install the Claude Code skill

If you also want to drive projection from Claude Code via the
`/bible-rvr1960` slash command (voice dictation → Claude → `verse --present`),
copy the skill folder:

```bash
mkdir -p ~/.claude/skills
cp -r path/to/this/repo/.claude-skills/bible-rvr1960 ~/.claude/skills/
```

(The skill itself isn't in this repo by default — it lives in
`~/.claude/skills/bible-rvr1960/SKILL.md` on the original machine. The CLI
runs fine without it.)

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/present` | Projection page (vMix Web Browser Input target) |
| GET  | `/control` | Operator page (Next/Prev/Clear, keyboard arrows) |
| GET  | `/search`  | Search box that takes Spanish/English/dictation |
| GET  | `/state`   | Current playlist state (debugging) |
| WS   | `/live`    | Real-time state stream; also accepts `next`/`prev`/`clear`/`goto:N` |
| POST | `/verse`   | Set the playlist explicitly: `{reference, verses[]}` |
| POST | `/next`    | Advance one verse |
| POST | `/prev`    | Back one verse |
| POST | `/goto/{n}` | Jump to 0-based index |
| POST | `/clear`   | Blank the projection |
| POST | `/search`  | Smart router: `{prompt, project}` → `{verses[], ref, path, elapsed_ms, ...}` |

---

## CLI usage

`bin/verse` is the only thing that touches the SQLite DB. It can run
standalone (no server required) for lookups, and it pushes to the running
server when you pass `--present`.

```bash
# Lookups (read-only)
bin/verse "Juan 3:16"
bin/verse "Juan 3:16-18"
bin/verse "Salmos 23"
bin/verse "primera de juan 4:8"
bin/verse "Juan 3:16; Romanos 8:28; 1 Juan 4:8"
bin/verse search "fe sin obras"
bin/verse --plain "Juan 3:16"          # text only, no header
bin/verse --json "Juan 3:16"           # machine-readable

# vMix projection (server must be running)
bin/verse --present "Mateo 5:3-12"     # playlist of 10, shows verse 1
bin/verse --next                        # advance one
bin/verse --prev
bin/verse --goto 4                      # 0-based index
bin/verse --clear

# Override the server URL if not on default port:
PRESENT_URL=http://192.168.1.50:8080 bin/verse --present "Juan 3:16"
```

Exit codes: `0` = match, `1` = no match, `2` = parse error.

---

## File layout

```
bible-api/
├── README.md                    ← this file
├── requirements.txt             ← Python deps
├── pyrightconfig.json           ← LSP config (points pyright at .venv)
├── .gitignore
│
├── bible-rvr1960.db             ← SQLite + FTS5 index. The canonical runtime data.
│
├── bin/
│   ├── verse                    ← lookup + search + projection CLI (stdlib only)
│   ├── serve                    ← launches uvicorn server.app:app on :8080
│   └── build_bible_db.py        ← rebuild .db from a Postgres dbrv1960 restore
│
├── server/
│   ├── __init__.py
│   ├── app.py                   ← FastAPI: /present, /control, /search, /verse,
│   │                              /next, /prev, /goto, /clear, WS /live
│   ├── parser.py                ← Haiku-backed Spanish dictation → reference parser
│   └── static/
│       ├── present.html         ← 1920×1080 transparent projection page (WS auto-reconnect)
│       ├── control.html         ← operator UI (touch + keyboard)
│       └── search.html          ← search textbox + structured result rendering
│
└── data/
    └── dbrv1960.backup          ← original Postgres dump (only needed to rebuild .db)
```

---

## Troubleshooting

**`/search` AI path returns "OAuth token expired" or 401**
The token expires every ~8 hours and the Claude Code daemon refreshes it.
If the server happens to read it during a gap, retry. If it persists,
run `claude` once to force a fresh login.

**`bin/verse --present` says "vMix engine unreachable"**
The server isn't running. Start it with `./bin/serve` from the repo root.
Or override `PRESENT_URL` if you're on a different host/port.

**vMix shows the page but with a black background**
Enable the Transparency checkbox on the Web Browser Input. The HTML uses
`background: transparent`, but vMix has to opt in.

**Pyright complains "Import 'fastapi' could not be resolved"**
Make sure your IDE picked up `pyrightconfig.json`, which points at `.venv`.
Restart the language server if needed. Runtime is unaffected.

**Search returns no match for a verse I know exists**
Try the headered output: `bin/verse "Juan 3:16"` (no flags). If it returns
nothing, the SQLite DB is missing or corrupt — re-copy `bible-rvr1960.db`
from the source machine, or rebuild it (see below).

---

## Rebuilding the SQLite DB (optional)

You only need this if you're changing the corpus. The shipped
`bible-rvr1960.db` is canonical for runtime.

```bash
# 1. Install + start Postgres
sudo dnf install -y postgresql-server   # or apt/brew equivalent
sudo /usr/bin/postgresql-setup --initdb
sudo systemctl enable --now postgresql

# 2. Restore the backup
sudo -u postgres createdb dbrv1960
sudo -u postgres pg_restore -d dbrv1960 data/dbrv1960.backup

# 3. Rebuild the SQLite DB + FTS5 index
.venv/bin/python bin/build_bible_db.py
# → writes bible-rvr1960.db at the project root
```

---

## Security notes

**No Anthropic credentials live in this repo.** The token / API key is the
responsibility of whoever runs the server:

- OAuth path: `~/.claude/.credentials.json`, chmod 0600, never copied into the
  repo. The server reads it via `Path.home()` at runtime, so each user
  automatically uses their own.
- API-key path: `ANTHROPIC_API_KEY` env var. Put it in your shell rc
  (`~/.bashrc`, `~/.zshrc`), `.env.local`, or a secrets manager — anywhere
  outside this repo. `.gitignore` already excludes `.env*` files as a guard.

The `.gitignore` also excludes `.claude/settings.local.json`, `.credentials.json`,
session files, and the venv — so even an accidental `git add -A` won't pull
them in. If you fork this repo, sweep your fork for these patterns once
before pushing:

```bash
grep -rE "sk-ant-|eyJ[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9]" . 2>/dev/null | grep -v .venv
# (empty output = clean)
```

**Server binding:** defaults to `127.0.0.1`. Don't expose it to the public
internet — there's no auth on the endpoints, and `/search` will happily burn
Anthropic API quota for whoever hits it. For LAN access from a separate vMix
machine, `HOST=0.0.0.0 ./bin/serve` is fine on a trusted production network;
add a reverse proxy with basic auth if you don't trust the segment.

---

## Dependencies

| Package | Purpose | Pinned |
|---|---|---|
| `fastapi` | HTTP + WebSocket server | `>=0.115` |
| `uvicorn[standard]` | ASGI runtime | `>=0.32` |
| `anthropic` | Claude API client (OAuth or API key) | `>=0.40` |

Python stdlib only for the CLI (`bin/verse`) — no external deps, by design,
so the operator can rely on it even if the venv is broken.

---

## License & attribution

RVR1960 text is in the public domain in most jurisdictions; verify for your
own use. The serving code in this repo is yours to license as you see fit.
