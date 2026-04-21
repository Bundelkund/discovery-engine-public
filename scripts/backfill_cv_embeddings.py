"""Backfill cv_embedding for profiles with cv_text but no embedding.

Uses OpenAI text-embedding-3-small (1536 dimensions).
Safe to re-run: only processes profiles where cv_embedding IS NULL.

Usage (inside container):
    docker exec discovery-engine-discovery-engine-1 python -m scripts.backfill_cv_embeddings

Or locally with .env:
    python -m scripts.backfill_cv_embeddings
"""

import asyncio
import logging
import sys

from openai import AsyncOpenAI
from supabase import create_client

from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_cv_embeddings")

EMBEDDING_MODEL = "text-embedding-3-small"
MIN_CV_TEXT_LEN = 100
CV_TEXT_CAP = 8000


async def _embed(client: AsyncOpenAI, text: str) -> list[float]:
    response = await client.embeddings.create(
        input=[text[:CV_TEXT_CAP]],
        model=EMBEDDING_MODEL,
    )
    return response.data[0].embedding


async def main() -> int:
    settings = get_settings()
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not configured")
        return 1

    supabase = create_client(settings.supabase_url, settings.supabase_key)
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

    result = (
        supabase.table("profiles")
        .select("id, name, cv_text")
        .is_("cv_embedding", "null")
        .execute()
    )
    rows = result.data or []
    candidates = [
        r for r in rows
        if r.get("cv_text") and len(r["cv_text"]) >= MIN_CV_TEXT_LEN
    ]

    logger.info(
        "Found %d profiles, %d eligible (cv_text >= %d chars)",
        len(rows), len(candidates), MIN_CV_TEXT_LEN,
    )

    updated = 0
    failed = 0
    for row in candidates:
        profile_id = row["id"]
        name = row.get("name") or profile_id[:8]
        try:
            embedding = await _embed(openai_client, row["cv_text"])
            supabase.table("profiles").update(
                {"cv_embedding": embedding}
            ).eq("id", profile_id).execute()
            updated += 1
            logger.info(
                "OK  %s (%s) — %d dims", profile_id[:8], name, len(embedding)
            )
        except Exception as e:
            failed += 1
            logger.error("FAIL %s (%s): %s", profile_id[:8], name, e)

    logger.info("Done. updated=%d failed=%d", updated, failed)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
