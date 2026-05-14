"""Natural-language → RVR1960 reference / FTS query parser.

Auth, in priority order (whichever the OPERATOR running the server has set up):
  1. ANTHROPIC_API_KEY env var — direct API key from console.anthropic.com.
  2. Claude.ai OAuth token at `~/.claude/.credentials.json` — written by
     `claude login`, refreshed by the Claude Code daemon. The server only
     reads it; it never travels into the repo.

Neither path bakes a secret into source. Each user runs the server with their
own credentials.

Round-trip on Haiku: ~0.7-1.5s per query.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic

_CREDS = Path.home() / ".claude" / ".credentials.json"


def _build_client() -> Anthropic:
    """Construct an Anthropic client using whatever auth the OS has available.

    Read on every call so a token refresh (Claude Code rotates ~hourly) lands
    automatically. Negligible overhead.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return Anthropic(api_key=api_key)
    if _CREDS.exists():
        token = json.loads(_CREDS.read_text())["claudeAiOauth"]["accessToken"]
        return Anthropic(auth_token=token)
    raise RuntimeError(
        "No Anthropic credentials found. Either set ANTHROPIC_API_KEY in the "
        f"environment, or run `claude login` so {_CREDS} exists."
    )


_SYSTEM_PROMPT = """\
You translate user requests into RVR1960 Spanish Bible lookups.

ALWAYS call the `resolve_query` tool. Never answer in plain text.

Modes:
- "lookup": user named a specific book + chapter (+ optional verse[s]).
  Output `ref` as a clean Spanish reference, e.g. "Juan 3:16", "Mateo 5:3-12",
  "Salmos 23" (whole chapter), "1 Juan 4:8", "Apocalipsis 22:20".
- "search": user asked a topic/phrase, NOT a specific reference.
  Output `query` as a 2-4 word Spanish phrase suitable for FTS.
- "unclear": the query cannot reasonably be resolved.

Spanish dictation — normalize numbers and connectors:
- Spelled numbers: uno=1, dos=2, tres=3, cuatro=4, cinco=5, seis=6, siete=7,
  ocho=8, nueve=9, diez=10, once=11, doce=12, trece=13, catorce=14, quince=15,
  dieciseis=16, diecisiete=17, dieciocho=18, diecinueve=19, veinte=20,
  veintiuno=21, veintidos=22, veintitres=23, ... cincuenta=50, cien=100, etc.
- Ordinals: "primera de Juan" → "1 Juan", "segunda de Corintios" → "2 Corintios",
  "tercera de Juan" → "3 Juan".
- Connectors: "capítulo X versículo Y" → "X:Y", "del X al Y" / "del uno al diez"
  → verse range "X-Y", "al" inside a range = "-", "y" between refs = ";".

Books — always Spanish, accent-tolerant. Translate English on the fly:
  Matthew→Mateo, Mark→Marcos, Luke→Lucas, John→Juan, Acts→Hechos,
  Romans→Romanos, 1/2 Corinthians→1/2 Corintios, Galatians→Gálatas,
  Ephesians→Efesios, Philippians→Filipenses, Hebrews→Hebreos, James→Santiago,
  1/2/3 John→1/2/3 Juan, Revelation→Apocalipsis, Genesis→Génesis, Exodus→Éxodo,
  Psalms→Salmos, Proverbs→Proverbios, Isaiah→Isaías, etc.

Examples (the only output is the tool call):
- "Juan 3:16" → lookup, ref="Juan 3:16"
- "dame Juan uno del uno al diez" → lookup, ref="Juan 1:1-10"
- "primera de juan capítulo cuatro versículo ocho" → lookup, ref="1 Juan 4:8"
- "salmos veintitres" → lookup, ref="Salmos 23"
- "Apocalipsis veintidos veinte" → lookup, ref="Apocalipsis 22:20"
- "Mateo cinco del tres al doce" → lookup, ref="Mateo 5:3-12"
- "Romans 8:28 in Spanish" → lookup, ref="Romanos 8:28"
- "show me Mateo 5:3 to 12" → lookup, ref="Mateo 5:3-12"
- "the verse about fe sin obras" → search, query="fe sin obras"
- "what does the Bible say about being a good shepherd" → search, query="buen pastor"
- "versículos sobre el amor" → search, query="amor"
- "verse from chapter five of matthew about peacemakers" → lookup, ref="Mateo 5:9"
"""


_TOOL = {
    "name": "resolve_query",
    "description": "Resolve the user's request to either a Spanish reference (mode=lookup) or an FTS phrase (mode=search).",
    "input_schema": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["lookup", "search", "unclear"]},
            "ref": {
                "type": "string",
                "description": "Spanish reference like 'Juan 3:16' or 'Mateo 5:3-12'. Required when mode=lookup.",
            },
            "query": {
                "type": "string",
                "description": "Short Spanish FTS phrase. Required when mode=search.",
            },
            "note": {
                "type": "string",
                "description": "One-line explanation. Only set for mode=unclear.",
            },
        },
        "required": ["mode"],
    },
}


def parse_query(prompt: str, model: str = "claude-haiku-4-5") -> tuple[dict[str, Any], float]:
    """Return ({mode, ref?, query?, note?}, elapsed_seconds)."""
    t0 = time.time()
    client = _build_client()
    resp = client.messages.create(
        model=model,
        max_tokens=256,
        system=[
            {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "resolve_query"},
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.time() - t0
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            data = dict(block.input)  # tool input is already a dict
            data["_usage"] = {
                "input": resp.usage.input_tokens,
                "output": resp.usage.output_tokens,
                "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            }
            return data, elapsed
    return {"mode": "unclear", "note": "model did not call the tool"}, elapsed
