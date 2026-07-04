import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Roadmap 4.1 / audit S-1 (exec-2): source-material starvation fix. Discovery
# used to store only a 200-500 word factual summary as the sole fact source
# for blueprint, every section, length-correction expansion, and Shorts
# grounding — a 9-minute documentary written from a paragraph. fetcher.py now
# asks Claude to return the full verbatim post text + top comments it
# retrieved via web_search (not a Reddit API/scraping fetch — no Reddit API
# key is available), capped by this shared ceiling everywhere `story.body`/
# `Content.source_excerpt` is truncated before being sent to a Claude call,
# so the persisted excerpt and every downstream consumer agree on one budget.
MAX_SOURCE_EXCERPT_CHARS = 60_000


@dataclass
class Story:
    url: str
    title: str
    body: str
    language: str       # taken from channel_sources.language
    source_type: str    # 'rss' | 'reddit'
    source_value: str   # original source identifier (feed URL or r/subreddit)
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    upvotes: int = 0    # Reddit score; 0 for RSS
    comments: int = 0   # comment/reply count

    @property
    def content_hash(self) -> str:
        """SHA-256(URL + title) — matches the deduplication key stored in content.content_hash."""
        return hashlib.sha256(f"{self.url}{self.title}".encode()).hexdigest()
