"""Keep native setup quality separate from a model's portfolio priority.

Old frozen online caches store predictions in ``score``. Recover quality only
from the matching frozen setup, never from the prediction or list position.
The v8 quality formula has a native range of 0–120, not a calibrated probability.
"""

from dataclasses import replace
import math


def attach_signal_quality(ranked, original, minimum_score=72):
    if not math.isfinite(minimum_score) or not 0 <= minimum_score <= 120:
        raise ValueError("原始质量分门槛必须在0至120之间")
    source = {stock.code: stock for stock in original}
    codes = {stock.code for stock in ranked}
    if (
        len(source) != len(original)
        or len(codes) != len(ranked)
        or codes != set(source)
    ):
        raise ValueError("原始与排序股票池不一致或代码重复")
    result = []
    for stock in ranked:
        raw = source[stock.code]
        if not stock.times.equals(raw.times):
            raise ValueError(f"{stock.code}原始与排序时间轴不一致")
        originals = {(s["i"], s["leg"]): s for s in raw.signals}
        if len(originals) != len(raw.signals):
            raise ValueError(f"{stock.code}原始信号身份重复")
        seen, signals = set(), []
        for signal in stock.signals:
            key = (signal["i"], signal["leg"])
            setup = originals.get(key)
            if key in seen or setup is None:
                raise ValueError(f"{stock.code}排序信号缺少唯一原始依据")
            seen.add(key)
            if any(signal.get(k) != setup.get(k) for k in ("family", "count", "stop")):
                raise ValueError(f"{stock.code}原始信号结构或复权尺度不一致")
            quality, priority = float(setup["score"]), float(signal["score"])
            if (
                not math.isfinite(quality)
                or not 0 <= quality <= 120
                or not math.isfinite(priority)
            ):
                raise ValueError(f"{stock.code}质量分或排序分无效")
            if quality < minimum_score:
                continue
            selection = dict(
                quality_score=quality,
                quality_scale="v8-native-0-120",
                priority_score=priority,
                minimum_quality_score=minimum_score,
            )
            signals.append(
                {
                    **signal,
                    "quality_score": quality,
                    "priority_score": priority,
                    "evidence": {
                        **(signal.get("evidence") or {}),
                        "selection": selection,
                    },
                }
            )
        result.append(replace(stock, signals=signals))
    return result
