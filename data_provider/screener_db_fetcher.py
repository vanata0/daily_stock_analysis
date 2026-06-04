# -*- coding: utf-8 -*-
"""
ScreenerDBFetcher
=================

Local DuckDB 快速通道：读取 stock_screener 项目维护的 ``daily.duckdb``，
将 A 股前复权日线（``open_qfq / close_qfq``）作为最高优先级数据源。

优先级：-1（高于 TickFlowFetcher P0，最先被尝试）

配置（.env）：
  SCREENER_DAILY_DB   = /path/to/daily.duckdb
                        默认 ~/stock_screener/data/db/daily.duckdb
  SCREENER_DB_MAX_STALENESS_DAYS = 7   （允许数据落后几个自然日，默认 7）

stock_screener 的 daily.duckdb 每个交易日 16:30 后自动更新，正常运行时
延迟不超过当天（盘后 30 分钟内）。

数据不可用条件（任一满足即跳过，回落到下一数据源）：
  - duckdb 包未安装
  - 文件路径不存在
  - 所查询股票无数据
  - 最新日期落后超过 SCREENER_DB_MAX_STALENESS_DAYS 个自然日
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(
    Path(os.getenv("STOCK_DATA_DIR", str(Path.home() / "stock_new" / "data")))
    / "db"
    / "market.duckdb"
)
_MAX_STALENESS_DAYS_DEFAULT = 7


def _resolve_db_path() -> str:
    return os.getenv("SCREENER_DAILY_DB", _DEFAULT_DB_PATH)


def _max_staleness_days() -> int:
    try:
        return int(os.getenv("SCREENER_DB_MAX_STALENESS_DAYS", str(_MAX_STALENESS_DAYS_DEFAULT)))
    except ValueError:
        return _MAX_STALENESS_DAYS_DEFAULT


class ScreenerDBFetcher(BaseFetcher):
    """本地 DuckDB 前复权日线 K 线数据源（最高优先级）。

    stock_new 持有 market.duckdb 的 exclusive write lock 时，每次查询都会
    失败并降级到下一数据源（DuckDB 1.5.2 不允许跨进程的 read/write 混合）。
    """

    name = "ScreenerDBFetcher"
    priority = int(os.getenv("SCREENER_DB_PRIORITY", "-1"))

    def __init__(self) -> None:
        self._db_path = _resolve_db_path()
        self._available: Optional[bool] = None  # None = not yet checked

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        if self._available is False:
            return False
        if not Path(self._db_path).exists():
            if self._available is None:
                logger.info(
                    "[ScreenerDBFetcher] 文件不存在，跳过: %s", self._db_path
                )
            self._available = False
            return False
        try:
            import duckdb  # noqa: F401
        except ImportError:
            if self._available is None:
                logger.info("[ScreenerDBFetcher] duckdb 包未安装，跳过")
            self._available = False
            return False
        self._available = True
        return True

    # ------------------------------------------------------------------
    # Connection (per-query, read-only)
    # stock_new 持有 write lock 时本类连接会失败并降级，
    # 不持久持锁避免阻塞 stock_new 的写操作。
    # ------------------------------------------------------------------

    def _open_con(self):
        import duckdb
        return duckdb.connect(self._db_path, read_only=True)

    def close(self) -> None:
        # 保留接口兼容性，已无持久连接需要关闭
        pass

    def __del__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # BaseFetcher implementation
    # ------------------------------------------------------------------

    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        if not self.is_available:
            raise DataFetchError("ScreenerDBFetcher: not available")

        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            raise DataFetchError(
                f"ScreenerDBFetcher: 不支持的股票代码格式 '{stock_code}'"
            )

        try:
            con = self._open_con()
        except Exception as exc:
            # DuckDB lock conflict (stock_new has the file open exclusively)
            raise DataFetchError(
                f"ScreenerDBFetcher: 无法连接 DuckDB ({self._db_path}): {exc}"
            ) from exc

        try:
            df: pd.DataFrame = con.execute(
                """
                SELECT
                    code,
                    date::TEXT             AS date,
                    open_qfq               AS open,
                    high_qfq               AS high,
                    low_qfq                AS low,
                    close_qfq              AS close,
                    volume::DOUBLE         AS volume,
                    amount::DOUBLE         AS amount
                FROM stock_daily
                WHERE code = ?
                  AND date >= ?::DATE
                  AND date <= ?::DATE
                ORDER BY date
                """,
                [code, start_date, end_date],
            ).df()
        except Exception as exc:
            raise DataFetchError(
                f"ScreenerDBFetcher: DuckDB 查询失败 ({stock_code}): {exc}"
            ) from exc
        finally:
            try:
                con.close()
            except Exception:
                pass

        if df is None or df.empty:
            raise DataFetchError(
                f"ScreenerDBFetcher: {stock_code} 无数据 ({start_date}~{end_date})"
            )

        # Staleness check — latest row must be within N calendar days of today
        try:
            latest_str = df["date"].iloc[-1]
            latest = (
                latest_str
                if isinstance(latest_str, date)
                else datetime.strptime(str(latest_str)[:10], "%Y-%m-%d").date()
            )
            staleness = (date.today() - latest).days
            max_staleness = _max_staleness_days()
            if staleness > max_staleness:
                raise DataFetchError(
                    f"ScreenerDBFetcher: {stock_code} 数据过旧 "
                    f"(latest={latest}, staleness={staleness}d > {max_staleness}d)"
                )
            logger.debug(
                "[ScreenerDBFetcher] %s: %d 行, latest=%s, staleness=%dd",
                stock_code, len(df), latest, staleness,
            )
        except DataFetchError:
            raise
        except Exception as exc:
            logger.debug(
                "[ScreenerDBFetcher] %s: staleness 检查异常: %s", stock_code, exc
            )

        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """列名已在 SQL 里映射好，只需保留标准列。"""
        df = df.copy()
        df["code"] = normalize_stock_code(stock_code)
        keep = ["code"] + [c for c in STANDARD_COLUMNS if c in df.columns]
        return df[keep]
