import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone


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
