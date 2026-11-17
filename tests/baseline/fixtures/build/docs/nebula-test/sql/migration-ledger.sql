-- @meta
-- title: Nebula ledger migration
-- project: nebula-test
-- domain: Synthetic laboratory
-- topic: Nebula Dispatch
-- type: sql
-- status: pending
-- date: 2040-01-04
-- summary: A harmless SQL download for a fictional ledger.
-- related: nebula-test/api/dispatch-guide-v2
-- @end

-- NEBULA_SQL_FIXTURE: this script is never executed against a database.
CREATE TABLE nebula_fixture_ledger (id INTEGER PRIMARY KEY, signal TEXT NOT NULL);
