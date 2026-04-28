"""
Bond News Fetcher — Multi-Source RSS + API Aggregator
======================================================
Aggregates bond-market news from free, public, legal sources:

  1. RBI Press Releases         — Monetary policy, rate decisions, regulations
  2. SEBI Circulars / Orders    — Regulatory changes affecting bond markets
  3. Economic Times Bond News   — Market commentary, analysis
  4. MoneyControl Bonds/FI      — Fixed-income market news
  5. Livemint Markets           — Economic & bond-market news
  6. Business Standard Markets  — Bond market coverage
  7. CCIL News                  — Settlement, clearing related updates

All sources are **free, public, legal** RSS feeds or public web pages.
No API keys or authentication required.

News is stored in the `bond_news` table and served via `/api/v1/news`.
Keyword filtering ensures bond/debt-market relevance.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow, utcnow_iso

log = get_logger("news_fetcher")

# ── Attempt feedparser import (optional dependency) ──────────────────────────
try:
    import feedparser
    _HAS_FEEDPARSER = True
except ImportError:
    _HAS_FEEDPARSER = False
    log.warning("feedparser_not_installed", hint="pip install feedparser")

# ─── RSS Feed Sources ────────────────────────────────────────────────────────
_RSS_SOURCES = [
    {
        "name":     "RBI Press Releases",
        "code":     "RBI",
        "url":      "https://rbi.org.in/rss/rss.aspx",
        "priority": 1,  # highest priority — policy affects all bonds
        "category": "REGULATORY",
    },
    {
        "name":     "RBI Notifications",
        "code":     "RBI_NOTIF",
        "url":      "https://www.rbi.org.in/rss/rss_notf.aspx",
        "priority": 1,
        "category": "REGULATORY",
    },
    {
        "name":     "SEBI Circulars",
        "code":     "SEBI",
        "url":      "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=17&smid=39",
        "priority": 1,
        "category": "REGULATORY",
        "is_html":  True,  # SEBI doesn't have a standard RSS — we scrape
    },
    {
        "name":     "Economic Times - Bonds",
        "code":     "ET_BONDS",
        "url":      "https://economictimes.indiatimes.com/markets/bonds/rssfeeds/12498690.cms",
        "priority": 2,
        "category": "MARKET",
    },
    {
        "name":     "Economic Times - Economy",
        "code":     "ET_ECON",
        "url":      "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms",
        "priority": 3,
        "category": "MACRO",
    },
    {
        "name":     "MoneyControl - Bonds",
        "code":     "MC_BONDS",
        "url":      "https://www.moneycontrol.com/rss/bonds.xml",
        "priority": 2,
        "category": "MARKET",
    },
    {
        "name":     "MoneyControl - Economy",
        "code":     "MC_ECON",
        "url":      "https://www.moneycontrol.com/rss/economy.xml",
        "priority": 3,
        "category": "MACRO",
    },
    {
        "name":     "Livemint - Markets",
        "code":     "LIVEMINT",
        "url":      "https://www.livemint.com/rss/markets",
        "priority": 3,
        "category": "MARKET",
    },
    {
        "name":     "Business Standard - Markets",
        "code":     "BS_MARKETS",
        "url":      "https://www.business-standard.com/rss/markets-104.rss",
        "priority": 3,
        "category": "MARKET",
    },
]

# ─── Keywords for bond-relevance filtering ────────────────────────────────────
_BOND_KEYWORDS = re.compile(
    r'\b('
    r'bond|bonds|debenture|debentures|'
    r'g-sec|gsec|government\s+securities|'
    r'ncd|ncds|non-convertible|'
    r'yield|yields|yield\s+curve|'
    r'rbi|sebi|fimmda|ccil|nds-om|'
    r'repo\s+rate|reverse\s+repo|'
    r'monetary\s+policy|interest\s+rate|'
    r'credit\s+rating|rating\s+downgrade|rating\s+upgrade|'
    r'sdl|state\s+development\s+loan|'
    r'coupon|maturity|fixed\s+income|debt\s+market|'
    r'sovereign|treasury|t-bill|'
    r'inflation|cpi|wpi|gdp|fiscal\s+deficit|'
    r'liquidity|open\s+market\s+operation|omo|'
    r'nbfc|psu\s+bond|corporate\s+bond|'
    r'crisil|icra|care|india\s+ratings|fitch|moody|'
    r'default|haircut|restructur[ie]|'
    r'sbi|hdfc|irfc|lic|nabard|powergrid|ntpc|nhai'
    r')\b',
    re.IGNORECASE,
)

# Keywords that indicate HIGH relevance (boost priority)
_HIGH_RELEVANCE_KEYWORDS = re.compile(
    r'\b('
    r'rbi\s+rate|repo\s+rate|g-sec|gsec|bond\s+yield|'
    r'monetary\s+policy|credit\s+rating|ncd|debenture|'
    r'debt\s+market|fixed\s+income|sovereign|sdl|default|'
    r'yield\s+curve|interest\s+rate\s+(cut|hike|decision)|'
    r'bond\s+market'
    r')\b',
    re.IGNORECASE,
)

# ─── Issuer names for portfolio-specific matching ─────────────────────────────
_PORTFOLIO_ISSUERS = [
    "IRFC", "Indian Railway Finance", "HDB Financial", "L&T Finance",
    "PNB Housing", "Tata Capital", "Tata Motors", "Axis Bank",
    "Gujarat State", "Haryana", "Tamil Nadu", "Maharashtra",
    "West Bengal", "UP Power", "UDAY",
    "NABARD", "Power Grid", "NTPC", "NHAI", "REC", "PFC",
    "SBI", "HDFC", "LIC", "Bajaj Finance", "Muthoot",
]

_ISSUER_RE = re.compile(
    r'\b(' + '|'.join(re.escape(i) for i in _PORTFOLIO_ISSUERS) + r')\b',
    re.IGNORECASE,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class NewsFetcher:
    """
    Multi-source bond news aggregator.
    Fetches from RSS feeds, filters for bond relevance, stores in DB.
    """

    async def fetch_all_sources(self, max_age_days: int = 7) -> dict:
        """
        Fetch news from all configured RSS sources in parallel.
        Returns summary dict with counts per source.
        """
        if not _HAS_FEEDPARSER:
            log.error("feedparser_required")
            return {"status": "error", "message": "feedparser not installed"}

        log.info("news_fetch_start", sources=len(_RSS_SOURCES))

        # Fetch all feeds in parallel
        tasks = [
            self._fetch_feed(src)
            for src in _RSS_SOURCES
            if not src.get("is_html")  # skip HTML-scrape sources for RSS path
        ]
        # Add SEBI scraper separately
        tasks.append(self._fetch_sebi_circulars())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_articles: list[dict] = []
        source_counts = {}
        errors = []

        for r in results:
            if isinstance(r, Exception):
                errors.append(str(r)[:100])
                continue
            if isinstance(r, list):
                for article in r:
                    all_articles.append(article)
                    src = article.get("source", "UNKNOWN")
                    source_counts[src] = source_counts.get(src, 0) + 1

        # Filter for bond relevance
        relevant = self._filter_relevant(all_articles)

        # Deduplicate
        unique = self._deduplicate(relevant)

        # Filter by age
        cutoff = (utcnow() - timedelta(days=max_age_days)).isoformat()
        recent = [a for a in unique if (a.get("published_at") or "") >= cutoff[:10]]

        # Assign relevance scores
        for article in recent:
            article["relevance_score"] = self._compute_relevance(article)
            article["matched_issuers"] = self._match_issuers(article)

        # Sort by relevance then date
        recent.sort(
            key=lambda a: (-a["relevance_score"], a.get("published_at", "") or ""),
            reverse=False,
        )
        recent.sort(key=lambda a: -a["relevance_score"])

        # Store in DB
        stored = await self._store_articles(recent)

        result = {
            "status":       "ok",
            "total_fetched": len(all_articles),
            "relevant":     len(relevant),
            "unique":       len(unique),
            "stored":       stored,
            "sources":      source_counts,
            "errors":       errors[:5],
            "fetched_at":   utcnow_iso(),
        }
        log.info("news_fetch_complete", **{k: v for k, v in result.items()
                                           if k != "errors"})
        return result

    async def get_news(
        self,
        limit: int = 50,
        category: Optional[str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        days: int = 30,
    ) -> list[dict]:
        """Retrieve stored news articles with optional filters."""
        conditions = ["1=1"]
        params: dict = {"lim": limit}

        if category:
            conditions.append("category = :cat")
            params["cat"] = category.upper()
        if source:
            conditions.append("source = :src")
            params["src"] = source.upper()
        if search:
            conditions.append(
                "(title LIKE :q OR summary LIKE :q OR matched_issuers LIKE :q)"
            )
            params["q"] = f"%{search}%"

        conditions.append(
            f"published_at >= datetime('now', '-{days} days')"
        )

        where = " AND ".join(conditions)

        rows = await fetch_all(
            f"""SELECT * FROM bond_news
                WHERE {where}
                ORDER BY relevance_score DESC, published_at DESC
                LIMIT :lim""",
            params,
        )
        return rows

    async def get_news_for_isin(self, isin: str, limit: int = 20) -> list[dict]:
        """
        Get news articles relevant to a specific ISIN.
        Looks up the issuer name from the bond master and searches news.
        """
        bond = await fetch_one(
            "SELECT issuer_name, bond_name FROM universal_bond_master WHERE isin = :isin",
            {"isin": isin},
        )
        if not bond:
            return []

        issuer = bond.get("issuer_name") or bond.get("bond_name") or ""
        # Extract key words from issuer name
        words = [w for w in issuer.split() if len(w) > 3 and w.upper() not in
                 ("LIMITED", "PRIVATE", "INDIA", "CORPORATION", "FINANCE", "THE")]

        if not words:
            return []

        # Search news matching any issuer keyword
        like_conditions = " OR ".join(
            f"(title LIKE :w{i} OR summary LIKE :w{i} OR matched_issuers LIKE :w{i})"
            for i in range(len(words))
        )
        params = {f"w{i}": f"%{w}%" for i, w in enumerate(words)}
        params["lim"] = limit

        rows = await fetch_all(
            f"""SELECT * FROM bond_news
                WHERE ({like_conditions})
                ORDER BY relevance_score DESC, published_at DESC
                LIMIT :lim""",
            params,
        )
        return rows

    async def get_stats(self) -> dict:
        """Return news service statistics."""
        total = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM bond_news"
        )
        by_source = await fetch_all(
            "SELECT source, COUNT(*) AS cnt FROM bond_news GROUP BY source ORDER BY cnt DESC"
        )
        by_category = await fetch_all(
            "SELECT category, COUNT(*) AS cnt FROM bond_news GROUP BY category ORDER BY cnt DESC"
        )
        latest = await fetch_one(
            "SELECT MAX(fetched_at) AS latest, MIN(published_at) AS oldest FROM bond_news"
        )
        return {
            "total_articles": (total or {}).get("cnt", 0),
            "by_source":      by_source,
            "by_category":    by_category,
            "latest_fetch":   (latest or {}).get("latest"),
            "oldest_article": (latest or {}).get("oldest"),
        }

    # ══════════════════════════════════════════════════════════════════
    # RSS FEED FETCHER
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_feed(self, source: dict) -> list[dict]:
        """Fetch and parse a single RSS feed."""
        url  = source["url"]
        code = source["code"]
        cat  = source.get("category", "GENERAL")
        prio = source.get("priority", 3)

        articles: list[dict] = []

        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, follow_redirects=True, timeout=15
            ) as client:
                resp = await client.get(url, timeout=12)
                if resp.status_code != 200:
                    log.debug("rss_fetch_failed", source=code, status=resp.status_code)
                    return []

                feed = feedparser.parse(resp.text)
                if not feed.entries:
                    log.debug("rss_no_entries", source=code)
                    return []

                for entry in feed.entries[:50]:  # cap per source
                    title = _clean_html(entry.get("title", "")).strip()
                    summary = _clean_html(
                        entry.get("summary") or entry.get("description") or ""
                    ).strip()[:1000]
                    link = entry.get("link", "")
                    pub_date = self._parse_feed_date(entry)

                    if not title:
                        continue

                    articles.append({
                        "title":        title,
                        "summary":      summary,
                        "url":          link,
                        "source":       code,
                        "source_name":  source["name"],
                        "category":     cat,
                        "priority":     prio,
                        "published_at": pub_date,
                    })

                log.info("rss_fetched", source=code, count=len(articles))

        except Exception as exc:
            log.warning("rss_fetch_error", source=code, err=str(exc)[:80])

        return articles

    async def _fetch_sebi_circulars(self) -> list[dict]:
        """
        Fetch recent SEBI circulars/orders that may affect bonds.
        SEBI publishes circulars at their website (no RSS, so we scrape).
        """
        articles: list[dict] = []
        url = ("https://www.sebi.gov.in/sebiweb/ajax/AjaxAction.jsp"
               "?methodName=getCircularsByYear"
               f"&year={date.today().year}&type=circular")

        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, follow_redirects=True, timeout=15
            ) as client:
                resp = await client.get(url, timeout=12)
                if resp.status_code != 200:
                    # Try the main listing page
                    resp = await client.get(
                        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
                        "?doListing=yes&sid=3&ssid=17&smid=39",
                        timeout=12,
                    )
                    if resp.status_code != 200:
                        return []

                body = resp.text
                # Try JSON parse
                try:
                    data = resp.json()
                    items = data if isinstance(data, list) else data.get("data", [])
                    for item in items[:30]:
                        title = str(item.get("title") or item.get("heading") or "").strip()
                        link = str(item.get("link") or item.get("url") or "")
                        if link and not link.startswith("http"):
                            link = "https://www.sebi.gov.in" + link
                        pub_date = _parse_date_str(
                            item.get("date") or item.get("publishDate") or ""
                        )
                        if title:
                            articles.append({
                                "title":        title,
                                "summary":      title,
                                "url":          link,
                                "source":       "SEBI",
                                "source_name":  "SEBI Circulars",
                                "category":     "REGULATORY",
                                "priority":     1,
                                "published_at": pub_date,
                            })
                except Exception:
                    # HTML parse fallback — extract circular links
                    import re as _re
                    patt = _re.compile(
                        r'<a\s+href="([^"]*)"[^>]*>\s*(.*?)\s*</a>', _re.IGNORECASE
                    )
                    for m in patt.finditer(body):
                        link, text = m.group(1), _clean_html(m.group(2))
                        if not text or len(text) < 15:
                            continue
                        if any(k in text.lower() for k in
                               ["bond", "debenture", "debt", "ncd", "fixed income",
                                "credit rating", "sebi", "circular"]):
                            if not link.startswith("http"):
                                link = "https://www.sebi.gov.in" + link
                            articles.append({
                                "title":        text[:200],
                                "summary":      text[:500],
                                "url":          link,
                                "source":       "SEBI",
                                "source_name":  "SEBI Circulars",
                                "category":     "REGULATORY",
                                "priority":     1,
                                "published_at": date.today().isoformat(),
                            })

            log.info("sebi_circulars_fetched", count=len(articles))

        except Exception as exc:
            log.warning("sebi_fetch_error", err=str(exc)[:80])

        return articles

    # ══════════════════════════════════════════════════════════════════
    # FILTERING & SCORING
    # ══════════════════════════════════════════════════════════════════

    def _filter_relevant(self, articles: list[dict]) -> list[dict]:
        """Filter articles for bond-market relevance using keywords."""
        relevant = []
        for article in articles:
            text = f"{article.get('title', '')} {article.get('summary', '')}"
            # Regulatory sources are always relevant
            if article.get("category") == "REGULATORY":
                relevant.append(article)
                continue
            # Bond-related keywords check
            if _BOND_KEYWORDS.search(text):
                relevant.append(article)
        return relevant

    def _compute_relevance(self, article: dict) -> int:
        """
        Compute a relevance score (0-100) for an article.
        Higher = more relevant to bond portfolio decisions.
        """
        text = f"{article.get('title', '')} {article.get('summary', '')}"
        score = 0

        # Base score from source priority (1=highest → 30pts)
        priority = article.get("priority", 3)
        score += max(0, (4 - priority)) * 10  # 30, 20, 10, 0

        # High-relevance keywords
        high_matches = len(_HIGH_RELEVANCE_KEYWORDS.findall(text))
        score += min(high_matches * 10, 30)

        # Any bond keyword match
        matches = len(_BOND_KEYWORDS.findall(text))
        score += min(matches * 3, 15)

        # Portfolio issuer mention
        issuer_matches = _ISSUER_RE.findall(text)
        if issuer_matches:
            score += min(len(issuer_matches) * 5, 15)

        # Regulatory category bonus
        if article.get("category") == "REGULATORY":
            score += 10

        return min(score, 100)

    def _match_issuers(self, article: dict) -> str:
        """Find portfolio-relevant issuer names mentioned in the article."""
        text = f"{article.get('title', '')} {article.get('summary', '')}"
        matches = set(_ISSUER_RE.findall(text))
        return ", ".join(sorted(matches)) if matches else ""

    def _deduplicate(self, articles: list[dict]) -> list[dict]:
        """Remove duplicate articles based on title hash."""
        seen: set[str] = set()
        unique: list[dict] = []
        for article in articles:
            # Hash title + source for dedup
            key = hashlib.md5(
                f"{article.get('title', '')[:80]}|{article.get('source', '')}".lower()
                .encode()
            ).hexdigest()
            if key not in seen:
                seen.add(key)
                article["hash_id"] = key
                unique.append(article)
        return unique

    # ══════════════════════════════════════════════════════════════════
    # DATE PARSING
    # ══════════════════════════════════════════════════════════════════

    def _parse_feed_date(self, entry) -> str:
        """Parse published date from RSS feed entry."""
        # feedparser normalizes dates in entry.published_parsed
        parsed = getattr(entry, "published_parsed", None) or \
                 getattr(entry, "updated_parsed", None)
        if parsed:
            try:
                from time import mktime
                return datetime.fromtimestamp(mktime(parsed)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        # Fallback to string parsing
        raw = (entry.get("published") or entry.get("updated") or
               entry.get("pubDate") or "")
        return _parse_date_str(raw) or utcnow_iso()[:19].replace("T", " ")

    # ══════════════════════════════════════════════════════════════════
    # DATABASE
    # ══════════════════════════════════════════════════════════════════

    async def _store_articles(self, articles: list[dict]) -> int:
        """Store articles in bond_news table. Upserts on hash_id."""
        stored = 0
        for a in articles:
            hash_id = a.get("hash_id") or hashlib.md5(
                f"{a.get('title', '')}|{a.get('source', '')}".lower().encode()
            ).hexdigest()
            try:
                await execute_query(
                    """INSERT INTO bond_news
                        (hash_id, title, summary, url, source, source_name,
                         category, relevance_score, matched_issuers,
                         published_at, fetched_at)
                       VALUES
                        (:hash_id, :title, :summary, :url, :source, :source_name,
                         :category, :relevance_score, :matched_issuers,
                         :published_at, datetime('now'))
                       ON CONFLICT(hash_id) DO UPDATE SET
                         relevance_score = CASE
                           WHEN excluded.relevance_score > bond_news.relevance_score
                           THEN excluded.relevance_score
                           ELSE bond_news.relevance_score
                         END,
                         fetched_at = datetime('now')""",
                    {
                        "hash_id":          hash_id,
                        "title":            a.get("title", "")[:500],
                        "summary":          a.get("summary", "")[:2000],
                        "url":              a.get("url", "")[:1000],
                        "source":           a.get("source", "UNKNOWN")[:20],
                        "source_name":      a.get("source_name", "")[:100],
                        "category":         a.get("category", "GENERAL")[:20],
                        "relevance_score":  a.get("relevance_score", 0),
                        "matched_issuers":  a.get("matched_issuers", "")[:500],
                        "published_at":     a.get("published_at", ""),
                    },
                )
                stored += 1
            except Exception as exc:
                log.debug("news_store_error", title=a.get("title", "")[:40],
                          err=str(exc)[:60])
        return stored

    async def ensure_table(self) -> None:
        """Create bond_news table if it doesn't exist."""
        try:
            await execute_query("""
                CREATE TABLE IF NOT EXISTS bond_news (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash_id      TEXT UNIQUE NOT NULL,
                    title        TEXT NOT NULL,
                    summary      TEXT,
                    url          TEXT,
                    source       TEXT NOT NULL,
                    source_name  TEXT,
                    category     TEXT DEFAULT 'GENERAL',
                    relevance_score INTEGER DEFAULT 0,
                    matched_issuers TEXT,
                    published_at TEXT,
                    fetched_at   TEXT DEFAULT (datetime('now')),
                    is_read      INTEGER DEFAULT 0
                )
            """)
            # Create index for faster queries
            await execute_query("""
                CREATE INDEX IF NOT EXISTS idx_bond_news_published
                ON bond_news(published_at DESC)
            """)
            await execute_query("""
                CREATE INDEX IF NOT EXISTS idx_bond_news_relevance
                ON bond_news(relevance_score DESC, published_at DESC)
            """)
            await execute_query("""
                CREATE INDEX IF NOT EXISTS idx_bond_news_source
                ON bond_news(source)
            """)
        except Exception as exc:
            log.debug("news_table_create_note", err=str(exc)[:60])


# ── Helper functions ──────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _parse_date_str(raw: str) -> str:
    """Best-effort date string parsing."""
    if not raw:
        return ""
    raw = str(raw).strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",  # RFC 822
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",  # ISO 8601
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d %b %Y",
        "%B %d, %Y",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    # Last resort: just return the raw string
    return raw[:19]
