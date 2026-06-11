"""Thin async wrapper around the Tavily Search API.

Tavily is designed for LLM agent pipelines — it returns structured
{url, title, content, score} results with clean snippets. No feed
maintenance, no blocked iCal URLs, no date-ordering quirks.

Pricing: ~1 credit per basic search, ~$0.001 per credit.
At 5 domains × 3 cities = 15 searches/sweep → ~$0.015/week.

Usage:
    searcher = TavilySearcher(api_key=os.getenv("TAVILY_API_KEY"))
    results = await searcher.search("energy storage events Boston June 2026")
    # returns: list of {url, title, content, score}
    await searcher.close()
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("lib.tavily")

_BASE_URL = "https://api.tavily.com/search"


class TavilySearcher:
    """Async Tavily search client.

    Parameters
    ----------
    api_key:
        Tavily API key (``TAVILY_API_KEY`` env var).
    timeout:
        Per-request timeout in seconds.
    max_results:
        Default number of results per search (overridable per call).
    search_depth:
        ``"basic"`` (1 credit, fast) or ``"advanced"`` (2 credits, deeper).
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 30,
        max_results: int = 10,
        search_depth: str = "basic",
        include_raw_content: bool = True,
    ):
        self.api_key = api_key
        self.max_results = max_results
        self.search_depth = search_depth
        # Pull cleaned full-page text (not just the snippet) — event dates
        # usually live in the page body, not the short search snippet.
        self.include_raw_content = include_raw_content
        self._client = httpx.AsyncClient(timeout=timeout)

    async def search(self, query: str, max_results: int | None = None) -> list[dict]:
        """Run a search and return a list of result dicts.

        Each result contains at minimum: ``url``, ``title``, ``content``
        (snippet), ``score``. Returns an empty list on any API error so the
        caller can continue gracefully.
        """
        n = max_results if max_results is not None else self.max_results
        try:
            resp = await self._client.post(
                _BASE_URL,
                json={
                    "api_key": self.api_key,
                    "query": query,
                    "search_depth": self.search_depth,
                    "max_results": n,
                    "include_answer": False,
                    "include_images": False,
                    "include_raw_content": self.include_raw_content,
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            log.debug("Tavily '%s' → %d results", query[:70], len(results))
            return results
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Tavily HTTP %s for query '%s'",
                exc.response.status_code, query[:70],
            )
            return []
        except Exception as exc:  # noqa: BLE001
            log.warning("Tavily search error (%s): %s", type(exc).__name__, query[:70])
            return []

    async def close(self) -> None:
        await self._client.aclose()
