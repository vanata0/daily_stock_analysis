# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests as _requests

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                result["growth"] = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates([
            ("stock_yjyg_em", {"symbol": stock_code}),
            ("stock_yjyg_em", {}),
            ("stock_yjbb_em", {"symbol": stock_code}),
            ("stock_yjbb_em", {}),
        ])
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates([
            ("stock_yjkb_em", {"symbol": stock_code}),
            ("stock_yjkb_em", {}),
        ])
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def _get_capital_flow_mairui(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        Fetch individual stock capital flow from Mairuiapi.

        Returns a stock_flow dict with main_net_inflow / inflow_5d / inflow_10d
        (all in yuan), plus a granular breakdown by deal size. Returns None when
        the key is absent, the request fails, or data is empty.
        """
        from src.config import get_config
        licence = get_config().mairui_api_key
        if not licence:
            return None

        # Mairuiapi uses bare 6-digit A-share codes (no sh/sz prefix)
        code = re.sub(r'^(sh|sz|bj)', '', stock_code, flags=re.IGNORECASE)

        url = f"https://api.mairuiapi.com/hsstock/history/transaction/{code}/{licence}?lt=10"
        try:
            resp = _requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("mairui capital_flow request failed for %s: %s", stock_code, exc)
            return None

        if not isinstance(data, list) or not data:
            return None

        # Check for API-level error responses (e.g. {"code": -1, "msg": "..."})
        if isinstance(data, dict):
            return None

        def _day_net(rec: dict) -> float:
            buy = rec.get("zmbtdcje", 0) or 0
            buy += rec.get("zmbddcje", 0) or 0
            sell = rec.get("zmstdcje", 0) or 0
            sell += rec.get("zmsddcje", 0) or 0
            return float(buy - sell)

        latest = data[0]
        main_net_inflow = _day_net(latest)
        inflow_5d = sum(_day_net(r) for r in data[:5]) if len(data) >= 5 else None
        inflow_10d = sum(_day_net(r) for r in data[:10]) if len(data) >= 10 else None

        # Granular breakdown for the most recent trading day
        breakdown = {
            "super_buy":  float(latest.get("zmbtdcje", 0) or 0),
            "large_buy":  float(latest.get("zmbddcje", 0) or 0),
            "mid_buy":    float(latest.get("zmbzdcje", 0) or 0),
            "small_buy":  float(latest.get("zmbxdcje", 0) or 0),
            "super_sell": float(latest.get("zmstdcje", 0) or 0),
            "large_sell": float(latest.get("zmsddcje", 0) or 0),
            "mid_sell":   float(latest.get("zmszdcje", 0) or 0),
            "small_sell": float(latest.get("zmsxdcje", 0) or 0),
        }

        return {
            "main_net_inflow": main_net_inflow,
            "inflow_5d": inflow_5d,
            "inflow_10d": inflow_10d,
            "breakdown": breakdown,
            "trade_date": str(latest.get("t", "")),
        }

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.

        Individual stock flow: Mairuiapi (primary) → AkShare East-Finance (fallback).
        Sector rankings: AkShare only.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        # --- Individual stock flow: Mairuiapi first ---
        mairui_flow = self._get_capital_flow_mairui(stock_code)
        if mairui_flow is not None:
            result["stock_flow"] = mairui_flow
            result["source_chain"].append("capital_stock:mairui")
        else:
            # Fallback: AkShare East-Finance
            stock_df, stock_source, stock_errors = self._call_df_candidates([
                ("stock_individual_fund_flow", {"stock": stock_code}),
                ("stock_individual_fund_flow", {"symbol": stock_code}),
                ("stock_individual_fund_flow", {}),
                ("stock_main_fund_flow", {"symbol": stock_code}),
                ("stock_main_fund_flow", {}),
            ])
            result["errors"].extend(stock_errors)
            if stock_df is not None:
                row = _extract_latest_row(stock_df, stock_code)
                if row is not None:
                    net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入", "净流入", "净额"]))
                    inflow_5d = _safe_float(_pick_by_keywords(row, ["5日", "五日"]))
                    inflow_10d = _safe_float(_pick_by_keywords(row, ["10日", "十日"]))
                    result["stock_flow"] = {
                        "main_net_inflow": net_inflow,
                        "inflow_5d": inflow_5d,
                        "inflow_10d": inflow_10d,
                    }
                    result["source_chain"].append(f"capital_stock:{stock_source}")

        sector_df, sector_source, sector_errors = self._call_df_candidates([
            ("stock_sector_fund_flow_rank", {}),
            ("stock_sector_fund_flow_summary", {}),
        ])
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result

    # ------------------------------------------------------------------
    # 融资融券 / 大宗交易 / 股东户数 — 东财 datacenter-web 直连
    # 防封：所有调用经 em_datacenter() → em_get()，串行限流 ≥1s
    # ------------------------------------------------------------------

    def get_margin_trading(self, stock_code: str, days: int = 30) -> Dict[str, Any]:
        """融资融券明细（日级，最近 N 天）。

        Returns:
            {
                "status": "ok" | "empty" | "failed",
                "stock_code": str,
                "records": [
                    {
                        "date": str,
                        "rzye": float,   # 融资余额（元）
                        "rzmre": float,  # 融资买入额
                        "rzche": float,  # 融资偿还额
                        "rqye": float,   # 融券余额（元）
                        "rzrqye": float, # 两融合计余额
                    },
                    ...
                ],
                "source": "eastmoney_datacenter",
                "errors": [],
            }
        """
        from data_provider.eastmoney_http import em_datacenter

        result: Dict[str, Any] = {
            "status": "failed",
            "stock_code": stock_code,
            "records": [],
            "source": "eastmoney_datacenter",
            "errors": [],
        }
        try:
            rows = em_datacenter(
                "RPTA_WEB_RZRQ_GGMX",
                filter_str=f'(SCODE="{stock_code}")',
                page_size=days,
                sort_columns="DATE",
                sort_types="-1",
            )
            records = []
            for row in rows:
                records.append(
                    {
                        "date": str(row.get("DATE", ""))[:10],
                        "rzye": float(row.get("RZYE") or 0),
                        "rzmre": float(row.get("RZMRE") or 0),
                        "rzche": float(row.get("RZCHE") or 0),
                        "rqye": float(row.get("RQYE") or 0),
                        "rzrqye": float(row.get("RZRQYE") or 0),
                    }
                )
            result["records"] = records
            result["status"] = "ok" if records else "empty"
        except Exception as exc:
            result["errors"].append(f"margin_trading:{type(exc).__name__}:{exc}")
            logger.warning("[margin_trading] %s failed: %s", stock_code, exc)
        return result

    def get_block_trade(self, stock_code: str, count: int = 20) -> Dict[str, Any]:
        """大宗交易记录（最近 N 条）。

        Returns:
            {
                "status": "ok" | "empty" | "failed",
                "stock_code": str,
                "records": [
                    {
                        "date": str,
                        "price": float,       # 成交价
                        "close": float,       # 当日收盘价
                        "premium_pct": float, # 溢价率（%）
                        "volume": float,      # 成交量（股）
                        "amount": float,      # 成交额（元）
                        "buyer": str,
                        "seller": str,
                    },
                    ...
                ],
                "source": "eastmoney_datacenter",
                "errors": [],
            }
        """
        from data_provider.eastmoney_http import em_datacenter

        result: Dict[str, Any] = {
            "status": "failed",
            "stock_code": stock_code,
            "records": [],
            "source": "eastmoney_datacenter",
            "errors": [],
        }
        try:
            rows = em_datacenter(
                "RPT_DATA_BLOCKTRADE",
                filter_str=f'(SECURITY_CODE="{stock_code}")',
                page_size=count,
                sort_columns="TRADE_DATE",
                sort_types="-1",
            )
            records = []
            for row in rows:
                close = float(row.get("CLOSE_PRICE") or 0)
                price = float(row.get("DEAL_PRICE") or 0)
                premium = round((price / close - 1) * 100, 2) if close else 0.0
                records.append(
                    {
                        "date": str(row.get("TRADE_DATE", ""))[:10],
                        "price": price,
                        "close": close,
                        "premium_pct": premium,
                        "volume": float(row.get("DEAL_VOLUME") or 0),
                        "amount": float(row.get("DEAL_AMT") or 0),
                        "buyer": str(row.get("BUYER_NAME") or ""),
                        "seller": str(row.get("SELLER_NAME") or ""),
                    }
                )
            result["records"] = records
            result["status"] = "ok" if records else "empty"
        except Exception as exc:
            result["errors"].append(f"block_trade:{type(exc).__name__}:{exc}")
            logger.warning("[block_trade] %s failed: %s", stock_code, exc)
        return result

    def get_holder_num_change(self, stock_code: str, count: int = 8) -> Dict[str, Any]:
        """股东户数变化（季度级）。

        Returns:
            {
                "status": "ok" | "empty" | "failed",
                "stock_code": str,
                "records": [
                    {
                        "date": str,            # 报告期（季末）
                        "holder_num": int,
                        "change_num": int,      # 环比变化数量
                        "change_ratio": float,  # 环比变化（%）
                        "avg_shares": float,    # 户均持股（股）
                    },
                    ...
                ],
                "trend": "decreasing" | "increasing" | "stable" | None,
                "source": "eastmoney_datacenter",
                "errors": [],
            }
        """
        from data_provider.eastmoney_http import em_datacenter

        result: Dict[str, Any] = {
            "status": "failed",
            "stock_code": stock_code,
            "records": [],
            "trend": None,
            "source": "eastmoney_datacenter",
            "errors": [],
        }
        try:
            rows = em_datacenter(
                "RPT_HOLDERNUMLATEST",
                filter_str=f'(SECURITY_CODE="{stock_code}")',
                page_size=count,
                sort_columns="END_DATE",
                sort_types="-1",
            )
            records = []
            for row in rows:
                records.append(
                    {
                        "date": str(row.get("END_DATE", ""))[:10],
                        "holder_num": int(row.get("HOLDER_NUM") or 0),
                        "change_num": int(row.get("HOLDER_NUM_CHANGE") or 0),
                        "change_ratio": float(row.get("HOLDER_NUM_RATIO") or 0),
                        "avg_shares": float(row.get("AVG_FREE_SHARES") or 0),
                    }
                )
            result["records"] = records
            result["status"] = "ok" if records else "empty"

            # Derive trend from most recent 3 periods
            if len(records) >= 2:
                recent_ratios = [r["change_ratio"] for r in records[:3] if r["change_ratio"] != 0]
                if recent_ratios:
                    avg = sum(recent_ratios) / len(recent_ratios)
                    if avg < -3:
                        result["trend"] = "decreasing"
                    elif avg > 3:
                        result["trend"] = "increasing"
                    else:
                        result["trend"] = "stable"
        except Exception as exc:
            result["errors"].append(f"holder_num:{type(exc).__name__}:{exc}")
            logger.warning("[holder_num] %s failed: %s", stock_code, exc)
        return result
