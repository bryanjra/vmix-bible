"""vMix Bible engine.

Local-only FastAPI app the `verse` CLI pushes into and vMix renders from.

  GET  /present       -> the projection page (vMix Web Browser Input points here)
  GET  /control       -> operator control page (phone / second screen)
  WS   /live          -> live state stream, broadcast to all clients on every change
  POST /verse         -> set the verse playlist (resets index to 0)
  POST /next          -> index += 1 (clamped)
  POST /prev          -> index -= 1 (clamped)
  POST /goto/{n}      -> index = n (0-based, clamped)
  POST /clear         -> blank the screen
  GET  /state         -> current state (for debugging)
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .parser import parse_query

VERSE_CLI = Path(__file__).resolve().parent.parent / "bin" / "verse"

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

app = FastAPI(title="vMix Bible engine")


class Verse(BaseModel):
    book: str
    chapter: int
    verse: int
    text: str


class VersePayload(BaseModel):
    reference: Optional[str] = None
    verses: list[Verse]


# Single global state. One operator, one projection — no need for sessions.
_state: dict[str, Any] = {"verses": [], "index": 0, "reference": None}
_state_lock = asyncio.Lock()
_clients: set[WebSocket] = set()


def _snapshot() -> dict[str, Any]:
    """Serialize state for a WS message. Includes the resolved current + next verse."""
    verses = _state["verses"]
    idx = _state["index"]
    current = verses[idx] if 0 <= idx < len(verses) else None
    upcoming = verses[idx + 1] if 0 <= idx + 1 < len(verses) else None
    return {
        "type": "state",
        "reference": _state["reference"],
        "index": idx,
        "total": len(verses),
        "current": current,
        "next": upcoming,
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


async def _set_playlist(reference: Optional[str], verses: list[dict[str, Any]]) -> None:
    async with _state_lock:
        _state["verses"] = verses
        _state["index"] = 0
        _state["reference"] = reference
    await _broadcast()


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


@app.post("/verse")
async def push_verse(payload: VersePayload) -> dict[str, Any]:
    await _set_playlist(payload.reference, [v.model_dump() for v in payload.verses])
    return {"ok": True, **_snapshot()}


@app.post("/next")
async def next_verse() -> dict[str, Any]:
    async with _state_lock:
        if _state["verses"]:
            _state["index"] = min(_state["index"] + 1, len(_state["verses"]) - 1)
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/prev")
async def prev_verse() -> dict[str, Any]:
    async with _state_lock:
        if _state["verses"]:
            _state["index"] = max(_state["index"] - 1, 0)
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/goto/{n}")
async def goto(n: int) -> dict[str, Any]:
    async with _state_lock:
        if not _state["verses"]:
            raise HTTPException(status_code=409, detail="no playlist loaded")
        last = len(_state["verses"]) - 1
        _state["index"] = max(0, min(n, last))
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.post("/clear")
async def clear() -> dict[str, Any]:
    async with _state_lock:
        _state["verses"] = []
        _state["index"] = 0
        _state["reference"] = None
    await _broadcast()
    return {"ok": True, **_snapshot()}


@app.get("/state")
async def get_state() -> JSONResponse:
    return JSONResponse(_snapshot())


@app.websocket("/live")
async def live(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_json(_snapshot())
        while True:
            # Treat any inbound text as a control message, for control.html convenience.
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


@app.post("/search")
async def smart_search(payload: SearchPayload) -> dict[str, Any]:
    """Smart router.

    1. Try the CLI's built-in parser first — clean refs like "Juan 3:16" or
       "Mateo 5:3-12" resolve in ~50ms with no LLM hop.
    2. On parse failure, call Haiku via direct API (cached system prompt) to
       extract a structured {mode, ref|query} from natural Spanish/English
       dictation. Then feed that to the CLI.

    Optionally projects the full result as a playlist for /control.
    """
    t_start = time.time()
    path = "regex"
    parsed: dict[str, Any] = {}
    parse_ms = 0

    # ---- Path 1: regex / alias parser (fast) ----
    rc, data = await _verse_cli_json(payload.prompt)
    verses = (data or {}).get("results", []) if rc == 0 else []

    # ---- Path 2: AI fallback when the CLI couldn't parse ----
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

    if payload.project and verses:
        await _set_playlist(ref_label, verses)

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
