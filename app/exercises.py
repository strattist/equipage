"""Exercise catalog (French display names, aliases, explanations, links) and
`linkify`, which turns known exercise names in a reply into Discord masked links."""

import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from app import db

SEED_PATH = Path(__file__).with_name("exercises_seed.json")

# Regions linkify must leave alone: fenced code, inline code, existing masked links, bare URLs.
_PROTECTED = re.compile(
    r"```.*?```|`[^`\n]*`|\[[^\]\n]*\]\([^)\s]*\)|<?https?://[^\s<>]+>?", re.DOTALL
)


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _split_aliases(aliases: str) -> list[str]:
    return [a.strip() for a in aliases.split(",") if a.strip()]


def seed() -> None:
    """Insert catalog rows whose key is missing; never touch existing rows."""
    rows = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO exercises (key, name, aliases, explanation, url) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    r["key"],
                    r["name"],
                    ",".join(r["aliases"]),
                    r["explanation"],
                    r["url"],
                )
                for r in rows
            ],
        )


def _rows() -> list[dict]:
    with db.connect_ro() as conn:
        rows = conn.execute(
            "SELECT key, name, aliases, explanation, url FROM exercises ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def _public(row: dict) -> dict:
    return {k: row[k] for k in ("name", "aliases", "explanation", "url")}


def list_all() -> list[dict]:
    return [_public(r) for r in _rows()]


def _find(name: str) -> dict | None:
    wanted = _norm(name)
    for row in _rows():
        if wanted in {_norm(n) for n in [row["name"], *_split_aliases(row["aliases"])]}:
            return row
    return None


def get(name: str) -> dict | None:
    """Case- and accent-insensitive lookup on name or alias."""
    row = _find(name)
    return _public(row) if row else None


def _valid_url(url: str) -> bool:
    if not url or any(c.isspace() for c in url):
        return False
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def set_url(name: str, url: str) -> bool:
    row = _find(name)
    if row is None or not _valid_url(url):
        return False
    with db.connect() as conn:
        conn.execute("UPDATE exercises SET url = ? WHERE key = ?", (url, row["key"]))
    return True


def _pattern(rows: list[dict]) -> tuple[re.Pattern, dict[str, str]] | None:
    urls: dict[str, str] = {}
    for row in rows:
        if not row["url"]:
            continue
        for term in [row["name"], *_split_aliases(row["aliases"])]:
            urls.setdefault(term.casefold(), row["url"])
    if not urls:
        return None
    terms = sorted(urls, key=len, reverse=True)
    alternation = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"(?<!\w)(?:{alternation})(?![\w-])", re.IGNORECASE), urls


def _masked(text: str, url: str) -> str:
    return f"[{text}]({url.replace('(', '%28').replace(')', '%29')})"


def linkify(text: str) -> str:
    compiled = _pattern(_rows())
    if compiled is None:
        return text
    pattern, urls = compiled

    def link_plain(segment: str) -> str:
        return pattern.sub(
            lambda m: _masked(m.group(0), urls[m.group(0).casefold()]), segment
        )

    out, pos = [], 0
    for m in _PROTECTED.finditer(text):
        out.append(link_plain(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(link_plain(text[pos:]))
    return "".join(out)
