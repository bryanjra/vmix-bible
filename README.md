# vMix Bible engine

Live Reina-Valera 1960 (RVR1960) Spanish Bible projection for vMix, driven by
a local FastAPI server, a SQLite verse index, and a small natural-language
parser backed by Claude Haiku.

It exists so a media operator can say or type **"dame Juan uno del uno al
diez"** mid-service, see the first verse on the projector instantly, and
advance one verse at a time from a phone.

The projection state is a **queue** of readings. Manual searches and accepted
voice suggestions append to it; the operator can tap an item to project it,
step verse-by-verse across the whole queue with Next/Prev, or delete entries
they don't need.

---

## TL;DR

```bash
git clone https://github.com/bryanjra/vmix-bible.git
cd vmix-bible
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

claude login                       # or:  export ANTHROPIC_API_KEY=sk-ant-...
./bin/serve                        # http://127.0.0.1:8080
```

Then in vMix: **Add → Input → Web Browser**, URL `http://<host>:8080/present`,
1920×1080, **Transparency: ✔**. Search and project verses from
`http://<host>:8080/search`; advance live from `http://<host>:8080/control`.

Detailed walkthrough below.

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
git clone https://github.com/bryanjra/vmix-bible.git
cd vmix-bible

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

### 6. Optional: voice-listening companion (agentic suggestions)

The server can listen to the preacher's audio and propose verses in real
time. Suggestions appear on `/control` in the **Sugerencias** panel; the
operator taps **✓** to project or **✕** to dismiss. Nothing reaches the
projector without explicit confirmation.

The listener runs in-process — the audio device, channel, and whisper
model are picked from the `/control` UI. One Python process, one
`requirements.txt`, no extra daemon.

#### Install (Windows, native PowerShell)

The full server already includes the listener — installing
`requirements.txt` pulls `faster-whisper`, `sounddevice`, and `numpy`
alongside FastAPI. The `sounddevice` Windows wheel bundles PortAudio, so
no separate system install is needed.

```powershell
git clone https://github.com/bryanjra/vmix-bible.git
cd vmix-bible

py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# Optional: pre-cache the whisper model so first "Activar" tap is fast.
.venv\Scripts\python -c "from faster_whisper import WhisperModel; WhisperModel('small')"
#   small  ≈ 500 MB — real-time on a recent CPU, good Spanish accuracy
#   medium ≈ 1.5 GB — ~1.5× CPU, noticeably cleaner book names + numbers
```

Start the server:

```powershell
.venv\Scripts\python -m uvicorn server.app:app --host 127.0.0.1 --port 8080
```

(`bin/serve` is bash and only works in WSL / POSIX shells. On native
Windows use the `uvicorn` command above, or wrap it in a `.ps1`.)

#### Using it during service

1. Server running, `/control` open on the operator's phone or a tab.
2. In the **Escucha** card at the top of `/control`:
   - **Entrada** — pick the audio device. For a built-in mic this is
     usually *Microphone Array (Realtek)* or similar. For a Behringer
     audio interface (UMC1820, X-Air XR18, …), pick the device by name.
   - **Canal** — for a multichannel interface, pick the specific channel
     the preacher mic / aux send is on (1-based). For a normal mic, leave
     it at `1`.
   - **Modelo** — `small` (default) or `medium`. Change only if you've
     pre-cached the larger model.
3. Tap **Activar**. The status line shows `loading small…` while the
   model warms up (a few seconds after the first time), then `listening`.
4. As the preacher names references ("abramos en Juan tres dieciséis"),
   suggestions appear in the **Sugerencias** panel. Each one has three
   buttons:
   - **➕** — append to the queue, don't project yet (lets you batch
     several before deciding when to switch).
   - **▶** — append to the queue *and* jump to it on the projector.
   - **✕** — dismiss.
5. The **Cola** panel below shows everything queued so far. Tap any item
   to project it from verse 1; tap **×** to remove it; **Vaciar cola**
   empties everything.
6. **Next / Prev** step verse-by-verse and roll across the queue at item
   boundaries — keep tapping Next to flow through the whole service.
7. **Limpiar pantalla** stops projecting but keeps the queue intact, so
   you can pick up the same reading later.
8. Tap **Desactivar** to go fully manual — clears pending suggestions,
   stops the audio stream, and ignores incoming utterances until
   re-enabled.

Changing **Entrada**, **Canal**, or **Modelo** while listening is ON
restarts the stream with the new settings — useful for switching between a
laptop mic and the Behringer mid-setup.

#### Cue words

Cue words are the local heuristic that decides whether a transcribed
utterance is worth resolving (alongside digits and known book names). Edit
them live from `/control` → **Configuración ⚙** — type a word and press
Enter to add, tap **×** on a chip to remove. The list is normalized
(lowercased, accents stripped, blanks dropped) and persisted to
`cue-words.json` at the repo root (gitignored, so each install can tune
to its own preacher).

Defaults ship in `server/suggest.py::DEFAULT_CUE_WORDS`. If
`cue-words.json` is absent, the defaults are used.

