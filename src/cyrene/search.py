"""
Deep Search Pipeline -- Multi-stage search with query generation, parallel fetching,
filtering, and synthesis.

Architecture:
  Query Generator (LLM) --> SimpleXNG Searcher --> Filter (LLM) --> Synthesizer (LLM)
"""

import asyncio
import logging
import re
from typing import Any

import requests

from cyrene.call_llm import call_llm as _unified_call_llm
from cyrene.config import SEARCH_PROXY

logger = logging.getLogger(__name__)


def _proxied_session() -> requests.Session:
    """创建 requests Session，如果配置了代理则使用代理。"""
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    if SEARCH_PROXY:
        s.proxies = {"http": SEARCH_PROXY, "https": SEARCH_PROXY}
    return s

_HTTP_TIMEOUT = 30.0
_MAX_CONCURRENT = 20

# ---------------------------------------------------------------------------
# LLM call (same pattern as agent.py, text-only, no tools)
# ---------------------------------------------------------------------------


async def _call_llm(messages: list[dict]) -> str:
    """Call the LLM and return the response text content."""
    result = await _unified_call_llm(
        messages,
        return_text=True,
        caller="search",
        phase="no_tools",
    )
    return (result or "").strip()


# ---------------------------------------------------------------------------
# Stage 1: Query generation
# ---------------------------------------------------------------------------


async def _generate_queries(topic: str) -> list[str]:
    """Generate 3-5 search queries covering different angles of the topic."""
    system_msg = (
        "You are a search query generator. Given a user question, generate 3-5 specific search queries. "
        "Use different wordings to maximize coverage. One query per line. No numbering."
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": topic},
    ]

    try:
        text = await _call_llm(messages)
    except Exception as exc:
        logger.warning("Query generation LLM call failed: %s", exc)
        return []

    # Parse: one query per non-empty line
    queries = [line.strip().strip('"').strip("'") for line in text.splitlines() if line.strip()]
    # Filter out obviously non-query lines
    queries = [q for q in queries if len(q) > 3 and not q.lower().startswith(("here", "sure", "okay", "note:"))]

    if not queries:
        logger.warning("Query generation returned empty output, falling back")
        return []

    # Cap at 5
    return queries[:5]


def _get_simplexng_url() -> str:
    """Resolve the app-managed SimpleXNG search API URL."""
    from cyrene.searxng_manager import get_manager
    manager = get_manager()
    if manager.is_running:
        return manager.url
    return ""


