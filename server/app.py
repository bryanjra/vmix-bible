"""vMix Bible engine.

Local-only FastAPI app the `verse` CLI pushes into and vMix renders from.

Queue model: the projection state is a list of items (each item = one
reference with its verses). `current_item_id` points at what's playing;
`verse_index` is the line within it. Next/Prev step verse-by-verse, then
roll over to the next/previous item at the boundary.

  GET  /present          -> projection page (vMix Web Browser Input)
  GET  /control          -> operator control page
  WS   /live             -> live state stream
  POST /verse            -> CLI back-compat: replace queue with one item, play
  POST /next             -> advance verse (cross-item at boundaries)
  POST /prev             -> back one verse (cross-item at boundaries)
  POST /goto/{n}         -> jump to verse n within the current item
  POST /clear            -> stop projecting (keeps queue)
  POST /queue            -> append item; optional play=true to jump to it
  POST /queue/{id}/play  -> set current_item to id, verse_index=0
  POST /queue/{id}/delete-> remove item from queue
  POST /queue/clear      -> empty queue
  POST /search           -> smart router; project=true adds to queue, play=true also jumps
  POST /listen           -> toggle the agentic listener
  POST /audio/config     -> pick input device/channel/model
  GET  /audio/devices    -> enumerate input devices
  POST /suggest          -> external STT entry point
  POST /suggest/{id}/queue -> accept a suggestion to the queue only
  POST /suggest/{id}/play  -> accept a suggestion + jump to it
  POST /suggest/{id}/reject-> dismiss
  GET  /cue-words        -> list cue words used by `looks_versey`
  POST /cue-words        -> replace cue-word list (persists to disk)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .listener import AudioListener, default_input_index, list_input_devices
from .parser import parse_query
from .suggest import (
    MIN_UTTERANCE_LEN,
    PENDING_TTL,
    RateGate,
    RecentRefs,
    get_cue_words,
    load_cue_words,
    looks_versey,
    save_cue_words,
    set_cue_words,
)

ROOT = Path(__file__).resolve().parent.parent
VERSE_CLI = ROOT / "bin" / "verse"
CUE_WORDS_FILE = ROOT / "cue-words.json"

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Init listener, device list, and cue words on boot; stop the stream on shutdown."""
    del _app
    global _listener
    load_cue_words(CUE_WORDS_FILE)
    _state["audio_devices"] = list_input_devices()
    if _state["audio_config"]["device"] is None:
        d = default_input_index()
        if d is not None:
            _state["audio_config"]["device"] = d
    _listener = AudioListener(_on_listener_utterance)
    try:
        yield
    finally:
        if _listener is not None:
            _listener.stop()


app = FastAPI(title="vMix Bible engine", lifespan=_lifespan)


class Verse(BaseModel):
    book: str
    chapter: int
    verse: int
    text: str


class VersePayload(BaseModel):
    reference: Optional[str] = None
    verses: list[Verse]


# ---- state ----------------------------------------------------------------
# One operator, one projection — single global, single lock.
#
# queue: ordered list of items {id, ref, source, verses}
# current_item_id: which item is showing (id), or None
# verse_index: which verse within the current item is showing

_state: dict[str, Any] = {
    "queue": [],
    "current_item_id": None,
    "verse_index": 0,
    "listening": False,
    "suggestions": [],
    "audio_devices": [],
    "audio_config": {"device": None, "channel": 1, "model": "small"},
    "listener_status": "stopped",
    "listener_error": None,
}
_state_lock = asyncio.Lock()
_clients: set[WebSocket] = set()
_haiku_gate = RateGate()
_recent_refs = RecentRefs()
_listener: Optional[AudioListener] = None


# ---- queue helpers --------------------------------------------------------

def _find_item_idx(item_id: Optional[str]) -> Optional[int]:
    if item_id is None:
        return None
    for i, it in enumerate(_state["queue"]):
        if it["id"] == item_id:
            return i
    return None


def _current_item() -> Optional[dict[str, Any]]:
    idx = _find_item_idx(_state["current_item_id"])
    if idx is None:
        return None
    return _state["queue"][idx]


def _current_verse() -> Optional[dict[str, Any]]:
    item = _current_item()
    if item is None:
        return None
    verses = item["verses"]
    vi = _state["verse_index"]
    return verses[vi] if 0 <= vi < len(verses) else None


