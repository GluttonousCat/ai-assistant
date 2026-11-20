from contextlib import nullcontext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.config import get_config
from storage.pg import PgClient
from storage.repositories import report_analysis


@pytest.mark.integration
@pytest.mark.parametrize("session_zone", ["UTC", "Asia/Shanghai"])
def test_report_window_respects_server_clock_and_adjacent_cutoffs(monkeypatch, session_zone):
    get_config()
    end = datetime(2026, 11, 22, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    start = end - timedelta(days=1)
    with PgClient() as pg:
        pg.execute("SET LOCAL TIME ZONE %s", (session_zone,))
        pg.execute("CREATE TEMP TABLE morning_reports ON COMMIT DROP AS SELECT * FROM fin.report_meta WITH NO DATA")
        for rid, stamp, source in ((1, start, "zsxq_file"), (2, end, "zsxq_file"),
                                   (3, start, "zsxq_topic"), (4, start, "zsxq_topic_pdf_absent")):
            pg.execute("INSERT INTO pg_temp.morning_reports(report_id,created_at,source) VALUES (%s,%s,%s)",
                       (rid, stamp, source))

        class TempDatabase:
            def fetch_all(self, sql, args):
                return pg.fetch_all(sql.replace("fin.report_meta", "pg_temp.morning_reports"), args)

        monkeypatch.setattr(report_analysis, "PgClient", lambda **_: nullcontext(TempDatabase()))
        before = report_analysis.reports_ingested_between(start, end)
        after = report_analysis.reports_ingested_between(end, end + timedelta(days=1))
        assert [r["report_id"] for r in before] == [1]
        assert [r["report_id"] for r in after] == [2]
