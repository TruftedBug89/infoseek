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

    def __init__(self, timeout=12.0, min_interval=1.0, respect_robots=True, ua=None, retries=1):
        self.timeout = timeout
        self.min_interval = min_interval
        self.respect_robots = respect_robots
        self.ua = ua or random.choice(UAS)
        self.retries = retries
        self._next_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._robots: dict[str, tuple] = {}
        self._client = httpx.AsyncClient(
            follow_redirects=True, timeout=timeout,
            headers={
                "User-Agent": self.ua,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            },
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
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
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last_err or httpx.TransportError("request failed")

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def allowed(self, url: str) -> bool:
        """robots.txt check for direct page fetches (not search-engine endpoints)."""
        if not self.respect_robots:
            return True
        host = urlparse(url).netloc
        async with self._lock:
            rp, fetched = self._robots.get(host, (None, 0.0))
            if rp is None or time.time() - fetched > 3600:
                rp = RobotFileParser()
                rp.set_url(f"https://{host}/robots.txt")
                try:
                    await asyncio.to_thread(rp.read)
                except Exception:
                    rp = None
                self._robots[host] = (rp, time.time())
        return rp is None or rp.can_fetch(self.ua, url)