#### Tuning

| Knob | Where | Effect |
|---|---|---|
| Cue words | `/control` → **Configuración** | Words that trip the resolver |
| `MIN_UTTERANCE_LEN` | `server/suggest.py` | Shorter strings skip resolution |
| `DEDUP_SECONDS` | `server/suggest.py` | Same ref within this window is suppressed |
| `PENDING_TTL` | `server/suggest.py` | Pending suggestions auto-expire |
| `HAIKU_GATE_MAX_PER_MIN` | `server/suggest.py` | Hard ceiling on Haiku calls |
| `SILENCE_RMS` | `server/listener.py` | VAD threshold (raise if ambient noise leaks in) |

If false positives are high, trim cue words from `/control` or lower
`HAIKU_GATE_MAX_PER_MIN`. If recall is low, pick `medium` in the UI.

#### Troubleshooting

**No devices in the **Entrada** dropdown** — Windows hides inputs that
are disabled or have no permission. Open *Settings → Privacy & security
→ Microphone* and enable desktop-app access. For the Behringer, install
the ASIO / WDM driver and confirm the device appears in *Sound settings
→ Input*. Refresh `/control`.

**Status shows `error: missing audio deps`** — `requirements.txt` didn't
install the audio wheels. Re-run `.venv\Scripts\pip install -r requirements.txt`
and restart the server.

**Status shows `error: audio stream failed`** — usually the device is in
use by another app or the picked channel exceeds the device's input
count. Close the other app or pick a lower channel.

**Listener catches music / ambient noise** — raise `SILENCE_RMS` in
`server/listener.py` (0.012 default), or route a dedicated aux send
instead of the full mix.

**Model download is slow** — `faster-whisper` pulls from HuggingFace
the first time `Activar` is tapped (~500 MB for `small`, ~1.5 GB for
`medium`). Pre-cache as shown above.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/present` | Projection page (vMix Web Browser Input target) |
| GET  | `/control` | Operator page (queue, Next/Prev/Clear, listener, cue-words) |
| GET  | `/search`  | Search box that takes Spanish/English/dictation |
| GET  | `/state`   | Current state (debugging) |
| WS   | `/live`    | Real-time state stream; also accepts `next`/`prev`/`clear`/`goto:N` |
| POST | `/verse`   | CLI back-compat: replace the queue with one item and play it (`{reference, verses[]}`) |
| POST | `/next`    | Advance one verse — rolls into the next queue item at the boundary |
| POST | `/prev`    | Back one verse — rolls into the previous queue item at the boundary |
| POST | `/goto/{n}` | Jump to verse `n` (0-based) within the current item |
| POST | `/clear`   | Stop projecting (keeps the queue intact) |
| POST | `/queue`   | Append: `{reference?, verses[], source?, play?}` — `play=true` also jumps to it |
| POST | `/queue/{id}/play`   | Jump to that queue item (verse_index = 0) |
| POST | `/queue/{id}/delete` | Remove from queue (auto-advance if it was current) |
| POST | `/queue/clear`       | Empty the queue |
| POST | `/search`  | Smart router: `{prompt, project, play}` → `{verses[], ref, path, elapsed_ms, ...}`. `project=true` appends to queue; `play` controls auto-jump. |
| POST | `/listen`  | Toggle the agentic listener: `{on: bool}`. ON loads whisper and opens the audio stream (non-blocking; status streams over `/live`). OFF stops the stream and clears pending. |
| GET  | `/audio/devices` | List input devices PortAudio can see |
| POST | `/audio/config` | Pick the input: `{device: int, channel: int, model: str}` — restarts the stream if already listening |
| POST | `/suggest` | External STT entry point: `{utterance, ts?}` |
| POST | `/suggest/{id}/queue` | Accept a suggestion to the queue only |
| POST | `/suggest/{id}/play`  | Accept a suggestion and jump to it |
| POST | `/suggest/{id}/reject` | Dismiss a suggestion |
| GET  | `/cue-words` | List the cue words used by `looks_versey` |
| POST | `/cue-words` | Replace the cue-word list (`{words: [...]}`); persists to `cue-words.json` |

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
│   │                              /next, /prev, /goto, /clear, WS /live,
│   │                              /listen, /suggest, /audio/*
│   ├── parser.py                ← Haiku-backed Spanish dictation → reference parser
│   ├── suggest.py               ← cue-word heuristic + Haiku rate gate for listener
│   ├── listener.py              ← in-process audio capture + faster-whisper STT
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
| `faster-whisper` | In-process STT for the agentic listener | `>=1.0` |
| `sounddevice` | PortAudio bindings — input device + channel selection | `>=0.4.6` |
| `numpy` | Audio buffer math for VAD segmentation | `>=1.24` |

Python stdlib only for the CLI (`bin/verse`) — no external deps, by design,
so the operator can rely on it even if the venv is broken.

---

## License & attribution

RVR1960 text is in the public domain in most jurisdictions; verify for your
own use. The serving code in this repo is yours to license as you see fit.
