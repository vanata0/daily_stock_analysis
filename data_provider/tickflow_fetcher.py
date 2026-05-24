# -*- coding: utf-8 -*-
"""
===================================
TickFlowFetcher
===================================

Supports two usage modes:

1. **Per-stock daily K-line** (participates in the standard DataFetcherManager
   pipeline).  Uses ``client.klines.get(symbol, period="1d", adjust="forward")``.
   Active when TICKFLOW_API_KEY is set; priority controlled by
   TICKFLOW_KLINE_PRIORITY (default 0, set 99 to disable).

2. **A-share market review** (called explicitly by DataFetcherManager).
   Uses ``client.quotes.get(symbols=...)`` for indices and optionally
   ``client.quotes.get(universes=["CN_Equity_A"])`` for market breadth.

Symbol format used by the TickFlow API: ``<6-digit-code>.<EXCHANGE>``
  e.g. 600519 → 600519.SH, 300766 → 300766.SZ, 920748 → 920748.BJ
"""

import logging
import math
import os
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Any, Dict, List, Optional

import pandas as pd

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    is_bse_code,
    is_kc_cy_stock,
    is_st_stock,
    normalize_stock_code,
)


logger = logging.getLogger(__name__)

_CN_MAIN_INDEX_QUOTES = (
    ("000001.SH", "000001", "上证指数"),
    ("399001.SZ", "399001", "深证成指"),
    ("399006.SZ", "399006", "创业板指"),
    ("000688.SH", "000688", "科创50"),
    ("000016.SH", "000016", "上证50"),
    ("000300.SH", "000300", "沪深300"),
)
_MAX_SYMBOLS_PER_QUOTE_REQUEST = 5
_UNIVERSE_PERMISSION_NEGATIVE_CACHE_TTL_SECONDS = 900


def _to_tickflow_symbol(stock_code: str) -> str:
    """Convert a normalized 6-digit A-share code to TickFlow symbol format.

    Rules (standard A-share exchange routing):
      92xxxx, 43xxxx, 81xxxx, 82xxxx, 83xxxx, 87xxxx, 88xxxx → .BJ (Beijing)
      6xxxxx, 900xxx (Shanghai B-share), 51xxxx, 52xxxx,
        56xxxx, 58xxxx                                        → .SH (Shanghai)
      everything else (0, 1, 2, 3, 15xxxx, 16xxxx, ...)      → .SZ (Shenzhen)
    """
    code = normalize_stock_code(stock_code)
    if not code or not code.isdigit() or len(code) != 6:
        raise DataFetchError(f"TickFlowFetcher: unsupported code format '{stock_code}'")
    if is_bse_code(code):
        suffix = "BJ"
    elif code[0] in ("5", "6", "7", "9"):
        # 5xxxxx: Shanghai ETFs (51/52/56/58xxxx)
        # 6xxxxx: Shanghai Main Board
        # 7xxxxx: Shanghai convertible bonds & rights issues
        # 9xxxxx: Shanghai B-shares (900xxx) and other SH instruments
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{code}.{suffix}"


