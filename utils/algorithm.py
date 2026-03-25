# -*- encoding: utf-8 -*-
"""
@date: 2026/03/23
@file: algorithm.py
@author: GluttonousCat
"""
from __future__ import annotations

import warnings
import logging

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculate the Average Directional Index to measure trend strength.
    :param df: DataFrame containing 'high', 'low', and 'close' columns.
    :param period: The lookback period for smoothing.
    :return: A pandas Series representing the ADX values.
    """
    df_ta = df.copy()

    # 1. Calculate True Range (TR)
    df_ta['H-L'] = df_ta['high'] - df_ta['low']
    df_ta['H-C'] = np.abs(df_ta['high'] - df_ta['close'].shift(1))
    df_ta['L-C'] = np.abs(df_ta['low'] - df_ta['close'].shift(1))
    df_ta['TR'] = df_ta[['H-L', 'H-C', 'L-C']].max(axis=1)

    # 2. Calculate Directional Movement (+DM, -DM)
    df_ta['+DM'] = np.where((df_ta['high'] - df_ta['high'].shift(1)) >
                            (df_ta['low'].shift(1) - df_ta['low']),
                            np.maximum(
                                df_ta['high'] - df_ta['high'].shift(1), 0), 0)
    df_ta['-DM'] = np.where((df_ta['low'].shift(1) - df_ta['low']) >
                            (df_ta['high'] - df_ta['high'].shift(1)),
                            np.maximum(df_ta['low'].shift(1) - df_ta['low'], 0)
                            , 0)

    # 3. Wilder's Smoothing (RMA)
    alpha = 1 / period
    df_ta['ATR'] = df_ta['TR'].ewm(alpha=alpha, adjust=False).mean()
    df_ta['+DMI'] = df_ta['+DM'].ewm(alpha=alpha, adjust=False).mean() / df_ta['ATR'] * 100
    df_ta['-DMI'] = df_ta['-DM'].ewm(alpha=alpha, adjust=False).mean() / df_ta['ATR'] * 100

    # 4. Calculate DX and ADX
    df_ta['DX'] = np.abs(df_ta['+DMI'] - df_ta['-DMI']) / (
                df_ta['+DMI'] + df_ta['-DMI'] + 1e-8) * 100
    df_ta['ADX'] = df_ta['DX'].ewm(alpha=alpha, adjust=False).mean()

    return df_ta['ADX']


def calculate_rolling_poc(df: pd.DataFrame, window: int = 60, bins: int = 15) -> pd.Series:
    """
    Calculate the Rolling Point of Control using a Volume Profile approach.
    :param df: DataFrame containing 'high', 'low', 'close', and 'volume' columns.
    :param window: The rolling window size for the calculation.
    :param bins: Number of price bins used to segment the volume profile.
    :return: A pandas Series containing the POC price for each timestamp.
    """
    poc_list = [np.nan] * len(df)

    # Use Typical Price (Average of High, Low, Close)
    tp = (df['high'] + df['low'] + df['close']) / 3
    volume = df['volume'].values
    tp_values = tp.values

    for i in range(window, len(df)):
        window_tp = tp_values[i - window: i]
        window_vol = volume[i - window: i]

        # Calculate volume distribution weighted by volume
        hist, bin_edges = np.histogram(window_tp, bins=bins, weights=window_vol)

        # Find the index of the bin with the maximum volume
        max_vol_idx = np.argmax(hist)

        # POC price is the midpoint of the bin with the highest volume
        poc_price = (bin_edges[max_vol_idx] + bin_edges[max_vol_idx + 1]) / 2
        poc_list[i] = poc_price

    return pd.Series(poc_list, index=df.index)


def generate_wyckoff_signals_adx_vp(df: pd.DataFrame) -> pd.DataFrame:
    """
    Generate Wyckoff Spring strategy signals filtered by ADX and Volume Profile (POC).
    :param df: DataFrame containing OHLCV data.
    :return: The original DataFrame updated with indicators and the 'signal_adx_vp' column.
    """
    df['support_20'] = df['low'].rolling(window=20).min().shift(1)
    df['spread'] = df['high'] - df['low']
    df['close_pos'] = (df['close'] - df['low']) / (df['spread'] + 1e-8)

    df['ADX_14'] = calculate_adx(df, period=14)
    df['POC_60'] = calculate_rolling_poc(df, window=60)

    signals = []
    # Statistics counter for diagnostic analysis
    stats = {"insufficient_data": 0, "cond_break": 0, "cond_recover": 0,
             "cond_adx": 0, "cond_vp": 0}

    for i in range(len(df)):
        # Skip if necessary data windows are not yet filled
        if pd.isna(df['support_20'].iloc[i]) or pd.isna(
                df['POC_60'].iloc[i]) or pd.isna(df['ADX_14'].iloc[i]):
            signals.append(0)
            stats["insufficient_data"] += 1
            continue

        current = df.iloc[i]
        prev = df.iloc[i - 1]

        # Sub-conditions for the Wyckoff Spring pattern
        c1 = current['low'] < current['support_20']   # Price breaks below support
        c2 = current['close'] > current['support_20']  # Price recovers above support
        c3 = current['close_pos'] > 0.5               # Strong close (upper half of bar)
        c4 = prev['ADX_14'] < 25                      # Low ADX indicates ranging background
        c5 = current['support_20'] < current['POC_60'] # Support is below the main volume area

        # Debugging counters
        if c1: stats["cond_break"] += 1
        if c1 and c2: stats["cond_recover"] += 1
        if c4: stats["cond_adx"] += 1
        if c5: stats["cond_vp"] += 1

        if c1 and c2 and c3 and c4 and c5:
            signals.append(1)
            logging.info(
                f"🚩 Signal Found! Date: {df.index[i].date()} | Price: {current['close']} | POC: {current['POC_60']:.2f}")
        else:
            signals.append(0)

    # Log Diagnostic Report
    logging.info("=== Strategy Execution Diagnostic Report ===")
    logging.info(f"Total rows: {len(df)}")
    logging.info(f"Skipped (insufficient window): {stats['insufficient_data']}")
    logging.info(f"Met break support condition: {stats['cond_break']}")
    logging.info(f"Met recover support condition: {stats['cond_recover']}")
    logging.info(f"Met low ADX (ranging) condition: {stats['cond_adx']}")
    logging.info(f"Met POC position condition: {stats['cond_vp']}")

    df['signal_adx_vp'] = signals
    return df