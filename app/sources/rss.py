import logging
from datetime import datetime

import feedparser

from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("rss")
class RssScraper(BaseScraper):
    source_id = "rss"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            feeds = config.get("feeds", [])
            all_jobs = []
            for feed_cfg in feeds:
                feed_url = feed_cfg.get("url", "")
                feed_name = feed_cfg.get("name", "unknown")
                if not feed_url:
                    continue
                try:
                    parsed = feedparser.parse(feed_url)
                    for entry in parsed.entries:
                        posted_at = None
                        if (
                            hasattr(entry, "published_parsed")
                            and entry.published_parsed
                        ):
                            try:
                                posted_at = datetime(
                                    *entry.published_parsed[:6]
                                )
                            except Exception:
                                pass
                        raw = RawJob(
                            title=entry.get("title", ""),
                            url=entry.get("link", ""),
                            company=feed_name,
                            description=entry.get("summary", ""),
                            source="rss",
                            external_id=entry.get(
                                "id", entry.get("link", "")
                            ),
                            posted_at=posted_at,
                        )
                        all_jobs.append(raw)
                except Exception as e:
                    logger.warning(f"RSS feed '{feed_name}' failed: {e}")
                    continue

            logger.info(
                f"RSS: fetched {len(all_jobs)} jobs from {len(feeds)} feeds"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"RSS fetch failed: {e}")
            return []
