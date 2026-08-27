"""SQLite disk cache (search results + extracted text) with TTL and compression.

Location: `$INFOSEEK_CACHE` if set; otherwise the platform cache dir
(`%LOCALAPPDATA%\\infoseek` on Windows, `~/.cache/infoseek` elsewhere).
If that location is not writable (sandboxed / restricted environments), the
cache transparently falls back to the system temp dir. `info()` reports the
active location and any persistent error instead of failing silently.
"""
import hashlib, os, sqlite3, tempfile, threading, time, zlib
from pathlib import Path

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_db_path: str = ""
_error: str | None = None  # last persistent failure, surfaced by info()
_Z_PREFIX = b"\x00zlib\x01"


def _default_cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "infoseek"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "infoseek"


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _cache_dir() -> Path:
    env = os.environ.get("INFOSEEK_CACHE")
    if env:
        return Path(env)
    d = _default_cache_dir()
    return d if _writable(d) else Path(tempfile.gettempdir()) / "infoseek"


def _db() -> sqlite3.Connection:
    global _conn, _db_path, _error
    if _conn is None:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        _db_path = str(cache_dir / "cache.sqlite")
        _conn = sqlite3.connect(_db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, val BLOB, ts REAL, ttl REAL)")
        _conn.commit()
        _error = None
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
    global _error
    try:
        with _lock:
            row = _db().execute("SELECT val, ts, ttl FROM kv WHERE key=?", (_key(kind, *parts),)).fetchone()
        if row and time.time() - row[1] < min(row[2], ttl):
            return _decode_val(row[0])
    except Exception as e:
        _error = f"{type(e).__name__}: {e}"
    return None


def set(kind: str, *parts, value: str = "", ttl: float = 86400) -> None:
    global _error
    try:
        blob = _encode_val(value)
        with _lock:
            _db().execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)",
                          (_key(kind, *parts), blob, time.time(), ttl))
            _db().commit()
        _error = None
    except Exception as e:
        _error = f"{type(e).__name__}: {e}"


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
        out = f"{row[0]} entries, ~{row[1] or 0} KB at {_db_path}"
        return out + (f" [warning: {_error}]" if _error else "")
    except Exception as e:
        return f"cache unavailable ({type(e).__name__}: {e})"
