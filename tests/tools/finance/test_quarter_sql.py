import sqlite3
from contextlib import closing

import pytest

from tools.finance.sql_builder import build_compare_query, build_industry_query, build_single_query
from tools.finance.stock_kb import parse_time_phrase


@pytest.fixture
def reports():
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("""
            ATTACH ':memory:' AS fin;
            ATTACH ':memory:' AS stock;
            CREATE TABLE fin.income (ts_code, end_date, report_type, revenue);
            CREATE TABLE fin.fina_indicator (ts_code, end_date, report_type);
            CREATE TABLE fin.balancesheet (ts_code, end_date, report_type);
            CREATE TABLE fin.cashflow (ts_code, end_date, report_type, n_cashflow_act);
            CREATE TABLE stock.stock_basic (ts_code, name);
            CREATE TABLE stock.v_industry_current (ts_code, index_l2, industry_l2, industry_l1);
            INSERT INTO stock.stock_basic VALUES ('A', 'Alpha'), ('B', 'Beta');
            INSERT INTO stock.v_industry_current VALUES
                ('A', '01', 'Bank', 'Finance'), ('B', '01', 'Bank', 'Finance');
            INSERT INTO fin.income VALUES
                ('A', '2023-03-31', '1', 10), ('A', '2023-12-31', '1', 90),
                ('B', '2023-03-31', '1', 20), ('B', '2023-06-30', '1', 50);
            INSERT INTO fin.cashflow VALUES
                ('A', '2023-03-31', '1', 2), ('A', '2023-03-31', '2', 200),
                ('B', '2023-03-31', '1', 4), ('B', '2023-06-30', '1', 8);
        """)
        yield db


@pytest.mark.parametrize("phrase", ["2023Q1", "2023年1季报", "2023年一季报"])
def test_quarter_spellings_select_the_same_filing(reports, phrase):
    period = parse_time_phrase(f"Alpha{phrase}的营收是多少？")
    sql = build_single_query("A", "Alpha", "revenue", "i", period)
    assert reports.execute(sql).fetchall() == [("Alpha", "2023-03-31", 10)]


def test_comparison_returns_the_requested_quarter_for_each_stock(reports):
    sql = build_compare_query([("A", "Alpha"), ("B", "Beta")], "revenue", "i",
                              ("quarter", (2023, 1)))
    assert reports.execute(sql).fetchall() == [
        ("Beta", "2023-03-31", 20), ("Alpha", "2023-03-31", 10),
    ]


@pytest.mark.parametrize(("period", "expected"), [
    (("quarter", (2023, 1)), [("Beta", "2023-03-31", 20), ("Alpha", "2023-03-31", 10)]),
    (("quarter", (2023, 2)), [("Beta", "2023-06-30", 50)]),
    (None, [("Alpha", "2023-12-31", 90), ("Beta", "2023-06-30", 50)]),
])
def test_industry_respects_requested_period_or_each_members_latest(reports, period, expected):
    sql = build_industry_query("01", "revenue", "i", period)
    rows = reports.execute(sql).fetchall()
    assert [(name, end_date, revenue) for name, _, end_date, revenue in rows] == expected


def test_industry_cashflow_matches_quarter_and_report_type(reports):
    sql = build_industry_query("01", "n_cashflow_act", "c", ("quarter", (2023, 1)))
    assert reports.execute(sql).fetchall() == [
        ("Beta", "Bank", "2023-03-31", 4), ("Alpha", "Bank", "2023-03-31", 2),
    ]
