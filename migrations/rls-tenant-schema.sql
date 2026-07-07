-- AUDIT-P0-03 — Enable RLS on the tenant.* schema (supabase-schema-audit 2026-07-05).
--
-- RLS was disabled on all five tenant.* tables. Live verification 2026-07-07 (main
-- session) confirmed they are NOT anon-reachable today: anon/authenticated hold ZERO
-- table grants on the tenant schema, and the schema is not exposed via PostgREST
-- (the security advisor does not even flag it). So this is a defense-in-depth gap, not
-- an open door — one accidental GRANT would expose everything with no RLS to catch it.
--
--   tenant.tenant_keys, tenant.profiles, tenant.applications,
--   tenant.matches, tenant.search_terms
--
-- The only writer is tenant-module, which connects with the service_role key
-- (verified: SUPABASE_KEY JWT role=service_role, ref=guocdgjpbvsvcvchgolm). service_role
-- BYPASSES RLS, so enabling RLS with no policy denies anon/authenticated without touching
-- the module. Mirrors the rls-internal-tables.sql / rls-new-tables.sql decision.
--
-- Reversible: `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` if a non-service reader surfaces.

ALTER TABLE tenant.tenant_keys   ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.profiles      ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.applications  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.matches       ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.search_terms  ENABLE ROW LEVEL SECURITY;
