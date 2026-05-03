# Discovery-Engine: Handoff & Publication Readiness

Date: 2026-05-03
Repo: `C:/Users/Konektos/github/discovery-engine`
Remote: `git@github.com:Bundelkund/discovery-engine.git` (currently private)
Tracked files: 97
Recent prep: `ba25a79 chore: redact internal infra refs before public release`, `a08497d chore(public): replace portals.yaml with demo + add .local override`

---

## 1. Verdict

| Audience | Verdict |
|---|---|
| **External developer handoff** | READY-WITH-FIXES |
| **Public publication (open-source on GitHub)** | NOT-READY |

The repo has been actively prepared for public release (two recent "public/redact" commits) and the secret hygiene is genuinely solid — no secret has ever been in Git history. But three real blockers remain for publication: missing LICENSE, hardcoded internal Windows path in a tracked script, and `.env.example` does not match the code's actual env var surface. Once those are fixed (≈30 min of work), this is publishable.

---

## 2. Blockers (must fix before public publication)

### B1. No LICENSE file — repo legally cannot be reused
- **Evidence**: `ls LICENSE*` → no match. `pyproject.toml` has no `license = ...` field.
- **Impact**: Without a license, default copyright applies — nobody (including external devs you handed it to) has the legal right to use, modify, or redistribute the code. GitHub will publish it but no one can fork it productively.
- **Fix**: Add `LICENSE` (MIT or Apache-2.0 are the usual choices for FastAPI service code) and add `license = "MIT"` (or matching SPDX) to `pyproject.toml`.

### B2. Hardcoded internal Windows path in tracked script
- **Evidence**: `scripts/verify_wa_prereq.sh:15` —
  ```
  WA_BACKEND_PATH="${WA_BACKEND_PATH:-C:/Users/Konektos/github/wonderapply/backend/app}"
  ```
