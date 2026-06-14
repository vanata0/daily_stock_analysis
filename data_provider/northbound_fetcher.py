# -*- coding: utf-8 -*-
"""
北向资金（沪深港通）实时分钟流向。

数据来源：同花顺 data.hexin.cn/market/hsgtApi
  - 零鉴权，仅需 User-Agent + Host/Referer
  - 不封 IP（TCP 非东财体系）
  - 实测 73 ms 拿到当日 262 个分钟点

注意：eastmoney 全系北向数据自 2024-08 起断供（净买额字段返回 NaN/0），
已改用同花顺 hsgtApi 作为数据源。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_HSGT_URL = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
_HSGT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}


def get_northbound_realtime(timeout: int = 10) -> List[Dict[str, Any]]:
    """获取沪深股通当日实时分钟流向。

    Returns:
        [{"time": "09:30", "hgt_yi": 12.5, "sgt_yi": -3.2}, ...]
        hgt_yi: 沪股通累计净买入（亿元，正=净流入，负=净流出）
        sgt_yi: 深股通累计净买入（亿元）
        返回空列表表示非交易时间或网络失败。
    """
    try:
        resp = requests.get(_HSGT_URL, headers=_HSGT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("[northbound] realtime fetch failed: %s", exc)
        return []

    times: List[str] = data.get("time") or []
    hgt: List[Optional[float]] = data.get("hgt") or []
    sgt: List[Optional[float]] = data.get("sgt") or []
    n = len(times)

    rows = []
    for i in range(n):
        hgt_val = hgt[i] if i < len(hgt) else None
        sgt_val = sgt[i] if i < len(sgt) else None
        # Eastmoney-sourced fields sometimes return 0 when data is absent
        rows.append(
            {
                "time": times[i],
                "hgt_yi": float(hgt_val) if hgt_val not in (None, 0, "-") else None,
                "sgt_yi": float(sgt_val) if sgt_val not in (None, 0, "-") else None,
            }
        )
    return rows


def get_northbound_summary(timeout: int = 10) -> Dict[str, Any]:
    """返回北向资金当日最新状态摘要（适合 agent context）。

    Returns:
        {
            "status": "ok" | "empty" | "failed",
            "date": "YYYY-MM-DD",
            "hgt_latest_yi": float | None,    # 沪股通最新累计净买入(亿)
            "sgt_latest_yi": float | None,    # 深股通最新累计净买入(亿)
            "total_latest_yi": float | None,  # 合计
            "hgt_direction": "inflow" | "outflow" | None,
            "sgt_direction": "inflow" | "outflow" | None,
            "data_points": int,
            "source": "ths_hsgtApi",
        }
    """
    today = date.today().strftime("%Y-%m-%d")
    rows = get_northbound_realtime(timeout=timeout)

    if not rows:
        return {
            "status": "failed",
            "date": today,
            "hgt_latest_yi": None,
            "sgt_latest_yi": None,
            "total_latest_yi": None,
            "hgt_direction": None,
            "sgt_direction": None,
            "data_points": 0,
            "source": "ths_hsgtApi",
        }

    # Walk from end to find last valid (non-None) values
    hgt_val: Optional[float] = next(
        (r["hgt_yi"] for r in reversed(rows) if r["hgt_yi"] is not None), None
    )
    sgt_val: Optional[float] = next(
        (r["sgt_yi"] for r in reversed(rows) if r["sgt_yi"] is not None), None
    )

    total = (
        round(hgt_val + sgt_val, 2)
        if hgt_val is not None and sgt_val is not None
        else None
    )

    def _direction(val: Optional[float]) -> Optional[str]:
        if val is None:
            return None
        return "inflow" if val >= 0 else "outflow"

    has_data = hgt_val is not None or sgt_val is not None
    return {
        "status": "ok" if has_data else "empty",
        "date": today,
        "hgt_latest_yi": hgt_val,
        "sgt_latest_yi": sgt_val,
        "total_latest_yi": total,
        "hgt_direction": _direction(hgt_val),
        "sgt_direction": _direction(sgt_val),
        "data_points": len(rows),
        "source": "ths_hsgtApi",
    }