def _next_verse_preview() -> Optional[dict[str, Any]]:
    """The verse that would be shown after one more `Next` (may roll to next item)."""
    item = _current_item()
    if item is None:
        return None
    verses = item["verses"]
    vi = _state["verse_index"]
    if 0 <= vi + 1 < len(verses):
        return verses[vi + 1]
    # Roll into the first verse of the next queue item if there is one.
    idx = _find_item_idx(_state["current_item_id"])
    if idx is None:
        return None
    nxt = idx + 1
    if 0 <= nxt < len(_state["queue"]):
        nv = _state["queue"][nxt]["verses"]
        return nv[0] if nv else None
    return None


def _make_item(ref: str, verses: list[dict[str, Any]], source: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:8],
        "ref": ref,
        "source": source,         # "cli" | "search" | "suggestion"
        "verses": verses,
        "ts": time.time(),
    }


def _queue_view() -> List[dict[str, Any]]:
    """Compact queue projection for WS snapshots — no per-item verses[]."""
    return [
        {
            "id": it["id"],
            "ref": it["ref"],
            "source": it["source"],
            "total_verses": len(it["verses"]),
        }
        for it in _state["queue"]
    ]


# ---- snapshot / broadcast -------------------------------------------------

def _live_suggestions() -> list[dict[str, Any]]:
    now = time.time()
    return [s for s in _state["suggestions"] if now - s["ts"] < PENDING_TTL]


def _snapshot() -> dict[str, Any]:
    """Serialize state for /live + /state. Keeps `current`/`next`/`reference`/
    `index`/`total` fields for back-compat with present.html and bin/verse."""
    item = _current_item()
    current = _current_verse()
    upcoming = _next_verse_preview()
    return {
        "type": "state",
        # Queue model
        "queue": _queue_view(),
        "current_item_id": _state["current_item_id"],
        "verse_index": _state["verse_index"],
        # Back-compat fields (present.html, bin/verse)
        "reference": item["ref"] if item else None,
        "index": _state["verse_index"],
        "total": len(item["verses"]) if item else 0,
        "current": current,
        "next": upcoming,
        # Listener / agentic surface
        "listening": _state["listening"],
        "suggestions": _live_suggestions(),
        "audio_devices": _state["audio_devices"],
        "audio_config": _state["audio_config"],
        "listener_status": _state["listener_status"],
        "listener_error": _state["listener_error"],
        "cue_words": get_cue_words(),
    }


