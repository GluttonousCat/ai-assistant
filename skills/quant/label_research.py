"""Causal label comparisons with frozen features and separate quality scores."""

from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from skills.quant.candidate_quality import attach_signal_quality
from skills.quant.market_context import MarketPolicy, apply_policy
from skills.quant.online_priority import (
    apply_predictions,
    auxiliary_outcome,
    feature_vector,
    fit_monthly,
    training_rows,
)


def restore_candidate_features(original, cached):
    source = {s.code: s for s in cached}
    if (
        len(source) != len(cached)
        or len({s.code for s in original}) != len(original)
        or set(source) != {s.code for s in original}
    ):
        raise ValueError("原始候选与特征缓存股票池不一致或代码重复")
    result = []
    for stock in original:
        feature = source[stock.code]
        if not stock.times.equals(feature.times) or any(
            not np.array_equal(stock.data[key], feature.data[key], equal_nan=True)
            for key in ("open", "high", "low", "close", "scale", "adjusted_close")
        ):
            raise ValueError(f"{stock.code}冻结特征的时间/原价/复权基准不一致")
        data = {
            **stock.data,
            **{k: feature.data[k].copy() for k in ("risk_atr_pct", "risk_downside20")},
        }
        result.append(replace(stock, data=data))
    return result


def learn_priorities(stocks, calendar, frames, cfg, minimum_quality=72):
    records = training_rows(stocks, calendar, frames, cfg)
    models, predictions = fit_monthly(records, cfg)
    if any(
        m["trained"]
        and pd.Timestamp(m["last_label"]) >= pd.Period(m["month"]).start_time
        for m in models
    ):
        raise ValueError("模型训练含有当月才知晓的标签")
    ranked = [
        apply_policy(s, MarketPolicy("none"))
        for s in apply_predictions(stocks, predictions, cfg)
    ]
    selected = attach_signal_quality(ranked, stocks, minimum_quality)
    signals = [s for stock in selected for s in stock.signals]
    return dict(
        series=selected,
        models=models,
        model_config=asdict(cfg),
        audit=dict(
            candidate_rows=len(records),
            known_outcomes=sum(bool(np.isfinite(r[4])) for r in records),
            forecasts=len(predictions),
            selected_signals=len(signals),
            minimum_quality=min((s["quality_score"] for s in signals), default=None),
            every_training_label_before_month=True,
            first_trained_month=next(
                (m["month"] for m in models if m["trained"]), None
            ),
        ),
    )


def fit_absolute_head(stocks, calendar, cfg):
    clock = {t: i for i, t in enumerate(pd.DatetimeIndex(calendar))}
    records = []
    seen = set()
    for stock in stocks:
        for signal in stock.signals:
            key = stock.code, signal["i"]
            if key in seen:
                raise ValueError("绝对收益模型存在同股同日重复候选")
            seen.add(key)
            y, known = auxiliary_outcome(stock, signal, clock, cfg)
            records.append(
                (
                    stock.code,
                    signal["i"],
                    stock.times[signal["i"]],
                    feature_vector(stock, signal),
                    y,
                    known,
                )
            )
    models, predictions = fit_monthly(records, cfg)
    if any(
        m["trained"]
        and pd.Timestamp(m["last_label"]) >= pd.Period(m["month"]).start_time
        for m in models
    ):
        raise ValueError("绝对收益模型含有训练月份之后才知晓的标签")
    return dict(models=models, predictions=predictions, candidate_rows=len(records))


def confirm_absolute_forecasts(
    relative, original, absolute, minimum_absolute=0, absolute_weight=0
):
    if (
        not np.isfinite(minimum_absolute)
        or not 0 <= minimum_absolute <= 1
        or not np.isfinite(absolute_weight)
        or not 0 <= absolute_weight <= 1
    ):
        raise ValueError("绝对收益确认门槛须在0至1%、排序权重在0至1之间")
    ranked = []
    for stock in relative["series"]:
        signals = []
        for signal in stock.signals:
            value = absolute["predictions"].get((stock.code, signal["i"]))
            if value is None or not np.isfinite(value) or value < minimum_absolute:
                continue
            score = (1 - absolute_weight) * signal["score"] + absolute_weight * value
            signals.append(
                {
                    **signal,
                    "score": float(score),
                    "evidence": {
                        **(signal.get("evidence") or {}),
                        "forecast_heads": dict(
                            relative=float(signal["score"]), absolute=float(value)
                        ),
                    },
                }
            )
        ranked.append(replace(stock, signals=signals))
    selected = attach_signal_quality(ranked, original, 72)
    signals = [s for stock in selected for s in stock.signals]
    return dict(
        series=selected,
        models=relative["models"],
        absolute_models=absolute["models"],
        model_config={
            **relative["model_config"],
            "absolute_confirmation_pct": minimum_absolute,
            "absolute_priority_weight": absolute_weight,
        },
        audit=dict(
            original_relative_signals=sum(len(s.signals) for s in relative["series"]),
            selected_signals=len(signals),
            minimum_quality=min((s["quality_score"] for s in signals), default=None),
        ),
    )
