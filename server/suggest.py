"""Listener → suggestion pipeline.

Short Spanish utterances arrive from the in-process listener (or the
external HTTP /suggest entry point). We decide whether each utterance
looks like a Bible reference, and only when it does we run the resolver.
Operator confirms via /control.

Cost gating: Haiku is only invoked when a cheap local heuristic trips
(digit, known book token, or scripture cue word). A per-minute ceiling
caps spend even if the heuristic over-fires.

Cue words are operator-tunable from /control. Defaults ship below; runtime
overrides are persisted to cue-words.json at the repo root.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Deque, Iterable, List


MIN_UTTERANCE_LEN = 3
DEDUP_SECONDS = 30
PENDING_TTL = 60
HAIKU_GATE_MAX_PER_MIN = 6


_BOOK_TOKENS = {
    "genesis", "exodo", "levitico", "numeros", "deuteronomio",
    "josue", "jueces", "rut", "samuel", "reyes", "cronicas",
    "esdras", "nehemias", "ester", "job", "salmos", "salmo",
    "proverbios", "eclesiastes", "cantares",
    "isaias", "jeremias", "lamentaciones", "ezequiel", "daniel",
    "oseas", "joel", "amos", "abdias", "jonas", "miqueas", "nahum",
    "habacuc", "sofonias", "hageo", "zacarias", "malaquias",
    "mateo", "marcos", "lucas", "juan", "hechos",
    "romanos", "corintios", "galatas", "efesios", "filipenses",
    "colosenses", "tesalonicenses", "timoteo", "tito", "filemon",
    "hebreos", "santiago", "pedro", "judas", "apocalipsis",
}

DEFAULT_CUE_WORDS = frozenset({
    "abramos", "abran", "abre", "leamos", "lean", "lee",
    "veamos", "vean", "vamos", "segun", "dice", "leyendo",
    "texto", "escritura", "versiculo", "versiculos", "capitulo",
})

# Runtime-mutable. Overridden via set_cue_words() and reloaded from disk
# on server startup.
_cue_words: set = set(DEFAULT_CUE_WORDS)

_DIGIT_RE = re.compile(r"\d")
_TOKEN_RE = re.compile(r"[a-záéíóúüñ]+", re.IGNORECASE)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _normalize_word(w: str) -> str:
    return _strip_accents(w.strip().lower())


def get_cue_words() -> List[str]:
    """Return cue words sorted for stable UI rendering."""
    return sorted(_cue_words)


def set_cue_words(words: Iterable[str]) -> List[str]:
    """Replace the runtime cue-word set. Empty/blank items are dropped.
    Returns the normalized list actually stored."""
    global _cue_words
    cleaned = {_normalize_word(w) for w in words if w and w.strip()}
    _cue_words = cleaned
    return sorted(cleaned)


def load_cue_words(path: Path) -> None:
    """Load cue words from JSON file if present. Silent on failure."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
        words = data.get("words", [])
        if isinstance(words, list):
            set_cue_words(words)
    except Exception:
        pass


def save_cue_words(path: Path) -> None:
    """Persist the current cue-word set to JSON. Best-effort."""
    try:
        path.write_text(json.dumps({"words": get_cue_words()}, indent=2) + "\n")
    except Exception:
        pass


def looks_versey(utterance: str) -> bool:
    """Cheap local check: should we bother running the resolver?"""
    if _DIGIT_RE.search(utterance):
        return True
    norm = _strip_accents(utterance.lower())
    for tok in _TOKEN_RE.findall(norm):
        if tok in _BOOK_TOKENS or tok in _cue_words:
            return True
    return False


class RateGate:
    """Hard ceiling on calls per rolling 60s window."""

    def __init__(self, max_per_min: int = HAIKU_GATE_MAX_PER_MIN) -> None:
        self._max = max_per_min
        self._hits: Deque[float] = deque()

    def allow(self) -> bool:
        now = time.time()
        cutoff = now - 60
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= self._max:
            return False
        self._hits.append(now)
        return True


class RecentRefs:
    """Tracks recently-suggested refs so we don't spam the operator."""

    def __init__(self, window_s: float = DEDUP_SECONDS) -> None:
        self._window = window_s
        self._hits: Deque[tuple] = deque()

    def seen(self, ref: str) -> bool:
        now = time.time()
        cutoff = now - self._window
        while self._hits and self._hits[0][1] < cutoff:
            self._hits.popleft()
        return any(r == ref for r, _ in self._hits)

    def record(self, ref: str) -> None:
        self._hits.append((ref, time.time()))
