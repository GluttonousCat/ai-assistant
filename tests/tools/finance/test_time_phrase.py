import pytest

from tools.finance.stock_kb import parse_time_phrase


@pytest.mark.parametrize(("phrase", "expected"), [
    ("2023 年 q2 营收", ("quarter", (2023, 2))),
    ("2023年三季度", ("quarter", (2023, 3))),
    ("2023年Q三季报", ("quarter", (2023, 3))),
    ("2023Q4", ("year", 2023)),
    ("2023年四季报", ("year", 2023)),
    ("2023年报", ("year", 2023)),
    ("2023H1", ("half", 2023)),
    ("2023年1月", ("year", 2023)),
    ("2023Q10", None),
    ("2023Q5", None),
    ("编号12023Q1", None),
])
def test_quarter_boundaries_and_other_reporting_periods(phrase, expected):
    assert parse_time_phrase(phrase) == expected
