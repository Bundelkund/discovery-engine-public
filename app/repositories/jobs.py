import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.models.job import ScoredJob
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class JobRepository(BaseRepository):
    # TABLE is resolved at runtime from the JOBS_TABLE env var so both
    # reads and writes follow the active shelf without a restart or redeploy.
    # Access via self._table rather than the class constant everywhere.

    @property
    def _table(self) -> str:
        """Active jobs shelf: JOBS_TABLE env var (default 'jobs_v2')."""
        return get_settings().jobs_table

    def get_coverage_metrics(self) -> dict:
        """Return coverage metrics for /health.

        Each sub-query is wrapped individually so a partial failure still
        yields the metrics that did succeed. Called once per /health request.
        """
        metrics = {
            "jobs_total": 0,
            "location_normalized_pct": 0.0,
            "dq_flags_pct": 0.0,
            "jobs_last_24h": 0,
        }

        total = 0
        try:
            res = self.client.table(self._table).select("id", count="exact").limit(1).execute()
            total = res.count or 0
            metrics["jobs_total"] = total
        except Exception as exc:
            logger.warning("coverage_total_failed", extra={"error": str(exc)})

        if total > 0:
            try:
                res = (
                    self.client.table(self._table)
                    .select("id", count="exact")
                    .not_.is_("location_normalized", "null")
                    .limit(1)
                    .execute()
                )
                loc_count = res.count or 0
                metrics["location_normalized_pct"] = round(100.0 * loc_count / total, 2)
            except Exception as exc:
                logger.warning("coverage_location_pct_failed", extra={"error": str(exc)})

            try:
                # dq_flags default is {}; count rows where it has been populated.
                res = (
                    self.client.table(self._table)
                    .select("id", count="exact")
                    .neq("dq_flags", "{}")
                    .limit(1)
                    .execute()
                )
                dq_count = res.count or 0
                metrics["dq_flags_pct"] = round(100.0 * dq_count / total, 2)
            except Exception as exc:
                logger.warning("coverage_dq_pct_failed", extra={"error": str(exc)})

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            res = (
                self.client.table(self._table)
                .select("id", count="exact")
                .gte("scraped_at", cutoff)
                .limit(1)
                .execute()
            )
            metrics["jobs_last_24h"] = res.count or 0
        except Exception as exc:
            logger.warning("coverage_last24h_failed", extra={"error": str(exc)})

        return metrics

    async def upsert(self, jobs: list[ScoredJob]) -> list[bool]:
        """Upsert refined jobs into the active shelf (self._table, default jobs_v2).

        ON CONFLICT (source, external_id):
          - insert: sets first_seen_at = last_seen_at = now(), status = 'active'
          - update: refreshes last_seen_at + all mutable fields; preserves first_seen_at.

        No profile_id — agnostik invariant.

        Returns a per-row success flag aligned to ``jobs`` input order: True when the
        row reached the shelf, False when its individual upsert raised. The caller
        (refine pipeline) marks ONLY the True rows 'refined' and leaves the False ones
        'new' to retry — a row swallowed here must never look refined upstream.
        """
        if not jobs:
            return []

        rows = []
        now = datetime.now(timezone.utc).isoformat()
        for job in jobs:
            rows.append(
                {
                    "title": job.title,
                    "url": job.url,
                    "company": job.company,
                    "location": job.location,
                    "description": job.description,
                    "source": job.source,
                    "external_id": job.external_id,
                    "content_hash": job.content_hash,
                    "score_stage_1": job.score_stage_1,
                    "archetype": job.archetype,
                    "company_domain": job.company_domain,
                    "scraped_at": (
                        job.posted_at.isoformat() if job.posted_at else now
                    ),
                    "last_seen_at": now,
                    "status": "active",
                    # Bundle-B additive columns
                    "location_normalized": job.location_normalized,
                    "location_lat": job.location_lat,
                    "location_lon": job.location_lon,
                    "is_remote": job.is_remote,
                    "is_hybrid": job.is_hybrid,
                    "dq_flags": job.dq_flags or {},
                }
            )

        results: list[bool] = []
        for row in rows:
            try:
                table = self._table
                await asyncio.to_thread(
                    lambda r=row, t=table: self.client.table(t)
                    .upsert(r, on_conflict="source,external_id")
                    .execute()
                )
                results.append(True)
            except Exception as exc:
                logger.error(
                    "upsert_job_failed",
                    extra={"url": row["url"][:80], "error": str(exc)},
                )
                results.append(False)
        return results

    async def mark_expired(self, threshold_days: int) -> int:
        """Mark jobs in the active shelf as 'expired' when last_seen_at is older than threshold_days.

        Does NOT delete rows — preserves history. Returns count of rows updated.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=threshold_days)).isoformat()
        table = self._table
        try:
            result = await asyncio.to_thread(
                lambda: self.client.table(table)
                .update({"status": "expired"})
                .lt("last_seen_at", cutoff)
                .eq("status", "active")
                .execute()
            )
            updated = len(result.data) if result.data else 0
            logger.info("mark_expired", extra={"threshold_days": threshold_days, "updated": updated})
            return updated
        except Exception as exc:
            logger.error("mark_expired_failed", extra={"error": str(exc)})
            return 0

    async def get_by_id(self, job_id: str) -> dict | None:
        """Get a single job by ID."""
        result = await asyncio.to_thread(
            lambda: self.client.table(self._table)
            .select("*")
            .eq("id", job_id)
            .execute()
        )
        return result.data[0] if result.data else None

    # --- Consumer-Agnostic Query API (Phase 3, AC-001 / AC-015-018) ---

    async def query(
        self,
        keywords_positive: list[str] | None = None,
        keywords_negative: list[str] | None = None,
        location: str | None = None,
        max_age_days: int | None = None,
        exclude_domain: list[str] | None = None,
        sort: str = "recency",
        limit: int = 50,
        offset: int = 0,
        # SHOULD params (AC-015-AC-018)
        source: list[str] | None = None,
        company_domain: list[str] | None = None,
        seniority: str | None = None,
        min_salary: int | None = None,
        max_salary: int | None = None,
        max_distance_km: int | None = None,
    ) -> tuple[list[dict], int]:
        """Consumer-agnostic job query with full filter support.

        Returns (rows, total_count).

        PostGIS is NOT installed; max_distance_km uses a SQL bounding-box
        prefilter on location_lat/location_lon and is refined with a
        Python-Haversine post-filter at the route layer.
        """
        q = self.client.table(self._table).select("*", count="exact")

        # -- MUST filters (AC-001) --

        # keywords_positive: keep rows where ANY keyword ilike-matches title OR description
        if keywords_positive:
            # Sanitize to prevent PostgREST operator injection
            def _safe(k: str) -> str:
                return k.replace(",", " ").replace("(", "").replace(")", "").replace(".", " ")

            or_parts = []
            for kw in keywords_positive:
                safe = _safe(kw)
                or_parts.append(f"title.ilike.%{safe}%")
                or_parts.append(f"description.ilike.%{safe}%")
            q = q.or_(",".join(or_parts))

        # keywords_negative: exclude rows matching any keyword in title OR description.
        # supabase-py v2 has no native NOT-OR — we apply each negative keyword
        # as its own not.or_ filter (AND logic between keywords).
        if keywords_negative:
            def _safe(k: str) -> str:  # noqa: F811
                return k.replace(",", " ").replace("(", "").replace(")", "").replace(".", " ")

            for kw in keywords_negative:
                safe = _safe(kw)
                q = q.not_.or_(f"title.ilike.%{safe}%,description.ilike.%{safe}%")

        # location: ILIKE match on `location` column
        if location:
            safe_loc = location.replace(",", " ").replace("(", "").replace(")", "")
            q = q.ilike("location", f"%{safe_loc}%")

        # max_age_days: scraped_at >= now() - interval
        if max_age_days is not None:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=max_age_days)
            ).isoformat()
            q = q.gte("scraped_at", cutoff)

        # exclude_domain: exclude rows where company_domain matches any entry
        if exclude_domain:
            for domain in exclude_domain:
                q = q.neq("company_domain", domain)

        # -- SHOULD filters (AC-015-AC-018) --

        # source: exact-match whitelist
        if source:
            q = q.in_("source", source)

        # company_domain: whitelist (contrast to exclude_domain)
        if company_domain:
            q = q.in_("company_domain", company_domain)

        # seniority: simple ILIKE heuristic on title
        if seniority:
            seniority_map = {
                "senior": ["senior", "sr.", "lead", "principal", "staff"],
                "junior": ["junior", "jr.", "entry", "trainee", "werkstudent"],
                "lead": ["lead", "principal", "staff", "head of"],
                "mid": ["mid", "medior", "intermediate"],
            }
            terms = seniority_map.get(seniority.lower(), [seniority])
            or_parts = [f"title.ilike.%{t}%" for t in terms]
            q = q.or_(",".join(or_parts))

        # salary: NULL-tolerant — only apply if column is not NULL
        if min_salary is not None:
            q = q.gte("salary_min", min_salary).not_.is_("salary_min", "null")
        if max_salary is not None:
            q = q.lte("salary_max", max_salary).not_.is_("salary_max", "null")

        # max_distance_km: SQL bounding-box prefilter (Haversine refinement happens at route layer).
        # Excludes rows with NULL location_lat/lon since they cannot satisfy a distance query.
        bbox_coords: tuple[float, float] | None = None
        if max_distance_km is not None and location is not None:
            bbox_coords = _geocode_city(location)
            if bbox_coords is not None:
                lat0, lon0 = bbox_coords
                delta_lat = max_distance_km / 111.0
                # Longitude degrees shrink with latitude; guard against lat=±90.
                cos_lat = max(0.01, math.cos(math.radians(lat0)))
                delta_lon = max_distance_km / (111.0 * cos_lat)
                q = (
                    q.gte("location_lat", lat0 - delta_lat)
                    .lte("location_lat", lat0 + delta_lat)
                    .gte("location_lon", lon0 - delta_lon)
                    .lte("location_lon", lon0 + delta_lon)
                )

        # -- Sort --
        if sort == "score_keyword":
            # NULL-last for score_stage_1
            q = q.order("score_stage_1", desc=True, nulls_first=False)
        else:
            # default: recency
            q = q.order("scraped_at", desc=True)

        # -- Pagination --
        q = q.limit(limit).offset(offset)

        res = await asyncio.to_thread(q.execute)
        rows: list[dict] = res.data or []
        total: int = res.count if res.count is not None else len(rows)

        logger.info(
            "jobs_query",
            extra={
                "keywords_positive": keywords_positive,
                "keywords_negative": keywords_negative,
                "location": location,
                "max_age_days": max_age_days,
                "sort": sort,
                "limit": limit,
                "offset": offset,
                "result_count": len(rows),
                "total": total,
            },
        )
        return rows, total


# ---------------------------------------------------------------------------
# Haversine helpers (PostGIS unavailable — pure-Python fallback)
# ---------------------------------------------------------------------------


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in km between two (lat, lon) points."""
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


