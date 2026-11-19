from datetime import datetime

import pandas as pd
import pytest

from jobs.cache_intraday_prefix import closed_prefix


def frames(times):
    return pd.DataFrame({"bar_end": pd.to_datetime(["2026-11-17 " + t for t in times]), "close": 10.})


def test_only_requested_closed_morning_bars_survive_future_labeled_rows():
    frame = frames(["10:30", "11:30", "14:00", "15:00"])
    result = closed_prefix(frame, "2026-11-17", "11:30", datetime(2026, 11, 17, 14, 15))
    assert list(result.bar_end.dt.strftime("%H:%M")) == ["10:30", "11:30"]
    assert len(frame) == 4


def test_an_open_bar_or_missing_morning_slot_cannot_be_accepted():
    with pytest.raises(ValueError, match="尚未收盘"):
        closed_prefix(frames(["10:30", "11:30"]), "2026-11-17", "11:30", datetime(2026, 11, 17, 11, 29))
    with pytest.raises(ValueError, match="缺根"):
        closed_prefix(frames(["11:30"]), "2026-11-17", "11:30", datetime(2026, 11, 17, 14))


@pytest.mark.parametrize("slot,count", [("10:30", 1), ("11:30", 2), ("14:00", 3), ("15:00", 4)])
def test_all_four_slots_require_the_exact_completed_prefix(slot, count):
    all_bars = frames(["10:30", "11:30", "14:00", "15:00"])
    result = closed_prefix(all_bars, "2026-11-17", slot, datetime(2026, 11, 17, 15, 2))
    assert len(result) == count and result.bar_end.iloc[-1].strftime("%H:%M") == slot
