#!/usr/bin/env python3
"""Build bible-rvr1960.db (SQLite) from the running Postgres dbrv1960.

Prereqs (only needed if rebuilding from scratch — the shipped .db file is
already canonical for runtime):

  1. Postgres installed and running locally.
  2. Database loaded from data/dbrv1960.backup:
       sudo -u postgres createdb dbrv1960
       sudo -u postgres pg_restore -d dbrv1960 data/dbrv1960.backup

Then run this script. It will recreate bible-rvr1960.db at the project root.
"""
import re
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "bible-rvr1960.db"
PG_DB = "dbrv1960"


def psql(query: str) -> list[list[str]]:
    out = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", PG_DB, "-tAF\t", "-c", query],
        capture_output=True, text=True, check=True, cwd="/tmp",
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def normalize_alias(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace(".", "")
    return s


_RTF_TOKEN = re.compile(r"\\\\[a-zA-Z]+\d*")


def clean_verse(text: str) -> str:
    # Stored bytes use literal '\\par', '\\i', '\\cf6' etc. (two backslashes)
    # inside RTF group braces '{...}' which may wrap the whole verse or just
    # spans within it (e.g. Jesus's words via \\cf6).
    t = text.strip()
    t = re.sub(r"\\\\par\b", "\n", t)
    t = _RTF_TOKEN.sub("", t)
    t = t.replace("{", "").replace("}", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# Spelled-out ordinal prefixes commonly used in voice/Spanish.
ORDINAL_PREFIXES = {
    "1": ["1", "primera", "primer", "primero", "i"],
    "2": ["2", "segunda", "segundo", "ii"],
    "3": ["3", "tercera", "tercero", "iii"],
}


def expand_aliases(book_name: str, raw_aliases: list[str]) -> set[str]:
    """Return a set of normalized aliases including ordinal variants."""
    aliases: set[str] = set()
    candidates = [book_name, *raw_aliases]
    for a in candidates:
        n = normalize_alias(a)
        if not n:
            continue
        aliases.add(n)
        # Expand ordinal: "1 juan" -> also "primera juan", "primer juan", "primero juan", "i juan", "1juan", "primera de juan"
        m = re.match(r"^(\d)\s+(.+)$", n)
        if m:
            num, rest = m.group(1), m.group(2)
            for prefix in ORDINAL_PREFIXES.get(num, []):
                aliases.add(f"{prefix} {rest}")
                aliases.add(f"{prefix} de {rest}")
                aliases.add(f"{prefix}{rest}")
    return aliases


def main() -> int:
    if DB_PATH.exists():
        DB_PATH.unlink()

    print(f"Building {DB_PATH} ...", file=sys.stderr)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            testament TEXT NOT NULL CHECK (testament IN ('A','N'))
        );
        CREATE TABLE book_aliases (
            alias TEXT PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES books(id)
        );
        CREATE TABLE verses (
            id INTEGER PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES books(id),
            chapter INTEGER NOT NULL,
            verse INTEGER NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(book_id, chapter, verse)
        );
        CREATE INDEX idx_verses_loc ON verses(book_id, chapter, verse);
        CREATE VIRTUAL TABLE verses_fts USING fts5(
            text,
            content='verses',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER verses_ai AFTER INSERT ON verses BEGIN
            INSERT INTO verses_fts(rowid, text) VALUES (new.id, new.text);
        END;
    """)

    # books
    rows = psql("SELECT lib_id, lib_nom, lib_tes FROM libros ORDER BY lib_id;")
    c.executemany("INSERT INTO books(id,name,testament) VALUES (?,?,?)",
                  [(int(r[0]), r[1], r[2]) for r in rows])
    book_names = {int(r[0]): r[1] for r in rows}
    print(f"  books: {len(rows)}", file=sys.stderr)

    # aliases (from libros_referencias + book names + ordinal expansions)
    ref_rows = psql("SELECT lib_id, lir_txt FROM libros_referencias ORDER BY lib_id, lir_id;")
    raw_aliases: dict[int, list[str]] = {}
    for r in ref_rows:
        raw_aliases.setdefault(int(r[0]), []).append(r[1])

    alias_inserts: dict[str, int] = {}
    for book_id, name in book_names.items():
        for a in expand_aliases(name, raw_aliases.get(book_id, [])):
            # First writer wins (canonical book takes precedence over collisions)
            alias_inserts.setdefault(a, book_id)
    c.executemany("INSERT INTO book_aliases(alias,book_id) VALUES (?,?)",
                  list(alias_inserts.items()))
    print(f"  aliases: {len(alias_inserts)}", file=sys.stderr)

    # verses
    vrows = psql("SELECT lib_id, ver_cap, ver_numver, ver_txt FROM versiculos ORDER BY lib_id, ver_cap, ver_numver;")
    verse_rows = [
        (int(r[0]), int(r[1]), int(r[2]), clean_verse(r[3]))
        for r in vrows
    ]
    c.executemany(
        "INSERT INTO verses(book_id,chapter,verse,text) VALUES (?,?,?,?)",
        verse_rows,
    )
    print(f"  verses: {len(verse_rows)}", file=sys.stderr)

    conn.commit()
    c.execute("INSERT INTO verses_fts(verses_fts) VALUES('optimize');")
    conn.commit()
    conn.close()

    print(f"Done. {DB_PATH} ({DB_PATH.stat().st_size // 1024} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