# Hardcoded top-40 DE cities dict for geocoding location param.
# Kept here for now; consolidation into LocationNormalizer is tracked as a P1 follow-up.
_DE_CITIES: dict[str, tuple[float, float]] = {
    "berlin": (52.5200, 13.4050),
    "hamburg": (53.5753, 10.0153),
    "münchen": (48.1351, 11.5820),
    "munich": (48.1351, 11.5820),
    "köln": (50.9333, 6.9500),
    "cologne": (50.9333, 6.9500),
    "frankfurt": (50.1109, 8.6821),
    "stuttgart": (48.7758, 9.1829),
    "düsseldorf": (51.2217, 6.7762),
    "dortmund": (51.5136, 7.4653),
    "essen": (51.4556, 7.0116),
    "bremen": (53.0793, 8.8017),
    "leipzig": (51.3397, 12.3731),
    "dresden": (51.0504, 13.7373),
    "hannover": (52.3759, 9.7320),
    "nürnberg": (49.4521, 11.0767),
    "nuremberg": (49.4521, 11.0767),
    "duisburg": (51.4344, 6.7623),
    "bochum": (51.4818, 7.2162),
    "wuppertal": (51.2562, 7.1508),
    "mannheim": (49.4875, 8.4660),
    "bonn": (50.7374, 7.0982),
    "karlsruhe": (49.0069, 8.4037),
    "münster": (51.9607, 7.6261),
    "augsburg": (48.3705, 10.8978),
    "wiesbaden": (50.0782, 8.2398),
    "gelsenkirchen": (51.5177, 7.0857),
    "mönchengladbach": (51.1805, 6.4428),
    "braunschweig": (52.2689, 10.5268),
    "kiel": (54.3233, 10.1394),
    "aachen": (50.7753, 6.0839),
    "magdeburg": (52.1205, 11.6276),
    "freiburg": (47.9990, 7.8421),
    "oberhausen": (51.4963, 6.8638),
    "erfurt": (50.9848, 11.0299),
    "rostock": (54.0887, 12.1400),
    "mainz": (49.9929, 8.2473),
    "kassel": (51.3127, 9.4797),
    "halle": (51.4828, 11.9697),
    "heidelberg": (49.3988, 8.6724),
}


def _geocode_city(location: str) -> tuple[float, float] | None:
    """Return (lat, lon) for a German city name, or None if not found."""
    key = location.strip().lower()
    if key in _DE_CITIES:
        return _DE_CITIES[key]
    for city_name, coords in _DE_CITIES.items():
        if city_name in key or key in city_name:
            return coords
    return None
