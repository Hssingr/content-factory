"""Asset manager — thread-safe immediate download cache for media assets.

Downloads remote URLs to {media_path}/cache/ at candidate-selection time, so
``media_url`` on every beat is always a local relative path before Remotion
ever sees it.  Remotion and Chromium never open an internet connection.

Design:
  - Hash-based filename: SHA-256(url)[:24] + extension — stable, deterministic.
  - Two-level cache: in-memory dict (O(1)) → disk (stat check).
  - Per-URL threading.Event for deduplication: if two threads request the same
    URL concurrently, one downloads while the other waits — no double download.
  - Raises AssetDownloadError on failure so the caller decides the fallback.

Returned paths are relative to settings.media_path (Remotion's --public-dir
root), e.g.  ``"cache/abc123def456789012345678.mp4"``.  They are never
absolute, never http://, and always resolvable by Remotion's staticFile().
"""

import hashlib
import logging
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings

logger = logging.getLogger(__name__)

_CACHE_SUBDIR     = "cache"
_DOWNLOAD_TIMEOUT = 30
_MAX_RETRIES      = 2
_RETRY_DELAY      = 1
_MIN_FILE_BYTES   = 512

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MediaFactory/1.0)",
}

# Thread-safe state
_state_lock = threading.Lock()
_in_flight:  dict[str, threading.Event] = {}   # url → event, set when download done
_completed:  dict[str, str]             = {}   # url → local relative path (memory cache)


