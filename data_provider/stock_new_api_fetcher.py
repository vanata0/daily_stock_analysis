# -*- coding: utf-8 -*-
"""
StockNewAPIFetcher
=================

通过本地 stock_new 项目的 HTTP API（/api/data/kline/{code}）获取
A 股前复权日线 K 线数据，作为最高优先级数据源。

优先级：-1（高于所有网络数据源，最先被尝试）

配置（.env）：
  STOCK_NEW_API_URL = http://localhost:8001
                      （stock_new 项目的 API 地址，默认 8001）
  STOCK_NEW_API_TIMEOUT = 3   （HTTP 请求超时秒数，默认 3）
  STOCK_NEW_API_MAX_COUNT = 500   （单次请求最多条数，上限 500，默认 500）

数据不可用条件（任一满足即跳过，回落到下一数据源）：
  - requests 包未安装
  - stock_new API 不可达（超时 / 连接拒绝）
  - 所查询股票无数据
  - 返回数据最新日期落后超过 SCREENER_DB_MAX_STALENESS_DAYS 个自然日
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code

logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "http://localhost:8001"
_DEFAULT_TIMEOUT = 3
_DEFAULT_MAX_COUNT = 500
_MAX_STALENESS_DAYS_DEFAULT = 7


def _api_url() -> str:
    return os.getenv("STOCK_NEW_API_URL", _DEFAULT_API_URL).rstrip("/")


def _api_timeout() -> int:
    try:
        return int(os.getenv("STOCK_NEW_API_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except ValueError:
        return _DEFAULT_TIMEOUT


def _max_count() -> int:
    try:
        v = int(os.getenv("STOCK_NEW_API_MAX_COUNT", str(_DEFAULT_MAX_COUNT)))
        return max(20, min(v, 500))
    except ValueError:
        return _DEFAULT_MAX_COUNT


def _max_staleness_days() -> int:
    try:
        return int(os.getenv("SCREENER_DB_MAX_STALENESS_DAYS", str(_MAX_STALENESS_DAYS_DEFAULT)))
    except ValueError:
        return _MAX_STALENESS_DAYS_DEFAULT


class StockNewAPIFetcher(BaseFetcher):
    """通过 stock_new 本地 HTTP API 获取前复权日线 K 线（最高优先级）。"""

    name = "StockNewAPIFetcher"
    priority = int(os.getenv("STOCK_NEW_API_PRIORITY", "-1"))

    def __init__(self) -> None:
        self._base_url = _api_url()
        self._available: Optional[bool] = None  # None = not yet checked

    # ------------------------------------------------------------------
    # Availability — lazy probe, cached per process lifetime
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import requests  # noqa: F401
        except ImportError:
            logger.info("[StockNewAPIFetcher] requests 包未安装，跳过")
            self._available = False
            return False

        # Quick probe: hit /api/data/status with short timeout
        import requests
        try:
            resp = requests.get(
                f"{self._base_url}/api/data/status",
                timeout=_api_timeout(),
            )
            if resp.status_code < 500:
                self._available = True
                return True
            logger.info(
                "[StockNewAPIFetcher] API 探测失败 status=%d, 跳过", resp.status_code
            )
            self._available = False
            return False
        except Exception as exc:
            logger.info("[StockNewAPIFetcher] API 不可达 (%s)，跳过", exc)
            self._available = False
            return False

    # ------------------------------------------------------------------
    # BaseFetcher implementation
    # ------------------------------------------------------------------

    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        # Re-check availability each call (in case server started/stopped)
        # but reuse cached result to avoid repeated probes in bulk runs
        if not self.is_available:
            raise DataFetchError("StockNewAPIFetcher: not available")

        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            raise DataFetchError(
                f"StockNewAPIFetcher: 不支持的股票代码格式 '{stock_code}'"
            )

        import requests

        count = _max_count()
        url = f"{self._base_url}/api/data/kline/{code}?count={count}"

        try:
            resp = requests.get(url, timeout=_api_timeout())
            resp.raise_for_status()
            records = resp.json()
        except Exception as exc:
            raise DataFetchError(
                f"StockNewAPIFetcher: HTTP 请求失败 ({stock_code}): {exc}"
            ) from exc

        if not records:
            raise DataFetchError(
                f"StockNewAPIFetcher: {stock_code} 无数据"
            )

        df_full = pd.DataFrame(records)

        # Staleness check on the FULL dataset (before date-range filter),
        # so that historical range requests don't get incorrectly flagged as stale.
        try:
            latest_str = df_full["date"].iloc[-1]
            latest_full = (
                latest_str
                if isinstance(latest_str, date)
                else datetime.strptime(str(latest_str)[:10], "%Y-%m-%d").date()
            )
            staleness = (date.today() - latest_full).days
            max_staleness = _max_staleness_days()
            if staleness > max_staleness:
                raise DataFetchError(
                    f"StockNewAPIFetcher: {stock_code} 数据过旧 "
                    f"(latest={latest_full}, staleness={staleness}d > {max_staleness}d)"
                )
            logger.debug(
                "[StockNewAPIFetcher] %s: full_latest=%s, staleness=%dd",
                stock_code, latest_full, staleness,
            )
        except DataFetchError:
            raise
        except Exception as exc:
            logger.debug(
                "[StockNewAPIFetcher] %s: staleness 检查异常: %s", stock_code, exc
            )

        # Filter by requested date range (after staleness check on full dataset)
        df = df_full[
            (df_full["date"] >= start_date[:10]) & (df_full["date"] <= end_date[:10])
        ].copy()

        if df.empty:
            raise DataFetchError(
                f"StockNewAPIFetcher: {stock_code} 在 {start_date}~{end_date} 无数据"
            )

        logger.debug(
            "[StockNewAPIFetcher] %s: %d 行 (%s~%s)",
            stock_code, len(df), start_date[:10], end_date[:10],
        )
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """列名已与 STANDARD_COLUMNS 对齐，只需保留标准列并补充 code。"""
        df = df.copy()
        df["code"] = normalize_stock_code(stock_code)
        keep = ["code"] + [c for c in STANDARD_COLUMNS if c in df.columns]
        return df[keep]
