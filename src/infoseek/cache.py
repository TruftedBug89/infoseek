"""SQLite disk cache (search results + extracted text) with TTL and compression."""
import hashlib, json, os, sqlite3, threading, time, zlib
from pathlib import Path

_lock = threading.Lock()
_conn = None
_Z_PREFIX = b"\x00zlib\x01"


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        cache_dir = os.environ.get("INFOSEEK_CACHE") or str(Path.home() / ".cache" / "infoseek")
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(Path(cache_dir) / "cache.sqlite"), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, val BLOB, ts REAL, ttl REAL)")
        _conn.commit()
    return _conn


def _key(*parts) -> str:
    return hashlib.sha1("\x00".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _decode_val(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        if raw.startswith(_Z_PREFIX):
            return zlib.decompress(raw[len(_Z_PREFIX):]).decode("utf-8", "replace")
        return raw.decode("utf-8", "replace")
    return str(raw)


def _encode_val(val: str) -> bytes:
    data = val.encode("utf-8")
    if len(data) > 120:
        return _Z_PREFIX + zlib.compress(data, level=6)
    return data


def get(kind: str, *parts, ttl: float) -> str | None:
    try:
        with _lock:
            row = _db().execute("SELECT val, ts, ttl FROM kv WHERE key=?", (_key(kind, *parts),)).fetchone()
        if row and time.time() - row[1] < min(row[2], ttl):
            return _decode_val(row[0])
    except Exception:
        pass
    return None


def set(kind: str, *parts, value: str = "", ttl: float = 86400, **kw) -> None:
    try:
        val = kw.get("value", value)
        t = kw.get("ttl", ttl)
        blob = _encode_val(val)
        with _lock:
            _db().execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)",
                          (_key(kind, *parts), blob, time.time(), t))
            _db().commit()
    except Exception:
        pass


def prune() -> int:
    """Remove expired entries from the cache."""
    try:
        now = time.time()
        with _lock:
            cur = _db().execute("DELETE FROM kv WHERE (? - ts) > ttl", (now,))
            _db().commit()
            return cur.rowcount
    except Exception:
        return 0


def info() -> str:
    try:
        with _lock:
            row = _db().execute("SELECT COUNT(*), ROUND(COALESCE(SUM(LENGTH(val)), 0)/1024) FROM kv").fetchone()
        return f"{row[0]} entries, ~{row[1] or 0} KB (WAL compressed)"
    except Exception as e:
        return f"cache unavailable ({e})"
