-- AUDIT-P0-01 — Lock down company_profiles write access
-- (supabase-schema-audit 2026-07-05; live advisor 2026-07-07 confirmed still open).
--
-- The `company_profiles_write` policy was `FOR ALL ... USING(true) WITH CHECK(true)` for
-- ALL roles, and anon/authenticated additionally held base INSERT/UPDATE/DELETE grants —
-- i.e. anyone with the anon key could overwrite or DELETE all 1046 enrichment rows
-- (advisor: rls_policy_always_true).
--
-- Writer audit 2026-07-07: the only writer is discovery-engine
-- (app/repositories/companies.py CompanyRepository.upsert), which connects with the
-- service_role key (verified role=service_role). service_role BYPASSES RLS, so removing
-- the permissive write policy + the anon/authenticated write grants leaves DE writes
-- intact while denying anon writes/deletes.
--
-- READ is intentionally left open: company_profiles holds low-sensitivity company data
-- (name, size, industry, location, linkedin) and no permissive-SELECT advisor finding
-- exists. The `company_profiles_read` (SELECT USING(true)) policy is kept so any reader
-- is unaffected. A service_role write policy is intentionally NOT recreated: it would be
-- redundant (service_role bypasses RLS) and a USING(true) replacement would re-trigger
-- the same advisor. This mirrors the "RLS-on + no write policy + service_role bypass"
-- pattern used across this DB.
--
-- Reversible: recreate `company_profiles_write` and re-GRANT the write privileges.

DROP POLICY IF EXISTS company_profiles_write ON public.company_profiles;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON public.company_profiles FROM anon, authenticated;
