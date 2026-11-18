"""Versioned strategy catalog, including the declarative rule entrypoint."""

from skills.trend.strategy import VERSION, TrendConfig

STRATEGIES = {
    "wyckoff_60f_v31": {
        "name": "60F量价·移动防守与进攻",
        "family": None,
        "generation": "wyckoff",
        "research_only": True,
        "description": "Spring低量测试或SOS后LPS；量价确认加仓、测试支撑上移与供应确认分批防守。实验策略，不宣称完整阶段识别。",
    },
    "custom_rules_v1": {
        "name": "自定义组合规则",
        "family": None,
        "generation": "rules",
        "description": "自由组合60F/日K指标、比较、交叉及全部/任一条件；盘后评估、次日开盘执行。规则工作台首版采用单次进出。",
    },
    "swing_structure_v9": {
        "name": "60F结构趋势·反转与回调",
        "family": None,
        "generation": "v9",
        "description": "按时间验证启动、推进、缩量回撤和再转强；底部需低点测试。盘后计划、次日执行，无日K形态门槛。固定对照未通过收益与风险验证，限研究。",
    },
    "swing_structure_pullback_v9": {
        "name": "60F结构趋势·上涨回调",
        "family": "trend_pullback",
        "generation": "v9",
        "description": "先有至少5%且4ATR的有序推进，再验证受控缩量回调；首次与后续分开记录。固定对照未通过验证，限研究。",
    },
    "swing_structure_reversal_v9": {
        "name": "60F结构趋势·测试反转",
        "family": "base_rebound",
        "generation": "v9",
        "description": "有反弹间隔的低点测试、DIF改善及测试量能衰减，等待60F重新收复短期结构；固定对照未通过验证，限研究。",
    },
    "swing_60f_v8": {
        "name": "主板60F·底背离与上涨回调",
        "family": None,
        "generation": "v8",
        "description": "60F已确认低点的MACD底背离或上涨回调转强；盘后汇总，次日开盘按风险执行，无日K形态门槛。参数研究不保证5%收益。",
    },
    "swing_reversal_v8": {
        "name": "主板60F·底背离反转",
        "family": "base_rebound",
        "generation": "v8",
        "description": "仅研究已确认的60F低点背离与转强，盘后决策、次日执行，无日K阳线要求。",
    },
    "swing_pullback_v8": {
        "name": "主板60F·上涨回调",
        "family": "trend_pullback",
        "generation": "v8",
        "description": "仅研究60F升势中的受控回调与恢复，盘后决策、次日执行，无日K阳线要求。",
    },
    "mtf_risk_v7": {
        "name": "60F结构机会·反转与趋势回调",
        "family": None,
        "generation": "v7",
        "description": "低位反转、缺口守稳及趋势受控回调分别识别，区分首次与后续；安全边际按结构失效距离衡量，日K确认后次日执行。",
    },
    "mtf_reversal_v6": {
        "name": "60F低位早期反转",
        "family": "base_rebound",
        "generation": "v6",
        "description": "阴跌或深坑后首阳观察，追踪原始起点；限早期12根/前4推进阳线，日K确认后次日开盘入场。低位不保证收益。",
    },
    "mtf_dual_v5": {
        "name": "60F趋势·日K双机会",
        "family": None,
        "generation": "v5",
        "description": "M8/M13/M21判断60F趋势；日K转强确认入场；结构止损与有限退出方案对照，按开发段选择后固定检验。",
    },
    "mtf_pullback_v5": {
        "name": "60F趋势·日K回调",
        "family": "trend_pullback",
        "generation": "v5",
        "description": "60F方向配合已有日线升势的受控回调，日K转强后下一交易日开盘入场。",
    },
    "mtf_rebound_v5": {
        "name": "60F修复·日K反包",
        "family": "base_rebound",
        "generation": "v5",
        "description": "60F均线开始修复，日K在阴跌/盘整后反包收复；不强等日线长均线多头。",
    },
    "trend_dual_v4": {
        "name": "趋势双机会",
        "family": None,
        "description": "原升势中的回调再起，以及横盘阴跌/砸坑后的结构修复；首阳观察，第2—4推进确认。",
    },
    "trend_pullback_v4": {
        "name": "趋势回调介入",
        "family": "trend_pullback",
        "description": "已有升势与受控回调，止跌后重新推进；60F要求已完成日线方向向上。",
    },
    "base_rebound_v4": {
        "name": "阴跌/砸坑反弹",
        "family": "base_rebound",
        "description": "横盘阴跌修复或坑后收回原平台；允许日线方向仍偏弱。",
    },
}


def catalog():
    from skills.trend.mtf import MtfConfig, version_for

    result = []
    for key, value in STRATEGIES.items():
        if value.get("research_only"):
            continue
        gen = value.get("generation")
        if gen == "rules":
            from skills.quant.rule_models import RuleStrategy
            from skills.quant.rule_prepare import VERSION as RULE_VERSION

            result.append(
                {
                    **value,
                    "id": key,
                    "version": RULE_VERSION,
                    "timeframes": ["1D"],
                    "entry_config": RuleStrategy().model_dump(),
                    "trend_timeframe": "60F",
                    "execution_schedule": "after_close",
                }
            )
            continue
        if gen in ("v8", "v9"):
            from skills.trend.swing import SwingConfig, VERSION as SWING_VERSION
            from skills.trend.structure import (
                StructureConfig,
                VERSION as STRUCTURE_VERSION,
            )

            result.append(
                {
                    **value,
                    "id": key,
                    "version": STRUCTURE_VERSION if gen == "v9" else SWING_VERSION,
                    "timeframes": ["1D"],
                    "entry_config": vars(
                        (StructureConfig if gen == "v9" else SwingConfig)(
                            family=value["family"] or "both"
                        )
                    ),
                    "trend_timeframe": "60F",
                    "execution_schedule": "after_close",
                }
            )
            continue
        mtf = gen in ("v5", "v6", "v7")
        cfg = (
            MtfConfig(
                fresh_reversal=gen in ("v6", "v7"),
                reversal_cycle_reset=gen in ("v6", "v7"),
                risk_setups=gen == "v7",
            )
            if mtf
            else TrendConfig()
        )
        result.append(
            {
                **value,
                "id": key,
                "version": version_for(cfg) if mtf else VERSION,
                "timeframes": ["1D"] if mtf else ["60F", "1D"],
                "entry_config": cfg.__dict__,
                "trend_timeframe": "60F" if mtf else None,
            }
        )
    return result
