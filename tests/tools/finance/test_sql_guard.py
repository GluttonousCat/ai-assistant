"""Reject writes hidden inside otherwise plausible read-only queries."""
import pytest

from tools.finance.sql_guard import validate_sql


@pytest.mark.parametrize("sql", [
    "SELECT 1; DELETE FROM fin.income",
    "WITH removed AS (DELETE FROM fin.income RETURNING *) SELECT * FROM removed",
    "SELECT * INTO fin.copied_income FROM fin.income",
    "SELECT * FROM fin.income FOR UPDATE",
    "WITH users AS (SELECT 1) SELECT * FROM auth.users",
    "SELECT * FROM other_db.fin.income",
])
def test_rejects_hidden_mutation_and_schema_bypass(sql):
    assert not validate_sql(sql).valid


@pytest.mark.parametrize("sql", [
    "SELECT * FROM fin.income LIMIT 10",
    "WITH latest AS (SELECT * FROM fin.income) SELECT * FROM latest",
    "SELECT 1 UNION ALL SELECT 2",
])
def test_read_only_queries_still_work(sql):
    assert validate_sql(sql).valid
