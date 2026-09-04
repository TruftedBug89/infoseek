"""Polite HTTP layer: shared client, per-host rate limiting, robots.txt respect."""
import asyncio, time, random
from urllib.parse import urlparse
import httpx
from urllib.robotparser import RobotFileParser

UAS = [
 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
 "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

class PoliteClient:
 """Async HTTP client with per-host min-interval throttling, retries, and optional robots.txt checks."""

 def __init__(self, timeout=12.0, min_interval=1.0, respect_robots=True, ua=None,
 retries=1, transport=None):
 self.timeout = timeout
 self.min_interval = min_interval
 self.respect_robots = respect_robots
 self.ua = ua or random.choice(UAS)
 self.retries = retries
 self._next_at: dict[str, float] = {}
 self._lock = asyncio.Lock()
 self._robots: dict[str, tuple] = {}
 self._robots_lock: dict[str, asyncio.Lock] = {}
 self._client = httpx.AsyncClient(
 follow_redirects=True, timeout=timeout,
 headers={
 "User-Agent": self.ua,
 "Accept-Language": "en-US,en;q=0.9",
 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
 "Accept-Encoding": "gzip, deflate",
 },
 limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
 **({"transport": transport} if transport else {}),
 )

 async def close(self):
 await self._client.aclose()

 async def _throttle(self, host: str):
 async with self._lock:
 now = time.monotonic()
 nxt = self._next_at.get(host, 0.0)
 if now < nxt:
 delay = nxt - now
 self._next_at[host] = nxt + self.min_interval
 else:
 delay = 0.0
 self._next_at[host] = now + self.min_interval
 if delay:
 await asyncio.sleep(delay)

 async def request(self, method: str, url: str, **kw) -> httpx.Response:
 retries = kw.pop("retries", self.retries)
 timeout = kw.pop("timeout", None)
 host = urlparse(url).netloc
 await self._throttle(host)
 last_err: Exception | None = None
 for attempt in range(retries + 1):
 try:
 r = await self._client.request(method, url, **(dict(timeout=timeout) if timeout else {}), **kw)
 retryable = r.status_code in (429, 503) or (r.status_code == 502 and "Retry-After" in r.headers)
 if retryable and attempt < retries:
 ra = r.headers.get("Retry-After")
 try:
 wait = min(max(float(ra), 1.0), 8.0) if ra else 2.0
 except ValueError:
 wait = 2.0
 await asyncio.sleep(wait)
 continue
 return r
 except httpx.HTTPError as e:
 last_err = e
 if attempt < retries:
 await asyncio.sleep(1.0 * (attempt + 1))
 raise last_err or httpx.TransportError("request failed")

 async def get(self, url: str, **kw) -> httpx.Response:
 return await self.request("GET", url, **kw)

 async def post(self, url: str, **kw) -> httpx.Response:
 return await self.request("POST", url, **kw)

 async def _fetch_robots(self, host: str):
 """Fetch + parse robots.txt. None means 'no file / unreachable -> allow'."""
 try:
 r = await self._client.get(f"https://{host}/robots.txt", timeout=3.0)
 if r.status_code == 200:
 rp = RobotFileParser()
 rp.parse(r.text.splitlines())
 return rp
 except Exception:
 pass
 return None

 async def allowed(self, url: str) -> bool:
 """robots.txt check for direct page fetches (not search-engine endpoints).

 Results are cached per host for 1h (including 'no robots.txt'), and the
 fetch runs under a per-host lock - never under the global throttle lock - so concurrent extractions are not serialized behind it."""
 if not self.respect_robots:
 return True
 host = urlparse(url).netloc
 if not host:
 return True
 entry = self._robots.get(host)
 if entry is None or time.time() - entry[1] > 3600:
 lock = self._robots_lock.setdefault(host, asyncio.Lock())
 async with lock:
 entry = self._robots.get(host)
 if entry is None or time.time() - entry[1] > 3600:
 entry = (await self._fetch_robots(host), time.time())
 self._robots[host] = entry
 rp = entry[0]
 return rp is None or rp.can_fetch(self.ua, url)
