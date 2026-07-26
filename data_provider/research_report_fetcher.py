# -*- coding: utf-8 -*-
"""
研报数据获取器（fail-open）。

数据来源：
  1. 东财 reportapi.eastmoney.com — 研报列表 + 评级 + 三年 EPS 预测（免费，无 key）
     经由 em_get() 限流，防封。
  2. 同花顺 basic.10jqka.com.cn — 机构一致预期 EPS（免费，无 key，需正常 UA）

任何单一接口失败均 fail-open：返回部分数据而非抛出异常。
"""
from __future__ import annotations

import logging
from io import StringIO
from typing import Any, Dict, List, Optional

import requests

from data_provider.eastmoney_http import em_get

logger = logging.getLogger(__name__)

_REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"
_THS_EPS_URL = "https://basic.10jqka.com.cn/new/{code}/worth.html"
_THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://basic.10jqka.com.cn/",
}


def get_research_reports(
    stock_code: str,
    max_count: int = 10,
    max_pages: int = 3,
) -> List[Dict[str, Any]]:
    """获取东财研报列表。

    Args:
        stock_code: 6 位 A 股代码（不含前缀）
        max_count: 最多返回条数（默认 10）
        max_pages: 最多翻页数（每页 100 条）

    Returns:
        [
            {
                "title": str,
                "date": "YYYY-MM-DD",
                "broker": str,          # 机构简称
                "rating": str,          # 评级（买入/增持/中性/…）
                "eps_this_year": float | None,
                "eps_next_year": float | None,
                "eps_next2_year": float | None,
                "industry": str,
                "info_code": str,       # 可拼 PDF URL
            },
            ...
        ]
        返回空列表表示无研报或请求失败。
    """
    records: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*",
            "pageSize": "100",
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "beginTime": "2000-01-01",
            "endTime": "2099-01-01",
            "pageNo": str(page),
            "fields": "",
            "qType": "0",
            "orgCode": "",
            "code": stock_code,
            "rcode": "",
            "p": str(page),
            "pageNum": str(page),
            "pageNumber": str(page),
        }
        try:
            resp = em_get(
                _REPORT_API_URL,
                params=params,
                headers={"Referer": "https://data.eastmoney.com/"},
                timeout=20,
            )
            data = resp.json()
        except Exception as exc:
            logger.warning("[research_report] page %d fetch failed: %s", page, exc)
            break

        rows = data.get("data") or []
        if not rows:
            break

        for row in rows:
            date_raw = str(row.get("publishDate") or "")
            records.append(
                {
                    "title": str(row.get("title") or ""),
                    "date": date_raw[:10],
                    "broker": str(row.get("orgSName") or ""),
                    "rating": str(row.get("emRatingName") or ""),
                    "eps_this_year": _to_float(row.get("predictThisYearEps")),
                    "eps_next_year": _to_float(row.get("predictNextYearEps")),
                    "eps_next2_year": _to_float(row.get("predictNextTwoYearEps")),
                    "industry": str(row.get("indvInduName") or ""),
                    "info_code": str(row.get("infoCode") or ""),
                }
            )
            if len(records) >= max_count:
                return records

        total_pages = int(data.get("TotalPage") or 1)
        if page >= total_pages:
            break

    return records[:max_count]