async def _search_simplexng(query: str) -> list[dict]:
    """Search via the built-in SimpleXNG SearXNG-compatible API."""
    base_url = _get_simplexng_url()
    if not base_url:
        return []
    url = f"{base_url.rstrip('/')}/search"
    headers = {"Accept": "application/json"}

    def _fetch() -> list[dict]:
        # SimpleXNG 是本地服务，必须忽略系统代理环境变量。
        sess = requests.Session()
        sess.trust_env = False
        r = sess.get(url, params={"q": query, "format": "json", "language": "zh-CN", "safesearch": "0"}, headers=headers, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("results", [])

    loop = asyncio.get_event_loop()
    try:
        raw_results = await loop.run_in_executor(None, _fetch)
    except Exception as exc:
        logger.warning("SimpleXNG search failed: %s", exc)
        return []

    results = []
    for r in raw_results:
        title = r.get("title", "").strip()
        url_val = r.get("url", "")
        content = r.get("content", "").strip()
        if title and url_val:
            results.append({"title": title, "url": url_val, "snippet": content, "query": query})
        if len(results) >= 5:
            break

    return results

def _strip_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    # Remove script/style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    # Remove all tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&#x27;", "'")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _fetch_url(url: str) -> str:
    """Fetch a URL and return its plain text content, truncated to 3000 chars."""

    def _fetch() -> str:
        sess = _proxied_session()
        r = sess.get(url, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.text

    loop = asyncio.get_event_loop()
    try:
        html = await loop.run_in_executor(None, _fetch)
    except Exception as exc:
        logger.debug("Failed to fetch URL %r: %s", url, exc)
        return ""

    text = _strip_html(html)
    return text[:3000]


# ---------------------------------------------------------------------------
# Stage 3: Result filtering
# ---------------------------------------------------------------------------


async def _filter_results(raw_results: list[dict], topic: str) -> list[dict]:
    """Filter search results by relevance, keeping only those marked RELEVANT."""
    if not raw_results:
        return []

    # Build the prompt with numbered results
    lines: list[str] = []
    for i, r in enumerate(raw_results, start=1):
        snippet = r.get("snippet", "")[:200]
        lines.append(f"{i}. [{r.get('title', '?')}]({r.get('url', '')})\n   snippet: {snippet}")

    system_msg = (
        "You are a search result filter. Given a topic and search results, "
        "classify each as RELEVANT or IRRELEVANT. "
        "Keep results that are relevant to the topic, including background or partial matches. Discard only clearly unrelated results.\n\n"
        f"Topic: {topic}\n\n"
        "Results:\n"
        f"{chr(10).join(lines)}\n\n"
        'Output format:\n'
        'KEEP: 1, 3, 5\n'
        'DISCARD: 2, 4'
    )

    # DEBUG: 打印传给 filter 的原始结果
    logger.warning("=== Stage 3 filter input (topic=%s) ===", topic[:40])
    for i, r in enumerate(raw_results[:8]):
        logger.warning("  [%d] %s | snippet: %s", i+1, r.get("title", "?")[:40], r.get("snippet", "")[:60])
    logger.warning("=== end filter input ===")

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": "Filter the results above."},
    ]

    try:
        text = await _call_llm(messages)
    except Exception as exc:
        logger.warning("Filter LLM call failed: %s", exc)
        return []  # caller will fallback

    # Parse "KEEP: 1, 3, 5" line
    keep_indices: set[int] = set()
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("keep:"):
            parts = line.split(":", 1)[1]
            for token in parts.split(","):
                token = token.strip()
                try:
                    idx = int(token) - 1  # convert to 0-based
                    if 0 <= idx < len(raw_results):
                        keep_indices.add(idx)
                except ValueError:
                    continue

    if not keep_indices:
        logger.warning("Filter returned no KEEP indices, raw: %r", text)
        return []

    filtered = [raw_results[i] for i in sorted(keep_indices)]
    logger.info("Filter kept %d/%d results", len(filtered), len(raw_results))
    return filtered


# ---------------------------------------------------------------------------
# Stage 4: Synthesis
# ---------------------------------------------------------------------------


async def _synthesize(relevant_results: list[dict], fetched_contents: list[str], topic: str) -> str:
    """Synthesize filtered results into a structured answer."""
    if not relevant_results:
        return ""

    lines: list[str] = []
    for i, r in enumerate(relevant_results):
        content = (fetched_contents[i] if i < len(fetched_contents) else "") or r.get("snippet", "")
        lines.append(
            f"{i + 1}. {r.get('title', '?')} ({r.get('url', '')})\n"
            f"   {content[:500]}"
        )

    system_msg = (
        "You are a research synthesizer. Combine the following search results "
        "into a clear, factual answer. Cite sources when possible. "
        "If sources disagree, note the disagreement.\n\n"
        f"Topic: {topic}\n\n"
        "Search results:\n"
        f"{chr(10).join(lines)}\n\n"
        "Answer:"
    )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": f"Provide a comprehensive answer about: {topic}"},
    ]

    try:
        answer = await _call_llm(messages)
    except Exception as exc:
        logger.warning("Synthesis LLM call failed: %s", exc)
        # Fallback: build a simple text summary from results
        return _fallback_synthesis(relevant_results, fetched_contents)

    return answer or _fallback_synthesis(relevant_results, fetched_contents)


