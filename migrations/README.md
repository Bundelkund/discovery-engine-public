# Bundle B Schema Migration

## What this migration does

Adds 6 new columns to the `jobs` table (additive-only — no DROP/RENAME/ALTER TYPE):

| Column | Type | Default |
|--------|------|---------|
| `location_normalized` | `text NULL` | — |
| `location_lat` | `double precision NULL` | — |
| `location_lon` | `double precision NULL` | — |
| `is_remote` | `boolean NOT NULL` | `false` |
| `is_hybrid` | `boolean NOT NULL` | `false` |
| `dq_flags` | `jsonb NOT NULL` | `'{}'::jsonb` |

Also creates three partial indexes for location and remote filtering performance.

## Why deferred execution

At Phase 4 execution time, no direct Postgres URL (`SUPABASE_DB_URL` / `DATABASE_URL`)
was available — only REST credentials (`SUPABASE_URL` + `SUPABASE_KEY`). The
`exec_sql` RPC does not exist on this Supabase project. DDL must be run via one of
the methods below.

## How to run

### Option A: Supabase Dashboard (recommended)

1. Log in to [app.supabase.com](https://app.supabase.com)
2. Select your Supabase project
3. Navigate to **SQL Editor** → **New query**
4. Paste the contents of `bundle-b-additive.sql` and click **Run**
5. Verify success: `SELECT column_name FROM information_schema.columns WHERE table_name = 'jobs' ORDER BY ordinal_position;`
   — should include `location_normalized`, `location_lat`, `location_lon`, `is_remote`, `is_hybrid`, `dq_flags`

### Option B: psql via SUPABASE_DB_URL

```bash
# Requires: SUPABASE_DB_URL set in environment (postgres://... connection string)
psql "$SUPABASE_DB_URL" -f migrations/bundle-b-additive.sql
```

### Option C: psql direct (Supabase connection pooler)

```bash
# Get connection details from Supabase Dashboard → Settings → Database
psql "postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres" \
  -f migrations/bundle-b-additive.sql
```

## Idempotency

All statements use `IF NOT EXISTS` — safe to run multiple times. Running on an
already-migrated database is a no-op.

## Rollback (emergency only)

See `DE-B-FOLLOWUP-02` task. In an emergency, the reverse is:

```sql
ALTER TABLE jobs
  DROP COLUMN IF EXISTS location_normalized,
  DROP COLUMN IF EXISTS location_lat,
  DROP COLUMN IF EXISTS location_lon,
  DROP COLUMN IF EXISTS is_remote,
  DROP COLUMN IF EXISTS is_hybrid,
  DROP COLUMN IF EXISTS dq_flags;
```

Do NOT run rollback if any downstream consumer is already writing these columns.
