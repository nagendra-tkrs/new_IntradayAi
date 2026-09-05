import logging
import asyncio
import time
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_YF_BASE = "https://query2.finance.yahoo.com"
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

SECTOR_CACHE_TTL = 86400  # 24 hours - sectors rarely change
SECTOR_FAILURE_TTL = 600  # 10 min backoff for "Unknown" so we retry without hammering


class SectorResolver:
    def __init__(self):
        self._cache: dict[str, tuple[float, str]] = {}
        self._pending_resolves: set[str] = set()

    def _ns_symbol(self, symbol: str) -> str:
        if symbol.endswith(".NS") or symbol.startswith("^"):
            return symbol
        return f"{symbol}.NS"

    def _strip_ns(self, symbol: str) -> str:
        if symbol.endswith(".NS"):
            return symbol[:-3]
        return symbol

    def _get_cached(self, symbol: str) -> Optional[str]:
        key = symbol.upper()
        if key in self._cache:
            ts, sector = self._cache[key]
            ttl = SECTOR_CACHE_TTL if sector != "Unknown" else SECTOR_FAILURE_TTL
            if time.time() - ts < ttl:
                return sector
            del self._cache[key]
        return None

    def _set_cached(self, symbol: str, sector: str):
        key = symbol.upper()
        self._cache[key] = (time.time(), sector)

    def _fetch_sector_sync(self, symbol: str) -> str:
        ns_sym = self._ns_symbol(symbol)
        from urllib.parse import quote as url_quote
        url = f"{_YF_BASE}/v10/finance/quoteSummary/{url_quote(ns_sym)}?modules=assetProfile"
        try:
            r = requests.get(url, headers=_YF_HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json()
            result = data.get("quoteSummary", {}).get("result")
            if result and len(result) > 0:
                profile = result[0].get("assetProfile", {})
                sector = profile.get("sector")
                if sector and isinstance(sector, str) and sector.strip():
                    return sector.strip()
        except Exception as e:
            logger.debug(f"Sector fetch failed for {ns_sym}: {e}")
        return "Unknown"

    async def resolve(self, symbol: str) -> str:
        cached = self._get_cached(symbol)
        if cached is not None:
            return cached
        sector = await asyncio.to_thread(self._fetch_sector_sync, symbol)
        self._set_cached(symbol, sector)
        return sector

    async def resolve_batch(self, symbols: list[str]) -> dict[str, str]:
        results: dict[str, str] = {}
        unresolved: list[str] = []
        for sym in symbols:
            cached = self._get_cached(sym)
            if cached is not None:
                results[sym] = cached
            else:
                unresolved.append(sym)

        if not unresolved:
            return results

        sem = asyncio.Semaphore(5)

        async def _resolve_one(sym: str):
            async with sem:
                sector = await self.resolve(sym)
                results[sym] = sector

        await asyncio.gather(*[_resolve_one(s) for s in unresolved])
        return results

    def invalidate(self, symbol: str):
        self._cache.pop(symbol.upper(), None)


sector_resolver = SectorResolver()
