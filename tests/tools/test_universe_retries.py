from types import SimpleNamespace

import pandas as pd
import pytest

from tools.market import universe


def frame():
    return pd.DataFrame({"ts_code": [f"{600000+n}.SH" for n in range(4001)],
                         "name": "示例", "industry": "制造", "market": "主板", "list_date": "20000101"})


def provider(monkeypatch, responses):
    import tushare
    calls = []
    def stock_basic(**kwargs):
        calls.append(kwargs)
        result = responses[min(len(calls)-1, len(responses)-1)]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(tushare, "pro_api", lambda *a, **kw: SimpleNamespace(stock_basic=stock_basic))
    monkeypatch.setattr(universe, "get_config", lambda: SimpleNamespace(tushare_token="test"))
    monkeypatch.setattr(universe.time, "sleep", lambda _: None)
    return calls


def test_incomplete_response_retries_without_shrinking_the_stock_pool(monkeypatch):
    complete = frame()
    calls = provider(monkeypatch, [complete.head(10), complete])
    assert len(universe.fetch_listed_universe()) == 4001
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [None, "duplicate", "missing_columns", "network_error"])
def test_exhausted_provider_failures_never_accept_an_incomplete_pool(monkeypatch, bad):
    f = frame()
    if bad == "duplicate":
        f.loc[1, "ts_code"] = f.loc[0, "ts_code"]
    elif bad == "missing_columns":
        f = f.drop(columns="ts_code")
    elif bad == "network_error":
        f = TimeoutError("provider unavailable")
    else:
        f = None
    calls = provider(monkeypatch, [f])
    with pytest.raises((ValueError, TimeoutError)):
        universe.fetch_listed_universe()
    assert len(calls) == 3