def get_eps_forecast(stock_code: str, timeout: int = 15) -> Dict[str, Any]:
    """获取同花顺机构一致预期 EPS。

    Returns:
        {
            "status": "ok" | "no_coverage" | "failed",
            "analyst_count": int | None,
            "eps_this_year": float | None,
            "eps_next_year": float | None,
            "source": "ths_basic",
        }
    """
    result: Dict[str, Any] = {
        "status": "failed",
        "analyst_count": None,
        "eps_this_year": None,
        "eps_next_year": None,
        "source": "ths_basic",
    }
    try:
        import pandas as pd

        url = _THS_EPS_URL.format(code=stock_code)
        resp = requests.get(url, headers=_THS_HEADERS, timeout=timeout)
        resp.encoding = "gbk"
        dfs = pd.read_html(StringIO(resp.text))
    except Exception as exc:
        logger.warning("[eps_forecast] %s fetch failed: %s", stock_code, exc)
        return result

    # Find a table that looks like an EPS forecast table
    target_df = None
    try:
        for df in dfs:
            cols = " ".join(str(c) for c in df.columns)
            if "每股收益" in cols or "均值" in cols or "EPS" in cols.upper():
                target_df = df
                break
        if target_df is None and dfs:
            target_df = dfs[0]
    except Exception:
        return result

    if target_df is None or target_df.empty:
        result["status"] = "no_coverage"
        return result

    try:
        rows = target_df.values.tolist()
        eps_vals: List[Optional[float]] = []
        analyst_count = None
        for row in rows[:2]:
            # Try to extract analyst count from second column and EPS from third
            if len(row) >= 3:
                if analyst_count is None:
                    analyst_count = _to_int(row[1])
                eps_vals.append(_to_float(row[2]))

        result["status"] = "ok" if eps_vals else "no_coverage"
        result["analyst_count"] = analyst_count
        if len(eps_vals) > 0:
            result["eps_this_year"] = eps_vals[0]
        if len(eps_vals) > 1:
            result["eps_next_year"] = eps_vals[1]
    except Exception as exc:
        logger.warning("[eps_forecast] %s parse failed: %s", stock_code, exc)
        result["status"] = "failed"

    return result


def get_research_context(
    stock_code: str,
    max_count: int = 5,
    tushare_fetcher=None,
    kpl_fetcher=None,
) -> Dict[str, Any]:
    """聚合研报+EPS 预测，返回适合 agent 消费的 context 块。

    降级链：KPL → Tushare report_rc → 东财 reportapi。
    KPL 排在最前是因为 Tushare 代理站即将下线；三者任一有结果即停止下探。

    Returns:
        {
            "status": "ok" | "partial" | "failed",
            "report_count": int,
            "latest_reports": [...],   # 最近 max_count 条
            "rating_summary": {"买入": int, "增持": int, ...},
            "eps_forecast": {...},
            "source_chain": [...],
        }
    """
    errors: List[str] = []
    source_chain: List[str] = []

    # --- 研报列表：Tushare 优先，东财兜底 ---
    reports: List[Dict[str, Any]] = []
    if kpl_fetcher is not None:
        try:
            kpl_reports = kpl_fetcher.get_research_reports(stock_code, max_count=max_count)
            if kpl_reports:
                reports = kpl_reports
                source_chain.append("kpl_research_field_list")
        except Exception as exc:
            errors.append(f"kpl_research:{type(exc).__name__}")

    if not reports and tushare_fetcher is not None:
        try:
            ts_reports = tushare_fetcher.get_research_reports(stock_code, max_count=max_count)
            if ts_reports:
                reports = ts_reports
                source_chain.append("tushare_report_rc")
        except Exception as exc:
            errors.append(f"tushare_report_rc:{type(exc).__name__}")

    if not reports:
        try:
            reports = get_research_reports(stock_code, max_count=max_count)
            source_chain.append("eastmoney_reportapi")
        except Exception as exc:
            errors.append(f"reportapi:{type(exc).__name__}")

    # --- EPS 预测 ---
    eps: Dict[str, Any] = {}
    try:
        eps = get_eps_forecast(stock_code)
        source_chain.append("ths_basic")
    except Exception as exc:
        errors.append(f"ths_eps:{type(exc).__name__}")

    # --- 评级统计 ---
    rating_summary: Dict[str, int] = {}
    for r in reports:
        rating = r.get("rating") or "未知"
        rating_summary[rating] = rating_summary.get(rating, 0) + 1

    has_data = bool(reports) or eps.get("status") == "ok"
    status = "ok" if has_data else ("partial" if source_chain else "failed")

    return {
        "status": status,
        "report_count": len(reports),
        "latest_reports": reports,
        "rating_summary": rating_summary,
        "eps_forecast": eps,
        "source_chain": source_chain,
        "errors": errors,
    }


def _to_float(val: Any) -> Optional[float]:
    try:
        if val is None or val == "":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> Optional[int]:
    try:
        if val is None or val == "":
            return None
        return int(float(val))
    except (TypeError, ValueError):
        return None
