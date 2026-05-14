# vMix Bible engine — project memory

Live RVR1960 Spanish Bible projection for vMix. FastAPI + SQLite/FTS5 +
Anthropic Haiku for dictation parsing. Single operator, single projection.

## Commands

```bash
./bin/serve                              # uvicorn on 127.0.0.1:8080
bin/verse "Juan 3:16"                    # lookup
bin/verse search "fe sin obras"          # FTS5
bin/verse --present "Mateo 5:3-12"       # push playlist; server must be up
bin/verse --next | --prev | --goto N | --clear
.venv/bin/python bin/build_bible_db.py   # rebuild bible-rvr1960.db (needs Postgres dump)
```

No test suite. Don't claim tests pass — there are none.

## Architecture (one paragraph)

`server/app.py` holds a single global `_state` dict (`verses`, `index`,
`reference`) under one `asyncio.Lock`. Three HTTP entry points
(`/search`, `/control`, plus the CLI `verse --present`) all mutate that
state and broadcast on the `/live` WebSocket. The server doesn't read
SQLite directly — it shells out to `bin/verse --json` as a subprocess
(`server/app.py:_verse_cli_json`). `server/parser.py` is the Haiku
fallback when the CLI's regex can't parse the input.

## Hard constraints

- **`bin/verse` is stdlib-only.** No `requests`, `httpx`, `anthropic`,
  etc. in that file. The operator must be able to run it even if `.venv`
  is broken. Use `urllib.request` for HTTP, `sqlite3` for DB.
- **Auth is read on every API call.** `server/parser.py:_build_client`
  re-reads `~/.claude/.credentials.json` each request so Claude Code's
  hourly token rotation lands automatically. Don't memoize the client.
- **Order is `ANTHROPIC_API_KEY` env > OAuth file > error.** Don't
  reverse it.
- **Python 3.9 target** (`pyrightconfig.json`). Existing code uses
  `from __future__ import annotations` + `Optional[X]`. Keep that style.
- **Server has no auth.** Defaults bind to `127.0.0.1`. If adding new
  endpoints, assume LAN-trusted; don't add features that would be unsafe
  on `0.0.0.0`.

## Gotchas

- `/live` WebSocket accepts inbound *text* as control commands —
  `next`, `prev`, `clear`, `goto:N` (`server/app.py:198`). Adding new
  message types means extending that handler, not just adding REST.
- `verse --present` calls `urlopen` with a 2s timeout. If the server's
  down, exit code is 2 and stderr points the operator at `bin/serve`.
- `bin/verse` exit codes are load-bearing: `0` match, `1` no match,
  `2` parse error. `server/app.py` checks `rc == 0` before reading
  results. Don't change them without updating the server.
- Spanish parsing is accent-tolerant via `unicodedata.normalize` in
  `bin/verse:normalize`. Don't add Unicode-naive comparisons elsewhere.
- `bible-rvr1960.db` lives at the repo root (not in `data/`). The CLI
  finds it via `Path(__file__).resolve().parent.parent`.
- `.gitignore` blocks `.env*`, `.claude/.credentials.json`,
  `.claude/settings.local.json`, `.claude/sessions/`. Don't add
  credential paths that bypass this.

## Code style

Default to no comments. The codebase favors short module docstrings +
one-line explanations for non-obvious branches. Match that. Type hints
everywhere; `Optional[X]` not `X | None` (Python 3.9).
