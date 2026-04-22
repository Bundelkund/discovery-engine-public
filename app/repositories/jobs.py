import logging
from datetime import datetime, timedelta, timezone

from app.models.job import ScoredJob
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class JobRepository(BaseRepository):
    TABLE = "jobs"

    async def insert_batch(self, jobs: list[ScoredJob], profile_id: str) -> int:
        if not jobs:
            return 0
        rows = []
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
                    "score_stage_2": job.score_stage_2,
                    "archetype": job.archetype,
                    "company_domain": job.company_domain,
                    "profile_id": profile_id,
                    "scraped_at": job.posted_at.isoformat() if job.posted_at else None,
                }
            )
        inserted = 0
        for row in rows:
            try:
                self.client.table(self.TABLE).insert(row).execute()
                inserted += 1
            except Exception as e:
                if "23505" in str(e):
                    logger.debug(f"Duplicate skipped: {row['url'][:60]}")
                else:
                    logger.error(f"Failed to insert job: {e}")
        return inserted

    async def update_stage1_score(
        self, job_url: str, score_stage_1: int, archetype: str = None,
        profile_id: str = None
    ) -> None:
        data = {"score_stage_1": score_stage_1}
        if archetype:
            data["archetype"] = archetype
        if profile_id:
            data["profile_id"] = profile_id
        self.client.table(self.TABLE).update(data).eq("url", job_url).execute()

    async def update_scores(
        self, job_url: str, score_stage_2: float
    ) -> None:
        self.client.table(self.TABLE).update(
            {"score_stage_2": score_stage_2}
        ).eq("url", job_url).execute()

    async def update_stage3_score(
        self,
        job_url: str,
        score_stage_3: float,
        match_reasoning: str = None,
        match_highlights: list[str] = None,
        match_pitch: str = None,
    ) -> None:
        data = {"score_stage_3": score_stage_3}
        if match_reasoning:
            data["match_reasoning"] = match_reasoning
        if match_highlights:
            data["match_highlights"] = match_highlights
        if match_pitch:
            data["match_pitch"] = match_pitch
        self.client.table(self.TABLE).update(data).eq("url", job_url).execute()

    async def get_unscored(
        self, profile_id: str, source: str = None, limit: int = 500
    ) -> list[dict]:
        """Get all unscored jobs — both profile-owned AND legacy (profile_id IS NULL)."""
        query = (
            self.client.table(self.TABLE)
            .select("*")
            .is_("score_stage_1", "null")
            .or_(f"profile_id.eq.{profile_id},profile_id.is.null")
        )
        if source:
            query = query.eq("source", source)
        result = query.limit(limit).execute()
        return result.data or []

    async def get_needs_rescore(
        self,
        profile_id: str,
        stage1_min: int = 50,
        source: str = None,
        limit: int = 500,
    ) -> list[dict]:
        """Get jobs with stage_1 >= stage1_min that still need stage_2 or stage_3.

        Used by the rescore path to upgrade jobs that were scored before the
        Stage 2/3 pipeline was wired up.
        """
        query = (
            self.client.table(self.TABLE)
            .select("*")
            .gte("score_stage_1", stage1_min)
            .or_("score_stage_2.is.null,score_stage_3.is.null")
            .or_(f"profile_id.eq.{profile_id},profile_id.is.null")
        )
        if source:
            query = query.eq("source", source)
        result = query.order("score_stage_1", desc=True).limit(limit).execute()
        return result.data or []

    # --- WA Provider API Methods ---

    async def list_jobs(
        self,
        profile_id: str,
        page: int = 1,
        page_size: int = 20,
        sort: str = "final_score",
        sort_dir: str = "desc",
        search: str = None,
        source: str = None,
        score_min: float = None,
        archetype: str = None,
    ) -> list[dict]:
        """List jobs with scores, paginated and filterable."""
        query = (
            self.client.table(self.TABLE)
            .select("*")
            .or_(f"profile_id.eq.{profile_id},profile_id.is.null")
        )
        if source:
            query = query.eq("source", source)
        if archetype:
            query = query.eq("archetype", archetype)
        if score_min is not None:
            query = query.gte("score_stage_1", score_min)
        if search:
            # Sanitize: strip PostgREST operators to prevent filter injection
            safe_search = search.replace(",", " ").replace(".", " ").replace("(", "").replace(")", "")
            pattern = f"%{safe_search}%"
            query = query.or_(
                f"title.ilike.{pattern},"
                f"company.ilike.{pattern},"
                f"description.ilike.{pattern}"
            )

        # Sorting: score_stage_3 > stage_2 > stage_1 for final_score
        sort_column = {
            "final_score": "score_stage_3",
            "scraped_at": "scraped_at",
            "company": "company",
        }.get(sort, "score_stage_3")
        desc = sort_dir == "desc"
        query = query.order(sort_column, desc=desc)

        # Pagination
        offset = (page - 1) * page_size
        query = query.range(offset, offset + page_size - 1)

        result = query.execute()
        return result.data or []

    async def count_jobs(
        self,
        profile_id: str,
        search: str = None,
        source: str = None,
        score_min: float = None,
        archetype: str = None,
    ) -> int:
        """Count jobs matching filters (for pagination total)."""
        query = (
            self.client.table(self.TABLE)
            .select("id", count="exact")
            .or_(f"profile_id.eq.{profile_id},profile_id.is.null")
        )
        if source:
            query = query.eq("source", source)
        if archetype:
            query = query.eq("archetype", archetype)
        if score_min is not None:
            query = query.gte("score_stage_1", score_min)
        if search:
            safe_search = search.replace(",", " ").replace(".", " ").replace("(", "").replace(")", "")
            pattern = f"%{safe_search}%"
            query = query.or_(
                f"title.ilike.{pattern},"
                f"company.ilike.{pattern},"
                f"description.ilike.{pattern}"
            )
        result = query.execute()
        return result.count if result.count is not None else 0

    async def get_by_id(self, job_id: str) -> dict | None:
        """Get a single job by ID."""
        result = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("id", job_id)
            .execute()
        )
        return result.data[0] if result.data else None

    # --- Consumer-Agnostic Query API (Phase 3, AC-001 / AC-015-018) ---

    def query(
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

        Location filter uses `location` column only pre-migration.
        Post-migration (Worker-D Phase 4) adds location_normalized — the
        coalesce pattern should be applied here once columns exist.

        PostGIS is NOT installed; max_distance_km uses Python-Haversine
        post-query filter on location_lat/location_lon (added in Phase 4
        migration).  Pre-migration, rows lack these columns and the filter
        is skipped gracefully via getattr with None fallback.
        """
        q = self.client.table(self.TABLE).select("*", count="exact")

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

        # keywords_negative: exclude rows matching any keyword in title OR description
        # Supabase-py v2 does not natively support NOT-OR in one call.
        # Workaround: apply each negative keyword as a separate not().ilike() filter
        # (AND logic — row excluded if it matches ANY single keyword twice is fine
        # because we call .or_ with not on each individually).
        # Using the postgrest-py filter `not.ilike` via .filter():
        if keywords_negative:
            def _safe(k: str) -> str:  # noqa: F811
                return k.replace(",", " ").replace("(", "").replace(")", "").replace(".", " ")

            for kw in keywords_negative:
                safe = _safe(kw)
                # Exclude rows where title OR description matches — use not.or_
                q = q.not_.or_(f"title.ilike.%{safe}%,description.ilike.%{safe}%")

        # location: ILIKE match on `location` column
        # NOTE: post-migration, also match `location_normalized` via coalesce — apply then.
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

        # -- Sort --
        if sort == "score_keyword":
            # NULL-last for score_stage_1
            q = q.order("score_stage_1", desc=True, nulls_first=False)
        else:
            # default: recency
            q = q.order("scraped_at", desc=True)

        # -- Pagination --
        q = q.limit(limit).offset(offset)

        res = q.execute()
        rows: list[dict] = res.data or []
        total: int = res.count if res.count is not None else len(rows)

        # -- max_distance_km: Python-Haversine post-query filter --
        # PostGIS is NOT installed.  location_lat/location_lon are Phase-4
        # migration columns — access via getattr with None fallback.
        if max_distance_km is not None and location is not None:
            coords = _geocode_city(location)
            if coords is None:
                logger.warning(
                    "max_distance_km_skipped",
                    extra={"reason": "location_not_geocodable", "location": location},
                )
            else:
                lat0, lon0 = coords
                filtered = []
                for row in rows:
                    row_lat = row.get("location_lat") if isinstance(row, dict) else getattr(row, "location_lat", None)
                    row_lon = row.get("location_lon") if isinstance(row, dict) else getattr(row, "location_lon", None)
                    if row_lat is None or row_lon is None:
                        # Column absent (pre-migration) — include row by default
                        filtered.append(row)
                    else:
                        dist = _haversine_km(lat0, lon0, float(row_lat), float(row_lon))
                        if dist <= max_distance_km:
                            filtered.append(row)
                total = len(filtered)
                rows = filtered

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

import math  # noqa: E402  (kept close to usage for locality)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in km between two (lat, lon) points."""
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


# Hardcoded top-20 DE cities dict for geocoding location param
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
    # Try exact match first
    if key in _DE_CITIES:
        return _DE_CITIES[key]
    # Try substring match
    for city_name, coords in _DE_CITIES.items():
        if city_name in key or key in city_name:
            return coords
    return None
