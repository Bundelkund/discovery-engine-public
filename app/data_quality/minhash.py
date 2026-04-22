"""MinHash-LSH based near-duplicate detection for job descriptions."""
import logging
import re
from typing import Any

from datasketch import MinHash, MinHashLSH

logger = logging.getLogger(__name__)


def _shingles(text: str, shingle_size: int) -> set[str]:
    """Generate character-level shingles from text."""
    text = re.sub(r"\s+", " ", text.lower().strip())
    if len(text) < shingle_size:
        return {text}
    return {text[i : i + shingle_size] for i in range(len(text) - shingle_size + 1)}


def _build_minhash(text: str, num_perm: int, shingle_size: int, seed: int) -> MinHash:
    """Create a MinHash object for the given text with a deterministic seed."""
    m = MinHash(num_perm=num_perm, seed=seed)
    for shingle in _shingles(text, shingle_size):
        m.update(shingle.encode("utf-8"))
    return m


class MinHashDedup:
    """Near-duplicate detection using MinHash-LSH.

    Usage::

        dedup = MinHashDedup(threshold=0.9, num_perm=128, shingle_size=5, seed=42)
        if dedup.is_near_duplicate(text, existing_hashes=[]):
            skip_job()
        else:
            dedup.add(text, job_id)

    *seed* controls the permutation hash seed so that MinHash signatures are
    reproducible across process restarts and datasketch-version upgrades.
    """

    def __init__(
        self,
        threshold: float = 0.9,
        num_perm: int = 128,
        shingle_size: int = 5,
        seed: int = 42,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold}")
        if num_perm < 1:
            raise ValueError(f"num_perm must be >= 1, got {num_perm}")
        if shingle_size < 1:
            raise ValueError(f"shingle_size must be >= 1, got {shingle_size}")

        self.threshold = threshold
        self.num_perm = num_perm
        self.shingle_size = shingle_size
        self.seed = seed

        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._hashes: dict[str, MinHash] = {}
        logger.info(
            "MinHashDedup initialised",
            extra={
                "threshold": threshold,
                "num_perm": num_perm,
                "shingle_size": shingle_size,
                "seed": seed,
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_near_duplicate(
        self,
        text: str,
        existing_hashes: list[Any] | None = None,  # noqa: ARG002
    ) -> bool:
        """Return True if *text* is a near-duplicate of any stored entry.

        *existing_hashes* is accepted for signature compatibility but is not
        used — the LSH index is the authoritative store.
        """
        if not text:
            return False
        m = _build_minhash(text, self.num_perm, self.shingle_size, self.seed)
        result = self._lsh.query(m)
        is_dup = len(result) > 0
        if is_dup:
            logger.info(
                "Near-duplicate detected",
                extra={"matches": result[:3]},
            )
        return is_dup

    def add(self, text: str, job_id: str) -> None:
        """Add *text* to the LSH index under *job_id*.

        If *job_id* is already in the index, the call is a no-op.
        """
        if not text or not job_id:
            return
        if job_id in self._hashes:
            logger.debug("Job already indexed, skipping", extra={"job_id": job_id})
            return
        m = _build_minhash(text, self.num_perm, self.shingle_size, self.seed)
        self._lsh.insert(job_id, m)
        self._hashes[job_id] = m
        logger.debug("Added to MinHash index", extra={"job_id": job_id})

    def remove(self, job_id: str) -> None:
        """Remove *job_id* from the LSH index.

        Called by the orchestrator when a job is rejected by DQ rules — prevents
        rejected content from blocking future near-duplicates and caps index size.
        """
        if not job_id or job_id not in self._hashes:
            return
        try:
            self._lsh.remove(job_id)
        except Exception as exc:
            logger.warning(
                "lsh_remove_failed",
                extra={"job_id": job_id, "error": str(exc)},
            )
        self._hashes.pop(job_id, None)
        logger.debug("Removed from MinHash index", extra={"job_id": job_id})

    def bulk_add(self, items: list[tuple[str, str]]) -> None:
        """Bulk-add (text, job_id) pairs — preferred for >10 k items."""
        for text, job_id in items:
            self.add(text, job_id)

    @property
    def size(self) -> int:
        """Number of entries currently in the index."""
        return len(self._hashes)