def _fallback_synthesis(relevant_results: list[dict], fetched_contents: list[str]) -> str:
    """Build a simple text summary when the LLM synthesis fails."""
    parts: list[str] = [f"Search results for your question:\n"]
    for i, r in enumerate(relevant_results):
        title = r.get("title", "?")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        content = (fetched_contents[i][:500] if i < len(fetched_contents) and fetched_contents[i] else "")
        detail = content or snippet
        parts.append(f"Source {i + 1}: {title}")
        parts.append(f"URL: {url}")
        if detail:
            parts.append(f"Summary: {detail}")
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry: deep_search
# ---------------------------------------------------------------------------


async def deep_search(topic: str) -> str:
    """Multi-stage deep search pipeline.

    Stages:
        1. Query selection: use the original user topic
        2. SimpleXNG search + fetch URL contents
        3. Filter (LLM): keep only relevant results
        4. Synthesize (LLM): produce structured answer

    Search intentionally goes through the built-in SimpleXNG backend only.
    """
    logger.info("Deep search starting for: %s", topic)

    # -----------------------------------------------------------------------
    # Stage 1: Single query only — 不生成多轮搜索，避免触发限流
    # -----------------------------------------------------------------------
    queries = [topic]
    logger.info("Stage 1: single query only")

    # -----------------------------------------------------------------------
    # Stage 2: Parallel search and fetch
    # -----------------------------------------------------------------------
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _limited_search(q: str) -> list[dict]:
        async with semaphore:
            return await _search_simplexng(q)

    search_tasks = [_limited_search(q) for q in queries]

    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    all_results: list[dict] = []
    for sr in search_results:
        if isinstance(sr, list):
            all_results.extend(sr)

    logger.info("Stage 2 search complete: %d raw results (SimpleXNG)", len(all_results))

    if not all_results:
        return f"Search returned no results for: {topic}"

    # Deduplicate by URL (keep first occurrence)
    seen_urls: set[str] = set()
    deduped: list[dict] = []
    for r in all_results:
        u = r.get("url", "")
        if u and u not in seen_urls:
            seen_urls.add(u)
            deduped.append(r)
        elif not u:
            deduped.append(r)

    # Cap at 15 results
    deduped = deduped[:15]

    # Fetch content for top 8 results in parallel
    async def _limited_fetch(r: dict) -> str:
        url = r.get("url", "")
        if not url:
            return ""
        async with semaphore:
            return await _fetch_url(url)

    fetch_tasks = [_limited_fetch(r) for r in deduped[:8]]
    fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    # Attach fetched content back to results
    for i, r in enumerate(deduped[:8]):
        if i < len(fetched) and isinstance(fetched[i], str):
            r["fetched_content"] = fetched[i]
        else:
            r["fetched_content"] = ""

    logger.info("Stage 2 fetch complete: %d URLs fetched", sum(1 for f in fetched if isinstance(f, str) and f))

    # DEBUG: 打印原始搜索结果标题
    if deduped:
        logger.warning("=== Stage 2 raw results (%d) ===", len(deduped))
        for i, r in enumerate(deduped[:10]):
            logger.warning("  [%d] %s | %s", i+1, r.get("title", "?")[:50], r.get("url", "")[:60])
        logger.warning("=== end raw results ===")

    # -----------------------------------------------------------------------
    # Stage 3: Filter
    # -----------------------------------------------------------------------
    filtered = await _filter_results(deduped, topic)
    if not filtered:
        logger.warning("Stage 3 filter returned empty, falling back to top 5 results")
        filtered = deduped[:5]
    logger.info("Stage 3 complete: %d relevant results", len(filtered))

    # -----------------------------------------------------------------------
    # Stage 4: Synthesize
    # -----------------------------------------------------------------------
    fetched_contents = [r.get("fetched_content", "") or r.get("snippet", "") for r in filtered]
    answer = await _synthesize(filtered, fetched_contents, topic)
    logger.info("Stage 4 complete: synthesis generated (%d chars)", len(answer))

    return answer