class AssetDownloadError(Exception):
    """Raised when a remote asset cannot be downloaded after all retries."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _media_root() -> Path:
    return Path(settings.media_path).resolve()


def _cache_dir() -> Path:
    d = _media_root() / _CACHE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dest_path(url: str, media_type: str) -> Path:
    ext = ".mp4" if media_type == "video" else ".jpg"
    return _cache_dir() / f"{compute_hash(url)}{ext}"


def _rel_path(abs_path: Path) -> str:
    """Return path relative to media_path (Remotion --public-dir root)."""
    try:
        return str(abs_path.relative_to(_media_root()))
    except ValueError:
        return str(abs_path)


def _do_download(url: str, dest: Path) -> None:
    """Download url to dest with retry.

    Args:
        url:  Remote URL to download.
        dest: Absolute local destination path.

    Raises:
        AssetDownloadError: If all download attempts fail or response is too small.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = Request(url, headers=_HTTP_HEADERS)
            with urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
                data = resp.read()
            if len(data) < _MIN_FILE_BYTES:
                raise AssetDownloadError(
                    f"Response too small ({len(data)} bytes) for {url[:80]!r}"
                )
            dest.write_bytes(data)
            return
        except AssetDownloadError:
            raise
        except (HTTPError, URLError, OSError) as exc:
            logger.warning(
                "AssetManager: download attempt %d/%d failed for %.80s: %s",
                attempt, _MAX_RETRIES, url, exc,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)
    raise AssetDownloadError(
        f"All {_MAX_RETRIES} download attempts failed for {url[:80]!r}"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def compute_hash(url: str) -> str:
    """Return a 24-character SHA-256 hex digest of the URL.

    Args:
        url: Remote media URL to hash.

    Returns:
        24-character lowercase hex string, stable across restarts.
    """
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def validate_local_file(path: str) -> bool:
    """Return True if path points to an existing non-empty local file.

    Accepts both absolute paths and paths relative to settings.media_path.

    Args:
        path: File path to validate (absolute or media_path-relative).

    Returns:
        True if the file exists and is larger than 512 bytes.
    """
    try:
        p = Path(path)
        if not p.is_absolute():
            p = _media_root() / path
        return p.exists() and p.stat().st_size > _MIN_FILE_BYTES
    except OSError:
        return False


def get_cached_asset(url: str, media_type: str) -> str | None:
    """Return local relative path if url is already cached on disk, else None.

    Does not attempt a download — use download_if_needed() for that.

    Args:
        url:        Remote URL to look up.
        media_type: "video" or "image".

    Returns:
        Path relative to media_path root (e.g. "cache/abc123.mp4"), or None
        if the asset is not cached.
    """
    with _state_lock:
        if url in _completed:
            return _completed[url]
    dest = _dest_path(url, media_type)
    if dest.exists() and dest.stat().st_size > _MIN_FILE_BYTES:
        rel = _rel_path(dest)
        with _state_lock:
            _completed[url] = rel
        return rel
    return None


def download_if_needed(url: str, media_type: str) -> str:
    """Download url to local cache and return a path relative to media_path.

    Returns the cached path immediately if the file already exists on disk or
    in memory.  Thread-safe: concurrent calls for the same URL deduplicate via
    a per-URL threading.Event — only one download runs; others wait for it.

    Args:
        url:        Remote https:// URL to download.
        media_type: "video" or "image" — determines file extension (.mp4/.jpg).

    Returns:
        Path relative to settings.media_path root, e.g. "cache/abc123.mp4".
        Always starts with "cache/" and never contains "http".

    Raises:
        AssetDownloadError: If the download failed after all retries or an
            in-flight download from another thread did not complete in time.
    """
    with _state_lock:
        # Memory cache hit
        if url in _completed:
            logger.debug("AssetManager: memory hit url=%.80s", url)
            return _completed[url]

        # Disk cache hit
        dest = _dest_path(url, media_type)
        if dest.exists() and dest.stat().st_size > _MIN_FILE_BYTES:
            rel = _rel_path(dest)
            _completed[url] = rel
            logger.info("AssetManager: disk hit url=%.80s → %s", url, rel)
            return rel

        # Check if another thread is downloading this URL
        if url in _in_flight:
            waiter_event  = _in_flight[url]
            is_downloader = False
        else:
            waiter_event        = threading.Event()
            _in_flight[url]     = waiter_event
            is_downloader       = True

    if not is_downloader:
        # Wait for the downloading thread to finish
        logger.debug("AssetManager: waiting for in-flight download url=%.80s", url)
        waiter_event.wait(timeout=_DOWNLOAD_TIMEOUT + 5)
        with _state_lock:
            if url in _completed:
                return _completed[url]
        raise AssetDownloadError(
            f"In-flight download for {url[:80]!r} did not complete in time"
        )

    # This thread is the downloader
    try:
        logger.info(
            "AssetManager: downloading url=%.80s type=%s dest=%s",
            url, media_type, dest.name,
        )
        _do_download(url, dest)
        rel = _rel_path(dest)
        with _state_lock:
            _completed[url] = rel
            if url in _in_flight:
                _in_flight[url].set()
                del _in_flight[url]
        logger.info("AssetManager: downloaded → %s", rel)
        return rel
    except Exception:
        with _state_lock:
            if url in _in_flight:
                _in_flight[url].set()   # unblock any waiters even on failure
                del _in_flight[url]
        raise


def reset_memory_cache() -> None:
    """Clear in-memory cache (useful between test runs, not for production).

    Does not delete files on disk — disk cache persists across resets.
    """
    with _state_lock:
        _completed.clear()
        _in_flight.clear()


# ── Migration helper ──────────────────────────────────────────────────────────

def repair_local_media_for_existing_sections(
    sections: list[dict],
) -> tuple[list[dict], int, int]:
    """Download any remote media_url values in existing sections to local cache.

    Used for content that was generated before the immediate-download
    architecture was in place.  Mutates each section in-place if a remote URL
    is found and successfully downloaded.

    Args:
        sections: List of section/beat dicts, each possibly carrying a remote
            ``media_url`` and/or remote ``url`` values inside ``clips``.

    Returns:
        ``(sections, repaired, failed)`` — the same list (mutated in-place),
        count of sections whose URL was replaced, and count of failures.
    """
    repaired = 0
    failed   = 0

    for s in sections:
        media_type = s.get("media_type", "image")
        url        = s.get("media_url", "")

        if url and url.startswith("http"):
            try:
                local = download_if_needed(url, media_type)
                s["original_media_url"] = url
                s["local_media_path"]   = local
                s["media_url"]          = local
                # Patch first clip too
                if s.get("clips"):
                    s["clips"][0]["url"] = local
                repaired += 1
                logger.info(
                    "repair: section=%s remote_url=%.80s → %s",
                    s.get("section_order", "?"), url, local,
                )
            except AssetDownloadError as exc:
                failed += 1
                logger.error(
                    "repair: section=%s failed to download %.80s: %s",
                    s.get("section_order", "?"), url, exc,
                )

    logger.info(
        "MEDIA_REPAIR: total=%d repaired=%d failed=%d",
        len(sections), repaired, failed,
    )
    return sections, repaired, failed
