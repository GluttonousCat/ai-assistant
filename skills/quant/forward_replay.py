"""Append causal observations on frozen quote units and model coefficients."""

from dataclasses import replace

import numpy as np
import pandas as pd

from skills.quant.online_priority import FEATURES, feature_vector
from skills.quant.prepare import _normalized


def extend_execution(stock, daily, anchor):
    position = stock.times.get_indexer([pd.Timestamp(anchor.bar_end)])[0]
    anchor_factor = float(anchor.adj_factor)
    base = float(anchor.factor_base)
    if (
        position < 0
        or not np.isfinite(base)
        or base <= 0
        or not np.isfinite(anchor_factor)
        or not np.isclose(
            anchor_factor / base, stock.data["scale"][position], rtol=1e-12, atol=1e-12
        )
    ):
        raise ValueError(f"{stock.code} 新增行情复权基准与旧记录不一致")
    frame, appended, issue = _normalized(daily)
    if issue:
        raise ValueError(f"{stock.code} {issue}")
    times = pd.DatetimeIndex(frame.bar_end)
    if times[0] <= stock.times[-1]:
        raise ValueError(f"{stock.code} 新增日线与冻结行情重叠")
    scale = frame.adj_factor.to_numpy(dtype=float) / base
    appended["scale"] = scale
    for name in ("open", "high", "low", "close"):
        appended["adjusted_" + name] = appended[name] * scale
    return replace(
        stock,
        times=stock.times.append(times),
        data={
            name: np.concatenate((stock.data[name], values))
            for name, values in appended.items()
        },
        signals=[],
    )


def frozen_predictions(stock, model):
    month = pd.Period(model["month"], freq="M")
    if not model["trained"] or pd.Timestamp(model["last_label"]) >= month.start_time:
        raise ValueError("冻结排序模型包含未到期训练标签")
    coef = np.asarray([model["coefficients"][key] for key in FEATURES])
    mean, scale = np.asarray(model["feature_means"]), np.asarray(model["feature_stds"])
    if (
        any(
            x.shape != coef.shape or not np.isfinite(x).all()
            for x in (mean, scale, coef)
        )
        or not np.isfinite(model["intercept"])
        or (scale <= 0).any()
    ):
        raise ValueError("冻结排序模型系数无效")
    predictions = {}
    for signal in stock.signals:
        i = signal["i"]
        if stock.times[i].to_period("M") != month:
            raise ValueError("冻结排序模型月份不匹配")
        x = feature_vector(stock, signal)
        if np.isfinite(x).all():
            predictions[(stock.code, i)] = float(
                model["intercept"] + np.clip((x - mean) / scale, -5, 5) @ coef
            )
    return predictions


def append_evidence(frozen, computed):
    """Keep frozen prefix byte-for-byte; append derived values and new signals."""
    n = len(frozen.times)
    if frozen.code != computed.code or not frozen.times.equals(computed.times[:n]):
        raise ValueError("延续行情改变了冻结时间轴")
    unused = {"structural_support", "support_origin", "structure_failed"}
    if set(frozen.data) - set(computed.data) - unused:
        raise ValueError("延续行情缺少已冻结的撮合证据列")
    data = {
        name: np.concatenate((values, computed.data[name][n:]))
        for name, values in frozen.data.items()
        if name in computed.data
    }
    return replace(
        frozen,
        times=computed.times,
        data=data,
        signals=[
            *frozen.signals,
            *(s for s in computed.signals if s["i"] >= n),
        ],
    )
