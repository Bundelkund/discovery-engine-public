# Feasibility Study: `sources`-Dimension Schema-Redesign

> Proposal: `HANDOVER-de-filter-fix.md` lines 111–164. Run: 2026-06-11.
> Scope: single-system (discovery-engine + Supabase `guocdgjpbvsvcvchgolm`). Dim 2 + Dim 5 (cross-system) skipped per triage.

## Verdict: **GO WITH CONDITIONS**

Cascade is small and well-bounded (3 files, ~8 DB-coupled lines). `feed_url` is effectively write-only → drop is low-risk. The one real hazard is the `on_conflict` unique key on the rename. Worth doing for the `type` payoff — but ADR-first and same-deploy atomic.

---

## Dim 1 — Schema Impact: **MEDIUM** (not CRITICAL)

Layers that cascade: DB column → supabase-py / REST query-builder → enumeration-JSON contract. ~2–3 layers, **not** 4+.

**`ats` → `source_code` rename — every DB-column-coupled site:**

| File:Line | Use | Action |
|---|---|---|
| `seed_ats_companies.py:65` | `"ats": ats` row write | rename key → `"source_code"` |
| `seed_ats_companies.py:86` | REST filter `ats=eq.{ats}` | `source_code=eq.{ats}` |
| `seed_ats_companies.py:104` | **`on_conflict: "ats,slug"`** | `"source_code,slug"` ← **hazard, see Conditions** |
| `db_slugs.py:41` | `.eq("ats", ats)` (Fix B) | `.eq("source_code", ats)` |
| `scan.py:55` | `.select("ats,slug")` | `select("source_code,slug")` |
| `scan.py:57,152` | `.eq("ats", …)` | rename |
| `scan.py:66` | `r["ats"]` grouping dict | `r["source_code"]` |

**NOT the DB column (leave alone):** `seed:114` `d["ats"]` + `ats_scanner.py:531` `"ats": ats` = enumeration-JSON key. The JSON is a separate contract; `row_from()` maps JSON→row. Keep JSON key `ats`, remap inside `row_from`. Decouples the two renames.

**`feed_url` drop — effectively write-only:**
- Only DB write = `seed_ats_companies.py:67`. **Zero** `.select("feed_url")` / `["feed_url"]` from any supabase query in `app/`. App never reads it back.
- Already template-derived at scan time: `prov["feed"].format(slug=s)` (`ats_scanner.py:516`), `FEED_URL.format(slug=s)` (`enumerate_personio.py:308`). The template the proposal wants to centralize **already exists in code** → "all derivable" assumption confirmed by construction, not hope.
- Soft consumer: `sheets_load_ats.py:80` `c.get("feed_url")` → Google-Sheet mirror column goes blank. Cosmetic; render template if the sheet still matters.

→ feed_url drop severity = **LOW**.

## Dim 4 — Performance: **PASS**
- `sources` lookup: ~18 rows, joined only against `ats_companies` (8.467 rows, low-churn) at seed/scan time — not on the hot insert path.
- Kimball call correct: NO FK on `jobs_v2`/`raw_jobs` → no `FOR KEY SHARE` lock per insert. Proposal already respects this.
- Net storage delta ≈ 0 (proposal honest: this is NOT a cap fix).

## Dim 6 — Effort vs Value: **PASS (conditional)**
- Effort: 1 migration + 3 file edits + ADR ≈ half a day.
- Value: the rename alone is cosmetic. **Real payoff = the `type` column** → DE-filter/monitor logic branches on `type IN ('ats','aggregator')` instead of hard-matching source strings. Today that distinction exists only implicitly in separate source modules (arbeitsagentur/jooble/themuse vs greenhouse/ashby/lever) — confirmed: **no `type`/`source_type`/`category` column exists anywhere today**.
- → Only worth it if `type` gets wired into real branching (Condition 5). Rename-for-rename alone = skip.

## Dim 7 — Scope Creep: **PASS**
- The triggering problem (DB > cap) is already SOLVED (P1 retention live, DB 419 MB). This is clean follow-up debt, not scope creep on an unfinished foundation.
- Carve-out correct: ESCO `profession_id` explicitly out (separate project). Good discipline.

## Dim 8 — Alternatives
1. **CHECK-only, no table** — add `type text CHECK (type IN ('ats','aggregator'))` directly on `ats_companies`, skip `sources` table + rename. Gets the #2 payoff for ~1h, zero rename cascade. Loses #4 (templates stay in code — but they already work in code). **Lightest path if #2 is the only real want.**
2. **Full proposal** (table + rename + FK + feed_url drop) — gets #2+#4+#5, costs the rename cascade + ADR.
3. **Do nothing** — naming debt persists; aggregator branching stays string-matched.

→ If you only care about clean aggregator branching: **Alt 1**. If you want the canonical term + template home too: **Alt 2** under conditions.

---

## Conditions (for GO on full proposal)

1. **ADR first.** Hard-to-reverse (rename + FK). 1 paragraph per CLAUDE.md 3-condition rule.
2. **Same-deploy atomic on the rename.** `on_conflict: "ats,slug"` (seed:104) is the upsert unique key. Migration renames column AND its unique constraint; `seed.py:104` updates to `source_code,slug` in the **same commit/deploy**. Migration-only or code-only ship → seed upsert targets nonexistent column → broken seed. (Solo dev + single deploy → acceptable; just don't split.)
3. **feed_url drop = separate later step.** Drop only after rename lands clean. Pre-drop: confirm the sheet mirror is dead or render the template into it. App-side already safe (write-only).
4. **Populate `sources` from real values, don't invent.** Enumerate distinct `source` actually present in `jobs_v2` + the 8 ats providers before writing the ~18 rows. The "~10 aggregators" is an estimate — verify against live data.
5. **Wire `type` into branching or don't bother.** The justification is cleaner monitor/DE logic. Land at least one real branch (aggregators skip slug/company-feed gating) — ties directly into **P5 (null-Sitemap reclassify)**. Do P5 + this together; the `type` split makes P5 stop hard-matching source strings.

## Recommendation
Take **Alt 1 (CHECK-only `type`)** now if the goal is the aggregator branching for P5 — 1h, no cascade, no ADR. Defer the full `sources` table + rename + feed_url drop to when the naming/template debt actually bites. Full proposal stays a valid GO-with-conditions backlog item, not urgent.
