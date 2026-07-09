from __future__ import annotations

from typing import Any
import os

import requests

YOUCOM_SEARCH_URL = "https://ydc-index.io/v1/search"


def _format_youcom_search_results(payload: dict[str, Any], *, count: int) -> str:
    results = payload.get("results") or {}
    web_results = results.get("web") or []
    news_results = results.get("news") or []

    lines = []
    for label, items in (("Web", web_results), ("News", news_results)):
        if not items:
            continue
        lines.append(f"{label} results:")
        for idx, item in enumerate(items[:count], start=1):
            title = item.get("title") or "Untitled"
            url = item.get("url") or ""
            description = item.get("description") or ""
            snippets = item.get("snippets") or []
            snippet_text = snippets[0] if snippets else description
            lines.append(f"{idx}. {title}")
            if url:
                lines.append(f"   {url}")
            if snippet_text:
                lines.append(f"   {snippet_text}")
        lines.append("")

    if not lines:
        return "No results returned by You.com."
    return "\n".join(lines).rstrip()


def youcom_search(query: str, *, count: int = 5, api_key: str | None = None) -> str:
    api_key = api_key or os.getenv("YDC_API_KEY")
    if not api_key:
        return "You.com search is disabled until YDC_API_KEY is configured."

    response = requests.get(
        YOUCOM_SEARCH_URL,
        params={"query": query, "count": count},
        headers={"X-API-Key": api_key},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return _format_youcom_search_results(payload, count=count)