async def _broadcast() -> None:
    msg = _snapshot()
    dead: list[WebSocket] = []
    for ws in list(_clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


# ---- queue mutations (callers must hold _state_lock when noted) -----------

def _append_item_locked(item: dict[str, Any], play: bool) -> None:
    _state["queue"].append(item)
    if play:
        _state["current_item_id"] = item["id"]
        _state["verse_index"] = 0


def _replace_queue_locked(item: dict[str, Any]) -> None:
    _state["queue"] = [item]
    _state["current_item_id"] = item["id"]
    _state["verse_index"] = 0


# ---- CLI subprocess -------------------------------------------------------

async def _verse_cli_json(*args: str) -> tuple[int, dict[str, Any] | None]:
    """Run `verse --json <args>`. Returns (exit_code, parsed_json_or_None)."""
    proc = await asyncio.create_subprocess_exec(
        str(VERSE_CLI), "--json", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    rc = proc.returncode if proc.returncode is not None else -1
    if not stdout.strip():
        return rc, None
    try:
        return rc, json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return rc, None


def _build_ref_label(verses: list[dict[str, Any]], fallback: str) -> str:
    """Same grouping logic as the CLI's build_reference_label."""
    if not verses:
        return fallback
    groups: list[list[dict]] = []
    cur_key: tuple | None = None
    buf: list[dict] = []
    for r in verses:
        key = (r["book"], r["chapter"])
        if key != cur_key:
            if buf:
                groups.append(buf)
            buf = [r]
            cur_key = key
        else:
            buf.append(r)
    if buf:
        groups.append(buf)
    parts: list[str] = []
    for g in groups:
        b, ch = g[0]["book"], g[0]["chapter"]
        vs = [r["verse"] for r in g]
        if len(vs) == 1:
            parts.append(f"{b} {ch}:{vs[0]}")
        elif vs == list(range(vs[0], vs[-1] + 1)):
            parts.append(f"{b} {ch}:{vs[0]}-{vs[-1]}")
        else:
            parts.append(f"{b} {ch}:{','.join(str(v) for v in vs)}")
    return "; ".join(parts) or fallback


# ---- /verse (CLI back-compat) ---------------------------------------------

@app.post("/verse")
async def push_verse(payload: VersePayload) -> dict[str, Any]:
    """CLI back-compat: replace the queue with this single item and play it.
    `bin/verse --present` relies on this semantic."""
    verses = [v.model_dump() for v in payload.verses]
    ref = payload.reference or _build_ref_label(verses, fallback="(sin ref)")
    item = _make_item(ref, verses, source="cli")
    async with _state_lock:
        _replace_queue_locked(item)
    await _broadcast()
    return {"ok": True, **_snapshot()}


# ---- navigation -----------------------------------------------------------

@app.post("/next")
async def next_verse() -> dict[str, Any]:
    """Advance one verse; at end of item, roll to first verse of next item."""
    async with _state_lock:
        idx = _find_item_idx(_state["current_item_id"])
        if idx is None:
            # Nothing playing — start at the first queue item, if any.
            if _state["queue"]:
                _state["current_item_id"] = _state["queue"][0]["id"]
                _state["verse_index"] = 0
        else:
            item = _state["queue"][idx]
            if _state["verse_index"] + 1 < len(item["verses"]):
                _state["verse_index"] += 1
            elif idx + 1 < len(_state["queue"]):
                _state["current_item_id"] = _state["queue"][idx + 1]["id"]
                _state["verse_index"] = 0
            # else: at end of last item — stay put.
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/prev")
async def prev_verse() -> dict[str, Any]:
    """Back one verse; at start of item, roll to last verse of previous item."""
    async with _state_lock:
        idx = _find_item_idx(_state["current_item_id"])
        if idx is None:
            pass
        elif _state["verse_index"] > 0:
            _state["verse_index"] -= 1
        elif idx > 0:
            prev = _state["queue"][idx - 1]
            _state["current_item_id"] = prev["id"]
            _state["verse_index"] = max(0, len(prev["verses"]) - 1)
        # else: at start of first item — stay put.
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/goto/{n}")
async def goto(n: int) -> dict[str, Any]:
    """Jump to verse n (0-based) within the current item."""
    async with _state_lock:
        item = _current_item()
        if item is None:
            raise HTTPException(status_code=409, detail="nothing playing")
        last = len(item["verses"]) - 1
        _state["verse_index"] = max(0, min(n, last))
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/clear")
async def clear() -> dict[str, Any]:
    """Stop projecting. Queue is preserved — operator can resume by tapping an item."""
    async with _state_lock:
        _state["current_item_id"] = None
        _state["verse_index"] = 0
    await _broadcast()
    return {"ok": True, **_snapshot()}


# ---- queue endpoints ------------------------------------------------------

class QueueAppendPayload(BaseModel):
    reference: Optional[str] = None
    verses: list[Verse]
    source: str = "manual"
    play: bool = False


@app.post("/queue")
async def queue_append(payload: QueueAppendPayload) -> dict[str, Any]:
    verses = [v.model_dump() for v in payload.verses]
    ref = payload.reference or _build_ref_label(verses, fallback="(sin ref)")
    item = _make_item(ref, verses, source=payload.source)
    async with _state_lock:
        _append_item_locked(item, play=payload.play)
    await _broadcast()
    return {"ok": True, "item_id": item["id"], **_snapshot()}


@app.post("/queue/{item_id}/play")
async def queue_play(item_id: str) -> dict[str, Any]:
    async with _state_lock:
        if _find_item_idx(item_id) is None:
            raise HTTPException(status_code=404, detail="no such queue item")
        _state["current_item_id"] = item_id
        _state["verse_index"] = 0
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/queue/{item_id}/delete")
async def queue_delete(item_id: str) -> dict[str, Any]:
    async with _state_lock:
        idx = _find_item_idx(item_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="no such queue item")
        was_current = _state["current_item_id"] == item_id
        del _state["queue"][idx]
        if was_current:
            # Move to the next item if one exists at the same index, else prev, else None.
            if idx < len(_state["queue"]):
                _state["current_item_id"] = _state["queue"][idx]["id"]
            elif _state["queue"]:
                _state["current_item_id"] = _state["queue"][-1]["id"]
            else:
                _state["current_item_id"] = None
            _state["verse_index"] = 0
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/queue/clear")
async def queue_clear() -> dict[str, Any]:
    async with _state_lock:
        _state["queue"] = []
        _state["current_item_id"] = None
        _state["verse_index"] = 0
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.get("/state")
async def get_state() -> JSONResponse:
    return JSONResponse(_snapshot())


# ---- listener / suggestions ----------------------------------------------

class SuggestPayload(BaseModel):
    utterance: str = Field(min_length=1, max_length=2000)
    ts: Optional[float] = None


class ListenPayload(BaseModel):
    on: bool


class AudioConfigPayload(BaseModel):
    device: Optional[int] = None
    channel: int = Field(default=1, ge=1)
    model: str = Field(default="small")


class CueWordsPayload(BaseModel):
    words: list[str]


async def _evaluate_and_queue(utterance: str) -> bool:
    """Resolver: utterance → optional suggestion appended to _state['suggestions']."""
    if not _state["listening"]:
        return False
    u = utterance.strip()
    if len(u) < MIN_UTTERANCE_LEN or not looks_versey(u):
        return False

    source = "regex"
    rc, data = await _verse_cli_json(u)
    verses = (data or {}).get("results", []) if rc == 0 else []

    if not verses:
        if not _haiku_gate.allow():
            return False
        try:
            parsed, _ = await asyncio.to_thread(parse_query, u)
        except Exception:
            return False
        if parsed.get("mode") == "lookup" and parsed.get("ref"):
            rc, data = await _verse_cli_json(parsed["ref"])
            verses = (data or {}).get("results", []) if rc == 0 else []
            source = "ai"

    if not verses:
        return False

    ref = _build_ref_label(verses, fallback=u)
    if _recent_refs.seen(ref):
        return False
    _recent_refs.record(ref)

    sug = {
        "id": uuid.uuid4().hex[:8],
        "utterance": u,
        "ref": ref,
        "verses": verses,
        "ts": time.time(),
        "source": source,
    }
    async with _state_lock:
        if not _state["listening"]:
            return False
        _state["suggestions"].append(sug)
    await _broadcast()
    return True


async def _on_listener_utterance(utterance: str, _ts: float) -> None:
    del _ts
    await _evaluate_and_queue(utterance)


def _sync_listener_status() -> None:
    if _listener is None:
        return
    _state["listener_status"] = _listener.status
    _state["listener_error"] = _listener.error


async def _start_listener_bg(device: int, channel: int, model: str) -> None:
    """Background task: load whisper + open the stream. Broadcasts each step."""
    if _listener is None:
        return
    async with _state_lock:
        _state["listener_status"] = f"loading {model}…"
        _state["listener_error"] = None
    await _broadcast()
    await asyncio.to_thread(_listener.start, device, channel, model)
    _sync_listener_status()
    if _listener.status == "listening":
        async with _state_lock:
            _state["listening"] = True
    await _broadcast()


@app.post("/listen")
async def set_listen(payload: ListenPayload) -> dict[str, Any]:
    if payload.on:
        cfg = _state["audio_config"]
        if cfg["device"] is None:
            raise HTTPException(status_code=409, detail="no audio device configured")
        if _listener is None:
            raise HTTPException(status_code=503, detail="listener not initialized")
        asyncio.create_task(
            _start_listener_bg(int(cfg["device"]), int(cfg["channel"]), str(cfg["model"]))
        )
    else:
        async with _state_lock:
            _state["listening"] = False
            _state["suggestions"] = []
        if _listener is not None:
            await asyncio.to_thread(_listener.stop)
            _sync_listener_status()
        await _broadcast()
    return {"ok": True, **_snapshot()}


@app.get("/audio/devices")
async def audio_devices() -> dict[str, Any]:
    devices = list_input_devices()
    _state["audio_devices"] = devices
    return {"devices": devices, "config": _state["audio_config"]}


@app.post("/audio/config")
async def set_audio_config(payload: AudioConfigPayload) -> dict[str, Any]:
    cfg = {"device": payload.device, "channel": payload.channel, "model": payload.model}
    async with _state_lock:
        _state["audio_config"] = cfg
    if _state["listening"] and _listener is not None and payload.device is not None:
        await asyncio.to_thread(
            _listener.start, int(payload.device), int(payload.channel), str(payload.model)
        )
        _sync_listener_status()
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/suggest")
async def suggest(payload: SuggestPayload) -> Response:
    await _evaluate_and_queue(payload.utterance)
    return Response(status_code=204)


# Suggestion confirmation — two actions and a dismiss.

async def _pop_suggestion(sid: str) -> Optional[dict[str, Any]]:
    async with _state_lock:
        sug = next((s for s in _state["suggestions"] if s["id"] == sid), None)
        if sug is None:
            return None
        _state["suggestions"] = [s for s in _state["suggestions"] if s["id"] != sid]
    return sug


async def _accept_suggestion(sid: str, play: bool) -> bool:
    sug = await _pop_suggestion(sid)
    if sug is None:
        return False
    item = _make_item(sug["ref"], sug["verses"], source="suggestion")
    async with _state_lock:
        _append_item_locked(item, play=play)
    await _broadcast()
    return True


async def _reject_suggestion(sid: str) -> bool:
    sug = await _pop_suggestion(sid)
    if sug is None:
        return False
    await _broadcast()
    return True


@app.post("/suggest/{sid}/queue")
async def suggest_queue(sid: str) -> dict[str, Any]:
    if not await _accept_suggestion(sid, play=False):
        raise HTTPException(status_code=404, detail="no such suggestion")
    return {"ok": True, **_snapshot()}


@app.post("/suggest/{sid}/play")
async def suggest_play(sid: str) -> dict[str, Any]:
    if not await _accept_suggestion(sid, play=True):
        raise HTTPException(status_code=404, detail="no such suggestion")
    return {"ok": True, **_snapshot()}


@app.post("/suggest/{sid}/reject")
async def reject_suggestion(sid: str) -> dict[str, Any]:
    if not await _reject_suggestion(sid):
        raise HTTPException(status_code=404, detail="no such suggestion")
    return {"ok": True, **_snapshot()}


# ---- cue words ------------------------------------------------------------

@app.get("/cue-words")
async def cue_words_get() -> dict[str, Any]:
    return {"words": get_cue_words()}


@app.post("/cue-words")
async def cue_words_post(payload: CueWordsPayload) -> dict[str, Any]:
    cleaned = set_cue_words(payload.words)
    save_cue_words(CUE_WORDS_FILE)
    await _broadcast()
    return {"ok": True, "words": cleaned}


# ---- WebSocket ------------------------------------------------------------

@app.websocket("/live")
async def live(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_json(_snapshot())
        while True:
            # Inbound text is for keyboard-friendly nav only (next/prev/clear/goto).
            # Queue/suggestion/listener actions go via HTTP.
            data = await ws.receive_text()
            cmd = data.strip().lower()
            if cmd == "next":
                await next_verse()
            elif cmd == "prev":
                await prev_verse()
            elif cmd == "clear":
                await clear()
            elif cmd.startswith("goto:"):
                try:
                    await goto(int(cmd.split(":", 1)[1]))
                except (ValueError, HTTPException):
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# ---- page routes ----------------------------------------------------------

@app.get("/present")
async def present() -> FileResponse:
    return FileResponse(STATIC / "present.html")


@app.get("/control")
async def control() -> FileResponse:
    return FileResponse(STATIC / "control.html")


@app.get("/search")
async def search_page() -> FileResponse:
    return FileResponse(STATIC / "search.html")


class SearchPayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    project: bool = False
    play: bool = True   # only consulted when project=true


@app.post("/search")
async def smart_search(payload: SearchPayload) -> dict[str, Any]:
    """Smart router: regex first, Haiku fallback. project=true appends to queue;
    play controls whether to also jump to it. project=false just returns verses."""
    t_start = time.time()
    path = "regex"
    parsed: dict[str, Any] = {}
    parse_ms = 0

    rc, data = await _verse_cli_json(payload.prompt)
    verses = (data or {}).get("results", []) if rc == 0 else []

    if not verses:
        try:
            parsed, parse_seconds = await asyncio.to_thread(parse_query, payload.prompt)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"parser failed: {e}")
        parse_ms = int(parse_seconds * 1000)
        path = "ai"

        mode = parsed.get("mode")
        if mode == "lookup" and parsed.get("ref"):
            rc, data = await _verse_cli_json(parsed["ref"])
        elif mode == "search" and parsed.get("query"):
            rc, data = await _verse_cli_json("search", parsed["query"])
        else:
            rc, data = 1, None
        verses = (data or {}).get("results", []) if rc == 0 else []

    fallback_label = (
        parsed.get("ref")
        or (f"search: {parsed['query']}" if parsed.get("query") else payload.prompt)
    )
    ref_label = _build_ref_label(verses, fallback=fallback_label)

    queued_id: Optional[str] = None
    if payload.project and verses:
        item = _make_item(ref_label, verses, source="search")
        async with _state_lock:
            _append_item_locked(item, play=payload.play)
        queued_id = item["id"]
        await _broadcast()

    return {
        "ok": bool(verses),
        "path": path,                                            # "regex" | "ai"
        "mode": parsed.get("mode", "lookup" if verses else "unclear"),
        "ref": ref_label if verses else None,
        "query": parsed.get("query"),
        "note": parsed.get("note"),
        "verses": verses,
        "count": len(verses),
        "elapsed_ms": int((time.time() - t_start) * 1000),
        "parse_ms": parse_ms,
        "projected": bool(payload.project and verses),
        "played": bool(payload.project and payload.play and verses),
        "queued_item_id": queued_id,
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "present": "/present",
        "control": "/control",
        "search": "/search",
        "state": "/state",
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")
