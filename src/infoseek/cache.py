"""SQLite disk cache (search results + extracted text) with TTL."""
import hashlib, json, os, sqlite3, threading, time
from pathlib import Path

_lock = threading.Lock()
_conn = None

def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        cache_dir = os.environ.get("INFOSEEK_CACHE") or str(Path.home() / ".cache" / "infoseek")
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(Path(cache_dir) / "cache.sqlite"), check_same_thread=False)
        _conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, val TEXT, ts REAL, ttl REAL)")
        _conn.commit()
    return _conn

def _key(*parts) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

def get(kind: str, *parts, ttl: float) -> str | None:
    try:
        with _lock:
            row = _db().execute("SELECT val, ts, ttl FROM kv WHERE key=?", (_key(kind, *parts),)).fetchone()
        if row and time.time() - row[1] < min(row[2], ttl):
            return row[0]
    except Exception:
        pass
    return None

def set(kind: str, *parts, value: str, ttl: float):
    try:
        with _lock:
            _db().execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)",
                          (_key(kind, *parts), value, time.time(), ttl))
            _db().commit()
    except Exception:
        pass

def info() -> str:
    try:
        with _lock:
            row = _db().execute("SELECT COUNT(*), ROUND(SUM(LENGTH(val))/1024) FROM kv").fetchone()
        return f"{row[0]} entries, ~{row[1]} KB"
    except Exception as e:
        return f"cache unavailable ({e})"
