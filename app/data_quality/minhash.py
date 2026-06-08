"""MinHash-based near-duplicate detection backed by the dedup_memory DB table.

Design: num_perm=128 permutations split into band_width=4 hash buckets → 32 bands
per document. A band collision in dedup_memory signals a near-duplicate; an exact
content_hash match signals an exact duplicate. Process restarts are transparent
because all state lives in the DB.

Schema (dedup_memory):
  id uuid PK
  content_hash text NOT NULL
  band_hash text NOT NULL  (format: "band_{n}:{hex_digest}")
  seen_at timestamptz NOT NULL DEFAULT now()
  UNIQUE(content_hash, band_hash)
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _shingles(text: str, shingle_size: int) -> set[str]:
    """Generate character-level shingles from text."""
    text = re.sub(r"\s+", " ", text.lower().strip())
    if len(text) < shingle_size:
        return {text}
    return {text[i : i + shingle_size] for i in range(len(text) - shingle_size + 1)}


def _compute_content_hash(text: str) -> str:
    """SHA-256 hex digest of normalised text — used for exact-dup detection."""
    normalised = re.sub(r"\s+", " ", text.lower().strip())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _compute_band_hashes(
    text: str,
    num_perm: int,
    band_width: int,
    shingle_size: int,
    seed: int,
) -> list[str]:
    """Compute one band_hash string per LSH band.

    Uses a deterministic, datasketch-free approach:
    - Build num_perm independent hash lanes via SHA-256 keyed with (seed, lane).
    - Split into (num_perm // band_width) bands of band_width lanes each.
    - Each band_hash = "band_{n}:{hex(sha256(concat of lane values))}".

    This is reproducible across process restarts and Python version upgrades.
    """
    shingles = _shingles(text, shingle_size)
    if not shingles:
        return []

    # Build num_perm independent min-values (one per permutation lane)
    min_values: list[int] = []
    for lane in range(num_perm):
        lane_key = f"{seed}:{lane}".encode()
        lane_min = None
        for sh in shingles:
            h = hashlib.sha256(lane_key + sh.encode("utf-8")).digest()
            val = int.from_bytes(h[:8], "big")
            if lane_min is None or val < lane_min:
                lane_min = val
        min_values.append(lane_min or 0)

    # Split into bands
    num_bands = num_perm // band_width
    band_hashes: list[str] = []
    for b in range(num_bands):
        band_vals = min_values[b * band_width : (b + 1) * band_width]
        concat = b"".join(v.to_bytes(8, "big") for v in band_vals)
        digest = hashlib.sha256(concat).hexdigest()
        band_hashes.append(f"band_{b}:{digest}")

    return band_hashes


class MinHashDedup:
    """Near-duplicate detection backed by the dedup_memory Supabase table.

    Usage::

        dedup = MinHashDedup(supabase_client, threshold=0.9, num_perm=128,
                             band_width=4, shingle_size=5, seed=42)
        if dedup.is_near_duplicate(text):
            skip_job()
        else:
            dedup.add(text, content_hash)

    All DB calls are synchronous (supabase-py); callers from async context must
    wrap via asyncio.to_thread.
    """

    def __init__(
        self,
        supabase_client,
        threshold: float = 0.9,
        num_perm: int = 128,
        band_width: int = 4,
        shingle_size: int = 5,
        seed: int = 42,
        window_days: int = 42,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold}")
        if num_perm < 1:
            raise ValueError(f"num_perm must be >= 1, got {num_perm}")
        if band_width < 1 or num_perm % band_width != 0:
            raise ValueError(
                f"band_width must be >= 1 and divide num_perm evenly, "
                f"got band_width={band_width}, num_perm={num_perm}"
            )
        if shingle_size < 1:
            raise ValueError(f"shingle_size must be >= 1, got {shingle_size}")

        self.client = supabase_client
        self.threshold = threshold
        self.num_perm = num_perm
        self.band_width = band_width
        self.shingle_size = shingle_size
        self.seed = seed
        self.window_days = window_days

        logger.info(
            "MinHashDedup initialised (DB-backed)",
            extra={
                "threshold": threshold,
                "num_perm": num_perm,
                "band_width": band_width,
                "shingle_size": shingle_size,
                "seed": seed,
                "window_days": window_days,
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_near_duplicate(self, text: str) -> bool:
        """Return True if *text* is a near-duplicate of any stored entry.

        Queries dedup_memory for any band_hash collision WITHIN the retention
        window (seen_at >= now - window_days); a single shared band is sufficient
        to declare a near-duplicate match. Entries older than the window are
        ignored even before purge_old() physically removes them, so the dedup
        memory genuinely "forgets" after window_days.
        """
        if not text:
            return False

        band_hashes = _compute_band_hashes(
            text, self.num_perm, self.band_width, self.shingle_size, self.seed
        )
        if not band_hashes:
            return False

        window_start = (
            datetime.now(tz=timezone.utc) - timedelta(days=self.window_days)
        ).isoformat()
        try:
            result = (
                self.client.table("dedup_memory")
                .select("band_hash")
                .in_("band_hash", band_hashes)
                .gte("seen_at", window_start)
                .limit(1)
                .execute()
            )
            is_dup = bool(result.data)
            if is_dup:
                logger.info(
                    "near_duplicate_detected",
                    extra={"match": result.data[0].get("band_hash", "")[:40]},
                )
            return is_dup
        except Exception as exc:
            logger.warning(
                "dedup_memory_query_failed",
                extra={"error": str(exc)},
            )
            return False

    def add(self, text: str, content_hash: str | None = None) -> None:
        """Upsert band-hash rows + a content_hash sentinel into dedup_memory.

        Inserts 32 rows (one per band) with ON CONFLICT (content_hash, band_hash)
        DO NOTHING so concurrent writes are safe.

        *content_hash* — caller may supply the already-computed SHA-256 hash to
        avoid recomputing; if omitted it is derived from *text*.
        """
        if not text:
            return

        ch = content_hash or _compute_content_hash(text)
        band_hashes = _compute_band_hashes(
            text, self.num_perm, self.band_width, self.shingle_size, self.seed
        )
        if not band_hashes:
            return

        now = datetime.now(tz=timezone.utc).isoformat()
        rows = [
            {"content_hash": ch, "band_hash": bh, "seen_at": now}
            for bh in band_hashes
        ]

        try:
            self.client.table("dedup_memory").upsert(
                rows,
                on_conflict="content_hash,band_hash",
                ignore_duplicates=True,
            ).execute()
            logger.debug(
                "dedup_memory_upserted",
                extra={"content_hash": ch[:12], "bands": len(rows)},
            )
        except Exception as exc:
            logger.warning(
                "dedup_memory_upsert_failed",
                extra={"error": str(exc), "content_hash": ch[:12]},
            )

    def purge_old(self) -> int:
        """Delete dedup_memory rows older than window_days. Returns deleted count.

        Should be called periodically (e.g. daily via cron) to bound table size.
        """
        from datetime import timedelta

        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(days=self.window_days)
        ).isoformat()
        try:
            result = (
                self.client.table("dedup_memory")
                .delete()
                .lt("seen_at", cutoff)
                .execute()
            )
            deleted = len(result.data or [])
            logger.info(
                "dedup_memory_purged",
                extra={"deleted": deleted, "window_days": self.window_days},
            )
            return deleted
        except Exception as exc:
            logger.warning(
                "dedup_memory_purge_failed",
                extra={"error": str(exc)},
            )
            return 0
