"""
Deep Search Pipeline -- Multi-stage search with query generation, parallel fetching,
filtering, and synthesis.

Architecture:
  Query Generator (LLM) --> Parallel Searcher (asyncio) --> Filter (LLM) --> Synthesizer (LLM)
"""

import asyncio
import base64
import json
import logging
import re
from urllib.parse import parse_qs, quote, urlparse

import httpx

from cyrene.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_CONCURRENT = 20

# ---------------------------------------------------------------------------
# LLM call (same pattern as agent.py, text-only, no tools)
# ---------------------------------------------------------------------------


async def _call_llm(messages: list[dict]) -> str:
    """Call the LLM and return the response text content.

    Uses httpx.AsyncHTTPTransport(retries=1) to avoid HTTP/2 issues.
    """
    payload: dict = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }

    headers = {"Content-Type": "application/json"}
    if OPENAI_API_KEY and OPENAI_API_KEY.lower() not in ("lmstudio", "dummy", ""):
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]

    content = message.get("content") or ""
    return content.strip()


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


# ---------------------------------------------------------------------------
# Stage 2: Parallel search and content fetching
# ---------------------------------------------------------------------------


def _extract_ddg_url(href: str) -> str:
    """Extract the real URL from a DuckDuckGo redirect URL."""
    if "duckduckgo.com/l/?" in href or href.startswith("//"):
        # Handle relative URLs like //duckduckgo.com/l/?uddg=...
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        uddg = params.get("uddg")
        if uddg:
            return uddg[0]
    return href


async def _search_duckduckgo(query: str) -> list[dict]:
    """Search DuckDuckGo and return up to 5 results with title, url, snippet."""
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("DuckDuckGo search failed for query %r: %s", query, exc)
        return []

    html = resp.text
    results: list[dict] = []

    # Extract title+url from result__a tags
    title_matches = re.findall(r'<a[^>]*class="result__a"[^>]*href="(.*?)"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippet_matches = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)

    for i, (href, title_html) in enumerate(title_matches):
        title = re.sub(r"<.*?>", "", title_html).strip()
        real_url = _extract_ddg_url(href)
        snippet = ""
        if i < len(snippet_matches):
            snippet = re.sub(r"<.*?>", "", snippet_matches[i]).strip()
        results.append({"title": title, "url": real_url, "snippet": snippet, "query": query})

    return results[:5]


async def _search_bing(query: str) -> list[dict]:
    """Search Bing and return up to 5 results with title, url, snippet.
    Used as fallback when DuckDuckGo is unavailable (e.g. China)."""
    url = f"https://www.bing.com/search?q={quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Bing search failed for query %r: %s", query, exc)
        return []

    html = resp.text
    results: list[dict] = []

    # Bing results live in <li class="b_algo"> blocks
    algo_blocks = re.findall(r'<li\s+class="b_algo"[^>]*>([\s\S]*?)</li>', html, re.DOTALL)
    for block in algo_blocks:
        # Extract link from <h2><a href="...">title</a></h2>
        h2_match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block, re.DOTALL)
        if not h2_match:
            continue
        url_raw = h2_match.group(1)
        title = re.sub(r'<[^>]+>', '', h2_match.group(2)).strip()
        if not title or url_raw.startswith('/') or url_raw.startswith('#'):
            continue

        # Resolve Bing redirect URL (base64-encoded in u parameter)
        u_match = re.search(r'[?&]u=([a-zA-Z0-9+/_=-]+)', url_raw)
        if u_match:
            try:
                b64 = u_match.group(1)[2:].replace('-', '+').replace('_', '/')
                padded = b64 + '=' * (4 - len(b64) % 4) if len(b64) % 4 else b64
                decoded = base64.b64decode(padded).decode('utf-8')
                if decoded.startswith('http'):
                    url_raw = decoded
            except Exception:
                pass

        if 'bing.com' in url_raw and '?' not in url_raw.split('/')[-1]:
            continue

        # Extract snippet
        snippet = ""
        cap_match = re.search(r'<div[^>]*class="b_caption"[^>]*>([\s\S]*?)</div>', block, re.DOTALL)
        if cap_match:
            p_match = re.search(r'<p[^>]*>([\s\S]*?)</p>', cap_match.group(1), re.DOTALL)
            if p_match:
                snippet = re.sub(r'<[^>]+>', '', p_match.group(1)).strip()

        results.append({"title": title, "url": url_raw, "snippet": snippet, "query": query})
        if len(results) >= 5:
            break

    return results[:5]


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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except Exception as exc:
        logger.debug("Failed to fetch URL %r: %s", url, exc)
        return ""

    text = _strip_html(resp.text)
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
        "Only keep results that directly help answer the topic.\n\n"
        f"Topic: {topic}\n\n"
        "Results:\n"
        f"{chr(10).join(lines)}\n\n"
        'Output format:\n'
        'KEEP: 1, 3, 5\n'
        'DISCARD: 2, 4'
    )

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
        1. Query generation (LLM): generate 3-5 search queries
        2. Parallel search (asyncio): search DuckDuckGo + fetch URL contents
        3. Filter (LLM): keep only relevant results
        4. Synthesize (LLM): produce structured answer

    Error handling: any stage that fails falls back gracefully to the next stage.
    """
    logger.info("Deep search starting for: %s", topic)

    # -----------------------------------------------------------------------
    # Stage 1: Generate queries
    # -----------------------------------------------------------------------
    if len(topic) <= 15:
        # Short queries: use the topic directly
        queries = [topic]
    else:
        queries = await _generate_queries(topic)
        if not queries:
            queries = [topic]
    logger.info("Stage 1 complete: generated %d queries", len(queries))

    # -----------------------------------------------------------------------
    # Stage 2: Parallel search and fetch
    # -----------------------------------------------------------------------
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _limited_search(q: str, engine: str) -> list[dict]:
        async with semaphore:
            if engine == "ddg":
                return await _search_duckduckgo(q)
            else:
                return await _search_bing(q)

    # 同时搜索 DuckDuckGo 和 Bing
    search_tasks = [_limited_search(q, "ddg") for q in queries]
    search_tasks += [_limited_search(q, "bing") for q in queries]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    all_results: list[dict] = []
    for sr in search_results:
        if isinstance(sr, list):
            all_results.extend(sr)

    logger.info("Stage 2 search complete: %d raw results (DDG + Bing)", len(all_results))

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
