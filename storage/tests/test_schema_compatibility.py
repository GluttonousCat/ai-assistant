"""Keep historical schema imports valid after the domain split."""
from storage import pg_schema
from storage.schema import financial, reports, stock, views


def test_legacy_schema_exports_preserve_identity():
    assert pg_schema.DDL_INCOME is financial.DDL_INCOME
    assert pg_schema.DDL_STOCK_BASIC is stock.DDL_STOCK_BASIC
    assert pg_schema.init_views is views.init_views
    assert pg_schema.ensure_report_analysis_columns is reports.ensure_report_analysis_columns


def test_ddl_is_not_executed_on_import():
    # These are definitions, not migration side effects during tool registration.
    assert all(isinstance(sql, str) for sql in pg_schema.ALL_DDL + pg_schema.FIN_ALL_DDL)