def _date_to_ms(date_str: str) -> int:
    """Convert 'YYYY-MM-DD' to millisecond UTC timestamp (start of day)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class TickFlowFetcher(BaseFetcher):
    """TickFlow data source — daily K-line + market review."""

    name = "TickFlowFetcher"
    priority = int(os.getenv("TICKFLOW_KLINE_PRIORITY", "0"))

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        if api_key is None:
            api_key = os.getenv("TICKFLOW_API_KEY", "")
        self.priority = int(os.getenv("TICKFLOW_KLINE_PRIORITY", "0"))
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._client = None
        self._client_lock = RLock()
        self._universe_query_supported: Optional[bool] = None
        self._universe_query_checked_at: Optional[float] = None

    def close(self) -> None:
        """Close the underlying TickFlow client if it was created."""
        with self._client_lock:
            client = self._client
            self._client = None
            self._universe_query_supported = None
            self._universe_query_checked_at = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.debug("[TickFlowFetcher] 关闭客户端失败: %s", exc)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Best-effort cleanup during interpreter shutdown.
            pass

    def _build_client(self):
        from tickflow import TickFlow

        return TickFlow(api_key=self.api_key, timeout=self.timeout)

    def _get_client(self):
        if not self.api_key:
            return None
        if self._client is not None:
            return self._client

        with self._client_lock:
            if self._client is None:
                self._client = self._build_client()
            return self._client

    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Fetch daily OHLCV data via TickFlow klines API (forward-adjusted)."""
        client = self._get_client()
        if client is None:
            raise DataFetchError("TickFlowFetcher: no API key configured")

        try:
            symbol = _to_tickflow_symbol(stock_code)
        except DataFetchError:
            raise DataFetchError(
                f"TickFlowFetcher: cannot map '{stock_code}' to TickFlow symbol"
            )

        start_ms = _date_to_ms(start_date)
        # end_date is inclusive — add one day in ms so the last trading day
        # is included when the API uses exclusive upper bound.
        end_ms = _date_to_ms(end_date) + 86_400_000

        import time as _time
        t0 = _time.time()
        logger.info(
            "[TickFlowFetcher] 拉取日线 K 线: %s (%s ~ %s)",
            symbol, start_date, end_date,
        )
        try:
            df: pd.DataFrame = client.klines.get(
                symbol,
                period="1d",
                start_time=start_ms,
                end_time=end_ms,
                adjust="forward",
                as_dataframe=True,
            )
        except Exception as exc:
            elapsed = _time.time() - t0
            raise DataFetchError(
                f"TickFlowFetcher klines.get({symbol}) 失败 ({elapsed:.2f}s): {exc}"
            ) from exc

        elapsed = _time.time() - t0
        if df is None or df.empty:
            raise DataFetchError(
                f"TickFlowFetcher klines.get({symbol}) 返回空数据 ({elapsed:.2f}s)"
            )

        logger.info(
            "[TickFlowFetcher] 拉取完成: %s, %d 行, %.2fs",
            symbol, len(df), elapsed,
        )
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """Map TickFlow klines DataFrame columns to the project's standard schema.

        TickFlow returns a flat DataFrame with a RangeIndex and columns:
        symbol, name, timestamp (ms), trade_date (YYYY-MM-DD), trade_time,
        open, high, low, close, volume, amount.
        """
        df = df.copy()

        # Use trade_date as the 'date' column (already 'YYYY-MM-DD' strings)
        if "trade_date" in df.columns:
            df = df.rename(columns={"trade_date": "date"})
        elif "timestamp" in df.columns:
            # Fallback: derive date from millisecond timestamp (UTC)
            df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")

        # Add stock code
        df["code"] = normalize_stock_code(stock_code)

        # Keep only the standard columns that exist
        keep_cols = ["code"] + STANDARD_COLUMNS
        existing_cols = [c for c in keep_cols if c in df.columns]
        return df[existing_cols]

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value in (None, "", "-"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _ratio_to_percent(cls, value: Any) -> Optional[float]:
        ratio = cls._safe_float(value)
        if ratio is None:
            return None
        return ratio * 100.0

    @staticmethod
    def _extract_name(quote: Dict[str, Any]) -> str:
        ext = quote.get("ext") or {}
        name = ext.get("name") or quote.get("name") or ""
        return str(name).strip()

    @staticmethod
    def _is_universe_permission_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        code = str(getattr(exc, "code", "") or "").upper()
        message = (
            f"{getattr(exc, 'message', '')} {exc}"
        ).strip().lower()

        if status_code == 403:
            return True
        if code in {"PERMISSION_DENIED", "FORBIDDEN"}:
            return True
        return any(
            keyword in message
            for keyword in (
                "标的池查询",
                "universe",
                "permission",
                "forbidden",
            )
        )

    @staticmethod
    def _is_cn_equity_symbol(symbol: str) -> bool:
        normalized = normalize_stock_code(symbol)
        upper_symbol = (symbol or "").strip().upper()
        return (
            normalized.isdigit()
            and len(normalized) == 6
            and upper_symbol.endswith((".SH", ".SZ", ".BJ"))
        )

    @staticmethod
    def _round_limit_price(prev_close: float, ratio: float) -> float:
        return math.floor(prev_close * (1 + ratio) * 100 + 0.5) / 100.0

    @classmethod
    def _get_limit_ratio(cls, pure_code: str, name: str) -> float:
        if is_bse_code(pure_code):
            return 0.30
        if is_kc_cy_stock(pure_code):
            return 0.20
        if is_st_stock(name):
            return 0.05
        return 0.10

    def get_main_indices(self, region: str = "cn") -> Optional[List[Dict[str, Any]]]:
        """Fetch main A-share indices via TickFlow quotes."""
        if region != "cn":
            return None

        client = self._get_client()
        if client is None:
            return None

        symbols = [symbol for symbol, _, _ in _CN_MAIN_INDEX_QUOTES]
        quotes: List[Dict[str, Any]] = []
        for offset in range(0, len(symbols), _MAX_SYMBOLS_PER_QUOTE_REQUEST):
            batch_symbols = symbols[offset : offset + _MAX_SYMBOLS_PER_QUOTE_REQUEST]
            batch_quotes = client.quotes.get(symbols=batch_symbols)
            if batch_quotes:
                quotes.extend(batch_quotes)
        if not quotes:
            logger.warning("[TickFlowFetcher] 指数行情为空")
            return None

        quotes_by_symbol = {
            str(item.get("symbol", "")).upper(): item for item in quotes if item
        }
        results: List[Dict[str, Any]] = []

        for symbol, code, name in _CN_MAIN_INDEX_QUOTES:
            quote = quotes_by_symbol.get(symbol)
            if not quote:
                continue

            ext = quote.get("ext") or {}
            current = self._safe_float(quote.get("last_price")) or 0.0
            prev_close = self._safe_float(quote.get("prev_close")) or 0.0
            change = self._safe_float(ext.get("change_amount"))
            if change is None:
                change = current - prev_close if current or prev_close else 0.0
            amplitude = self._ratio_to_percent(ext.get("amplitude"))
            if amplitude is None and prev_close > 0:
                high = self._safe_float(quote.get("high")) or 0.0
                low = self._safe_float(quote.get("low")) or 0.0
                amplitude = (high - low) / prev_close * 100

            results.append(
                {
                    "code": code,
                    "name": name,
                    "current": current,
                    "change": change,
                    "change_pct": self._ratio_to_percent(ext.get("change_pct")) or 0.0,
                    "open": self._safe_float(quote.get("open")) or 0.0,
                    "high": self._safe_float(quote.get("high")) or 0.0,
                    "low": self._safe_float(quote.get("low")) or 0.0,
                    "prev_close": prev_close,
                    "volume": self._safe_float(quote.get("volume")) or 0.0,
                    "amount": self._safe_float(quote.get("amount")) or 0.0,
                    "amplitude": amplitude or 0.0,
                }
            )

        if len(results) != len(_CN_MAIN_INDEX_QUOTES):
            logger.warning(
                "[TickFlowFetcher] 指数行情不完整: %s/%s",
                len(results),
                len(_CN_MAIN_INDEX_QUOTES),
            )
            return None

        return results or None

    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """Calculate A-share market breadth from TickFlow universe quotes."""
        client = self._get_client()
        if client is None:
            return None

        now = monotonic()
        if self._universe_query_supported is False:
            checked_at = self._universe_query_checked_at or 0.0
            if (
                now - checked_at
                < _UNIVERSE_PERMISSION_NEGATIVE_CACHE_TTL_SECONDS
            ):
                return None
            self._universe_query_supported = None
            self._universe_query_checked_at = None

        try:
            quotes = client.quotes.get(universes=["CN_Equity_A"])
            self._universe_query_supported = True
            self._universe_query_checked_at = now
        except Exception as exc:
            if self._is_universe_permission_error(exc):
                self._universe_query_supported = False
                self._universe_query_checked_at = now
                logger.info(
                    "[TickFlowFetcher] 当前套餐不支持标的池查询，市场统计回退到现有数据源"
                )
                return None
            raise
        if not quotes:
            logger.warning("[TickFlowFetcher] 市场统计行情为空")
            return None

        stats = {
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "total_amount": 0.0,
        }
        valid_rows = 0

        for quote in quotes:
            if not quote:
                continue

            symbol = str(quote.get("symbol") or "").strip().upper()
            if not self._is_cn_equity_symbol(symbol):
                continue

            amount = self._safe_float(quote.get("amount"))
            if amount is not None and amount > 0:
                stats["total_amount"] += amount / 1e8

            pure_code = normalize_stock_code(symbol)
            last_price = self._safe_float(quote.get("last_price"))
            prev_close = self._safe_float(quote.get("prev_close"))

            if last_price is None or prev_close is None or amount is None or amount <= 0:
                continue

            name = self._extract_name(quote)
            if not name:
                logger.debug("[TickFlowFetcher] 缺少股票名称，按非 ST 处理: %s", symbol)

            ratio = self._get_limit_ratio(pure_code, name)
            limit_up = self._round_limit_price(prev_close, ratio)
            limit_down = math.floor(prev_close * (1 - ratio) * 100 + 0.5) / 100.0
            limit_up_tolerance = round(abs(prev_close * (1 + ratio) - limit_up), 10)
            limit_down_tolerance = round(
                abs(prev_close * (1 - ratio) - limit_down), 10
            )

            valid_rows += 1

            if abs(last_price - limit_up) <= limit_up_tolerance:
                stats["limit_up_count"] += 1
            if abs(last_price - limit_down) <= limit_down_tolerance:
                stats["limit_down_count"] += 1

            if last_price > prev_close:
                stats["up_count"] += 1
            elif last_price < prev_close:
                stats["down_count"] += 1
            else:
                stats["flat_count"] += 1

        if valid_rows == 0:
            logger.warning("[TickFlowFetcher] 市场统计未命中有效 A 股行情")
            return None

        return stats