- **Impact**: Leaks Florian's local machine layout AND username in a public repo; on any other machine the default fails. Script comment on line 13 even claims "no hardcoded absolute paths in docs" — contradicts the line below.
- **Fix**: Default to a relative sibling path (e.g. `../wonderapply/backend/app`) or strip the default entirely so the script requires `WA_BACKEND_PATH` to be set explicitly. Even better for public release: move `verify_wa_prereq.sh` out of the public repo (it is a WonderApply-specific cross-repo CI gate, not part of discovery-engine's own contract).

### B3. `.env.example` does not match the code's actual env surface
- **Evidence**: `.env.example` contains only `SUPABASE_URL`, `SUPABASE_KEY`, `WA_API_KEY`, `JH_API_KEY`, `HUNTER_API_KEY`. Missing:
  - `OPENAI_API_KEY` — README line 28 mentions it for Stage 2 scoring, real `.env` line 11 has a populated `sk-proj-...` value.
  - `DE_API_KEY` — `app/config.py:17` defines it as a settings field, real `.env` line 7 has it.
- **Impact**: A dev follows the README "cp .env.example .env, fill in keys" path and hits surprise runtime failures. Onboarding broken.
- **Fix**: Sync `.env.example` to match every env var read by `app/config.py` and `config/api-keys.yaml`'s `key_env` references. Add a one-line comment per var explaining purpose and where to obtain it.

---

## 3. Recommended before handoff

### R1. No CLAUDE.md
- `.claude/` directory exists but only contains `skills/build-with-agent-team` — no `CLAUDE.md` at root or in `.claude/`. An external developer onboarding via Claude Code has zero project conventions/build commands. (Per repo-handoff Phase 1.2 this is a Phase 1 FAIL.)
- **Fix**: Add a short `CLAUDE.md` (or root `CLAUDE.md`) covering: Python 3.12 / `pip install -e .[dev]` / `uvicorn app.main:app --port 8091` / `pytest` / project-specific patterns (registry decorators, repository layer, YAML configs).

### R2. `.understand-anything/` not in `.gitignore`
- **Evidence**: `git status` shows `?? .understand-anything/` (untracked). It contains a 5000+ line `knowledge-graph.json` which references internal context (WonderApply backend path on line 2232).
- **Impact**: A future `git add -A` pulls it in. On public release this leaks internal architecture commentary.
- **Fix**: Add `.understand-anything/` to `.gitignore` (already excludes `.claude/`, `.specs/`, `.refs/`, `.deepeval/`, `.worktrees/`, `.plan/`).

### R3. Repo author email visibility
- **Evidence**: Commit history contains `fl.rister@posteo.de` and `florian@konektos.de` (verified via `git log --pretty=format:"%ae" | sort -u`).
- **Impact**: For pure handoff: no problem. For public publication: the `posteo.de` address ends up indexed and scrapable. Florian's call — many maintainers ship under their real email.
- **Fix (optional)**: Decide which identity is the public face. If switching, GitHub's `users.noreply.github.com` is the standard pattern; existing history can stay or be rewritten with `git filter-repo` (destructive).

### R4. `migrations/README.md` references shared Supabase project
- **Evidence**: README line 14 — `Storage: Supabase (shared DB with WonderApply + JobHunt)`. `migrations/README.md` line 70 — `Do NOT run rollback if JobHunt or WonderApply is already writing these columns.`
- **Impact**: For public release this reads as "this code was extracted from a private monorepo; some of its docs assume sister services exist". Not a security blocker, but signals incomplete extraction.
- **Fix**: For publication, soften the language: `Storage: Supabase` and remove the JobHunt/WonderApply rollback caveat or generalize it.

---

## 4. Nice-to-have

- `pyproject.toml` lacks `description`, `authors`, `readme`, `license`, `urls.repository` — fill these in for a professional PyPI-presentable package even if not published to PyPI.
- No `CONTRIBUTING.md` or `CODE_OF_CONDUCT.md` (only matters if accepting external PRs).
- `Dockerfile` does not pin a base-image digest and runs as root. For a public reference example consider adding a non-root `USER` line and pinning `python:3.12-slim@sha256:...`.
- `tests/test_auth_per_consumer.py` references `wonderapply` and `jobhunt` as fixture IDs — fine (they are valid demo consumer IDs), but signals the original use-case if you want a fully neutral example.
- `.deepeval/`, `.refs/`, `.specs/` directories exist locally but are gitignored — fine.

---

## 5. Evidence appendix

### Secrets scan results (all CLEAN)

| Check | Result |
|---|---|
| `.env` ever committed (`git log --all --full-history -- .env`) | Never |
| Supabase project ID `guocdgjpbvsvcvchgolm` ever in git (`git log -p -S`) | Never |
| OpenAI key prefix `sk-proj-` ever in git | Never |
| Hunter API key `48dd3d25...` ever in git | Never |
| Tracked files matching `\.env$|secrets|credentials|\.local\.|\.key$` | None |
| Hardcoded `sk-`, `eyJ`, `AIza`, `ghp_`, `Bearer …`, `AKIA` in source | None |
| Hardcoded `password=`/`secret=`/`api_key="..."` patterns | None |

### Live secrets in local `.env` (NOT committed — present on Florian's machine only)

`C:/Users/Konektos/github/discovery-engine/.env` lines 3, 4, 5, 6, 8, 11 contain:
- `SUPABASE_URL=https://guocdgjpbvsvcvchgolm.supabase.co`
- `SUPABASE_KEY=eyJ...service_role JWT (exp 2065)...`
- `WA_API_KEY=33342...wa-dev` (looks like dev placeholder, but rotate before sharing)
- `JH_API_KEY=54359966ß...jh-dev` (same)
- `HUNTER_API_KEY=48dd3d25dab7b7453cddebe2547f64cf657f0925`
- `OPENAI_API_KEY=sk-proj-Ic3nfk41...`

These are SAFE in the sense they are gitignored. For a fresh developer who will need credentials, hand these over via 1Password / Bitwarden / direct DM — never via the repo. **Do rotate the OpenAI and Supabase service-role key before granting Git access** to anyone outside Florian, since they will end up in the new collaborator's local `.env` regardless of how careful Git hygiene is.

### Onboarding checklist (per `.claude/skills/repo-handoff/SKILL.md`)

| Check | Status | Detail |
|---|---|---|
| README.md exists, not boilerplate | PASS | 92 lines, real architecture + endpoint docs |
| Setup section with clone/install/run | PASS | README §Setup: `pip install -e .` / `uvicorn app.main:app --port 8091` |
| `.env.example` exists | WARN | Exists but missing `OPENAI_API_KEY`, `DE_API_KEY` (see B3) |
| `.gitignore` excludes `.env`, secrets, `*.local.*` | PASS | Lines 4, 17, 18 of `.gitignore` |
| CLAUDE.md present | FAIL | Missing (see R1) |
| LICENSE | FAIL | Missing (see B1) |
| Build & distribution docs | PASS | Dockerfile + docker-compose.yml + README §Docker |
| Design docs reachable | PASS | `docs/SCORING.md` + `migrations/README.md` |
| No orchestration artifacts (HANDOVER-*, REVIEW-*, sprint-*) in repo root | PASS | None tracked |
| `.specs/`, `.plan/` gitignored if private | PASS | Both gitignored |

### Files referenced in this audit

- `C:/Users/Konektos/github/discovery-engine/.gitignore`
- `C:/Users/Konektos/github/discovery-engine/.env.example`
- `C:/Users/Konektos/github/discovery-engine/.env` (local only, gitignored)
- `C:/Users/Konektos/github/discovery-engine/README.md`
- `C:/Users/Konektos/github/discovery-engine/pyproject.toml`
- `C:/Users/Konektos/github/discovery-engine/Dockerfile`
- `C:/Users/Konektos/github/discovery-engine/docker-compose.yml`
- `C:/Users/Konektos/github/discovery-engine/app/config.py:13-23`
- `C:/Users/Konektos/github/discovery-engine/config/api-keys.yaml`
- `C:/Users/Konektos/github/discovery-engine/config/portals.yaml` (clean demo, public-safe)
- `C:/Users/Konektos/github/discovery-engine/scripts/verify_wa_prereq.sh:15` (hardcoded path)
- `C:/Users/Konektos/github/discovery-engine/migrations/bundle-b-additive.sql` (clean — no project IDs)
- `C:/Users/Konektos/github/discovery-engine/migrations/README.md:14, 70` (mentions sister services)
- `C:/Users/Konektos/github/discovery-engine/docs/SCORING.md`

### Recommended next skills

After applying B1–B3 fixes:
- `/security-audit` — full secrets sweep (this report did a quick-check only)
- `/cto-architect-review` — repo hygiene at depth (Lean Six Sigma)
