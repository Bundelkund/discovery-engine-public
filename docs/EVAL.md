# Scoring Eval

> The engine is **profile-agnostic** (since the tenant-module decoupling, 2026-06-09). It ships
> no committed scoring profile: `load_scoring_profile()` returns `None`, the refine pipeline runs
> with an empty profile (title-gate disabled → keep all, score 0), and `store_threshold=0` stores
> everything that survives dedup + DQ. There is no per-profile accuracy to eval here.

**Per-profile scoring and its goldset eval moved to the tenant module** — see
`tenant-module/docs/EVAL.md` and `tenant-module/tests/test_match_goldset.py`.

The generic Stage-1 keyword scorer (`app/scoring/keyword.py`, `config/archetypes.yaml`) is kept
as a no-op-on-empty-profile mechanism for any deploy that opts into engine-side scoring via a
gitignored `config/scoring-profile.local.yaml` (resolution order in `app/config.py::load_scoring_profile`).

Removed 2026-06-09 with the florian engine goldset: `config/scoring-profile.yaml` (id "florian"),
`tests/test_scoring_goldset.py`, `tests/fixtures/scoring_goldset.csv`,
`tests/fixtures/scoring_goldset_runs/`, `scripts/compare_scoring_runs.py`.
