# -*- coding: utf-8 -*-
"""
KplFetcher —— 开盘啦（KPL）数据源
================================

通过本机常驻的 kpl-unified-client HTTP 服务获取 A 股数据，作为 Tushare
代理站下线后的主力替代源。

优先级：由 ``KPL_PRIORITY`` 控制，默认 -2（启用后领先 Tushare 接管 A 股行情）。

配置（.env）：
  KPL_ENABLED   = false                    是否启用；关闭时本 fetcher 完全不实例化
  KPL_API_BASE  = http://127.0.0.1:8010    kpl-unified-client 服务地址
  KPL_PRIORITY  = -2                       数据源优先级，越小越先尝试
  KPL_TIMEOUT   = 10                       单次请求超时秒数

不可用条件（任一满足即整体退出调用链，回落到其它数据源）：
  - KPL_ENABLED 未开启
  - 服务不可达
  - 凭证失效探针判定失败（详见 kpl_http.KplHttpClient.is_credential_valid）

日线口径说明：
  上游 /kline/daily/{id} 无参数、返回全量序列且**已前复权**，可直接用于技术
  指标计算；但不提供原始未复权价。volume 按「手」计，本模块按 DSA 标准换算
  为「股」；amount 已是「元」，不做换算。上游不返回涨跌幅，由收盘价推导。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    normalize_stock_code,
)
from .kpl_http import KplError, KplHttpClient, kpl_date_to_iso
from .realtime_types import ChipDistribution, RealtimeSource, UnifiedRealtimeQuote

logger = logging.getLogger(__name__)

# 上游 volume 按「手」计，DSA 标准列按「股」
_HAND_TO_SHARE = 100

# 涨停池按连板数分档查询，实测 5 板及以上通常为空，逐档聚合到此上限
_MAX_CONSECUTIVE_BOARD = 8

# 资金流最多回溯的交易日数
_MAX_FLOW_DAYS = 10
# 为跳过休市多探的日历日余量。10 个交易日最坏要跨 4 个周末(8 天)加一次春节
# 连休(约 9 天)，即约 27 个日历日；余量取 20 让最坏情况仍有富裕，多探的日子
# 只会命中空响应并被跳过，成本是几次本地 HTTP 调用。
_FLOW_LOOKBACK_SLACK = 20


class KplFetcher(BaseFetcher):
    """开盘啦数据源（仅 A 股）。"""

    name = "KplFetcher"
    priority = -2

    def __init__(
        self,
        api_base: Optional[str] = None,
        timeout: Optional[int] = None,
        priority: Optional[int] = None,
        client: Optional[KplHttpClient] = None,
    ) -> None:
        """构造 KplFetcher。

        Args:
            api_base: 服务地址；缺省读取全局配置
            timeout: 单次请求超时秒数；缺省读取全局配置
            priority: 数据源优先级；缺省读取全局配置
            client: 注入自定义 HTTP 客户端（测试用）
        """
        config = self._safe_config()

        resolved_base = api_base or getattr(config, "kpl_api_base", None)
        resolved_timeout = timeout if timeout is not None else getattr(config, "kpl_timeout", None)
        resolved_priority = priority if priority is not None else getattr(config, "kpl_priority", None)

        self._client = client or KplHttpClient(
            base_url=resolved_base or "http://127.0.0.1:8010",
            timeout=resolved_timeout or 10,
            on_credential_expired=_notify_credential_expired,
        )
        self.priority = resolved_priority if resolved_priority is not None else -2

    @staticmethod
    def _safe_config() -> Any:
        """读取全局配置；失败时返回 None，由调用方走默认值。"""
        try:
            from src.config import get_config

            return get_config()
        except Exception as exc:  # pragma: no cover - 配置缺失时不应阻断构造
            logger.debug("[KPL] 读取全局配置失败，改用默认值: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """数据源是否可用。

        必须是普通方法而不是 property —— DataFetcherManager 的可用性探测用
        ``callable()`` 判定，写成 property 会让这里的检查被静默跳过。
        """
        return self._client.is_credential_valid()

    # ------------------------------------------------------------------
    # BaseFetcher 实现：日线
    # ------------------------------------------------------------------

    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        if not self.is_available():
            raise DataFetchError("KplFetcher: 数据源不可用（凭证探针未通过或服务不可达）")

        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            raise DataFetchError(f"KplFetcher: 仅支持 A 股 6 位代码，收到 '{stock_code}'")

        try:
            payload = self._client.get(f"/kline/daily/{code}")
        except KplError as exc:
            raise DataFetchError(f"KplFetcher: 获取日线失败 ({stock_code}): {exc}") from exc

        days: List[Dict[str, Any]] = payload.get("days") or []
        if not days:
            raise DataFetchError(f"KplFetcher: {stock_code} 无日线数据")

        df = pd.DataFrame(days)
        df["date"] = df["date"].map(kpl_date_to_iso)
        df = df[df["date"].notna()]
        if df.empty:
            raise DataFetchError(f"KplFetcher: {stock_code} 日线日期全部无法解析")

        # 上游返回全量序列且不支持日期参数，这里按请求区间裁剪
        df = df[(df["date"] >= start_date[:10]) & (df["date"] <= end_date[:10])].copy()
        if df.empty:
            raise DataFetchError(
                f"KplFetcher: {stock_code} 在 {start_date[:10]}~{end_date[:10]} 无日线数据"
            )

        logger.debug(
            "[KplFetcher] %s: %d 行 (%s~%s)",
            stock_code, len(df), start_date[:10], end_date[:10],
        )
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """转成 DSA 标准列。

        上游字段：date, open, close, high, low, volume(手), amount(元),
                  turnover_pct, rights_event, state, state1, state_zt
        标准列：  date, open, high, low, close, volume(股), amount(元), pct_chg
        """
        df = df.copy()

        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "volume" in df.columns:
            df["volume"] = df["volume"] * _HAND_TO_SHARE

        # 上游不返回涨跌幅，由收盘价推导（首行无前收，保持 NaN 由后续清洗处理）
        if "close" in df.columns:
            df = df.sort_values("date")
            df["pct_chg"] = df["close"].pct_change() * 100

        df["code"] = normalize_stock_code(stock_code)
        keep = ["code"] + [c for c in STANDARD_COLUMNS if c in df.columns]
        return df[keep]

    # ------------------------------------------------------------------
    # 实时行情
    # ------------------------------------------------------------------

    def get_realtime_quote(self, stock_code: str, **_kwargs) -> Optional[UnifiedRealtimeQuote]:
        """获取 A 股实时行情。

        数据来自 /orderbook/{id}，是 KPL 单股字段最全的一条：除常规量价外
        还带 PE/PB、总市值/流通市值、涨跌停价与五档盘口。

        Returns:
            UnifiedRealtimeQuote；不可用或数据无效时返回 None（由上层降级）
        """
        if not self.is_available():
            logger.debug("[KplFetcher] 数据源不可用，跳过实时行情 %s", stock_code)
            return None

        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            logger.debug("[KplFetcher] 实时行情仅支持 A 股 6 位代码，收到 %r", stock_code)
            return None

        try:
            data = self._client.get(f"/orderbook/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 实时行情获取失败 %s: %s", stock_code, exc)
            return None

        price = _to_float(data.get("last"))
        if price is None or price <= 0:
            logger.debug("[KplFetcher] %s 实时行情无有效价格，跳过", stock_code)
            return None

        # ⚠️ orderbook 没有 volume 字段；实测 amount_wan 并非「成交额(万元)」而是
        # 「成交量(手)」——5 只标的上它与 /kline/daily 的 volume 逐一相等，且
        # turnover/(amount_wan*100) 均落在当日 low~high 内。字段名有误导性。
        volume_hand = _to_float(data.get("amount_wan"))
        quote = UnifiedRealtimeQuote(
            code=code,
            name=str(data.get("name") or "").strip(),
            source=RealtimeSource.KPL,
            market="cn",
            price=price,
            change_pct=_to_float(data.get("change_pct")),
            change_amount=_to_float(data.get("change")),
            volume=int(volume_hand * _HAND_TO_SHARE) if volume_hand else None,
            amount=_to_float(data.get("turnover")),
            volume_ratio=_to_float(data.get("vol_ratio")),
            turnover_rate=_to_float(data.get("turnover_ratio")),
            amplitude=_to_float(data.get("amplitude")),
            open_price=_to_float(data.get("open")),
            high=_to_float(data.get("high")),
            low=_to_float(data.get("low")),
            pre_close=_to_float(data.get("preclose")),
            # 优先 TTM 市盈率，缺失时退回静态市盈率
            pe_ratio=_to_float(data.get("ttm_pe")) or _to_float(data.get("pe")),
            pb_ratio=_to_float(data.get("pb")),
            total_mv=_to_float(data.get("total_mv")),
            circ_mv=_to_float(data.get("circ_mv")),
        )
        logger.debug(
            "[KplFetcher] %s 实时行情: price=%s pe=%s pb=%s",
            stock_code, quote.price, quote.pe_ratio, quote.pb_ratio,
        )
        return quote

    # ------------------------------------------------------------------
    # 个股辅助信息
    # ------------------------------------------------------------------

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """获取股票名称。"""
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None
        try:
            data = self._client.get(f"/orderbook/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取股票名称失败 %s: %s", stock_code, exc)
            return None
        name = str(data.get("name") or "").strip()
        return name or None

    def get_research_reports(
        self, stock_code: str, max_count: int = 10
    ) -> List[Dict[str, Any]]:
        """获取券商研报列表。

        用 /research/research-field-list（50 条，带标题）而不是
        /research/research-field-excel——后者只有评级分布与 3 条明细且没有
        title，填不满 DSA 的研报条目契约。

        返回结构与 TushareFetcher.get_research_reports 对齐，便于
        research_report_fetcher 的降级链无差别消费；失败返回 []（fail-open）。
        """
        if not self.is_available():
            return []
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return []

        try:
            payload = self._client.get(f"/research/research-field-list/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取研报失败 %s: %s", stock_code, exc)
            return []

        records: List[Dict[str, Any]] = []
        seen = set()
        for item in payload.get("items") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            date = _epoch_to_date(item.get("timestamp")) or ""
            key = (date, title)
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "title": title,
                "date": date,
                "broker": str(item.get("broker") or "").strip(),
                "rating": str(item.get("rating") or "").strip(),
                "abstract": "",
                "analyst": "",
                "eps": None,
                "classify": "",
            })
            if len(records) >= max_count:
                break

        logger.info("[KplFetcher] 研报获取成功 %s: %d 条", stock_code, len(records))
        return records

    def get_capital_flow(
        self, stock_code: str, days: int = 10
    ) -> Optional[Dict[str, Any]]:
        """获取个股主力资金流（当日实时 + 多日累计）。

        当日主力买/卖/净优先取 ``/big-money/stock-dp-realdata`` 的实时快照；
        多日累计逐交易日调用 ``/big-money/chouma-history?day=`` 取历史主力买卖额。

        端点选择依据（实测）：``/big-money/main-monitor-trend`` 虽然声明了
        ``date`` 参数，但传任何日期都返回同一份当日数据（4 种格式验证过），
        用它做多日累计会把同一天叠加 N 遍；``chouma-history`` 的 ``day``
        真实生效，同标的不同日期返回不同数值。``stock-dp-realdata`` 只给
        当日快照，不能替代 ``chouma-history`` 做多日累计。

        上游语义（2026-07-26 实测确认）：
          - ``items`` 是当日 09:30~15:00 每 5 分钟一条的**累计值**（49 条），
            ``main_buy`` 单调递增，故取 ``items[-1]`` 而非求和。末条与
            ``/big-money/stock-dp-realdata`` 的当日值逐位一致。
          - ``main_sell`` 上游已带负号，净额是相加而非相减。
          - ⚠️ 同结构里的 ``net_all`` 是**另一个口径**，与 ``main_buy+main_sell``
            符号常相反（000977 收盘 -1.36 亿 vs net_all +3.36 亿），不可混用。
          - 历史深度实测回溯到 T-400 仍完整；非交易日返回空 items，被跳过。
          - 盘中调用拿到的是当时的运行累计值，不是全天终值。

        Returns:
            {"main_buy", "main_sell", "main_net_inflow",
             "inflow_5d", "inflow_10d", "trade_date"}，金额单位为元；
            无数据返回 None（由上层降级）
        """
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None

        wanted = max(1, min(days, _MAX_FLOW_DAYS))
        daily: List[float] = []
        latest_buy: Optional[float] = None
        latest_sell: Optional[float] = None
        latest_day: Optional[str] = None
        realtime = self._get_capital_flow_realtime(code)
        if realtime is not None:
            daily.append(realtime["main_net"])
            latest_buy = realtime["main_buy"]
            latest_sell = realtime["main_sell"]
            latest_day = realtime["trade_date"]

        cursor = date.today() - timedelta(days=1 if realtime is not None else 0)
        # 上游按自然日索引，非交易日返回空；这里往前多探一些日历日以凑够交易日
        for _ in range(wanted * 2 + _FLOW_LOOKBACK_SLACK):
            if len(daily) >= wanted:
                break
            day_str = cursor.strftime("%Y%m%d")
            cursor -= timedelta(days=1)
            if latest_day and day_str == latest_day:
                continue
            try:
                payload = self._client.get(
                    f"/big-money/chouma-history/{code}", params={"day": day_str}
                )
            except KplError as exc:
                logger.debug("[KplFetcher] 资金流 %s %s 取数失败: %s", stock_code, day_str, exc)
                continue

            items = payload.get("items") or []
            if not items:
                continue
            last = items[-1]
            buy = _to_float(last.get("main_buy"))
            sell = _to_float(last.get("main_sell"))
            if buy is None and sell is None:
                continue
            # ⚠️ 主力买卖同时为 0 但当日有成交，说明该标的没有主力资金覆盖，
            # 不是「净流入为零」。实测北交所 920689 当日成交 1450 万
            # （turnover 与日线 amount 逐位一致）而 main_buy/main_sell 全为 0。
            # 当作真值 0 会用「零流入」冒充「无数据」，让下游误判为中性而不是
            # 回落到其它数据源。
            if not buy and not sell and _to_float(last.get("turnover")):
                logger.debug(
                    "[KplFetcher] %s %s 有成交但无主力资金覆盖，跳过该日",
                    stock_code, day_str,
                )
                continue
            # 上游 main_sell 已是负值，直接相加即为净额
            daily.append((buy or 0.0) + (sell or 0.0))
            if latest_day is None:
                latest_day = day_str
                latest_buy = buy if buy is not None else 0.0
                latest_sell = sell if sell is not None else 0.0

        if not daily:
            logger.debug("[KplFetcher] %s 无资金流数据", stock_code)
            return None

        result = {
            "main_buy": latest_buy,
            "main_sell": latest_sell,
            # 净额只用 main_net_inflow 一个键：Mairui / AkShare 兜底路径也产这个键，
            # 再加一个同值的 main_net 会在降级时变成「一个有值一个 None」的噪音
            "main_net_inflow": daily[0],
            "inflow_5d": sum(daily[:5]) if len(daily) >= 5 else None,
            "inflow_10d": sum(daily[:10]) if len(daily) >= 10 else None,
            "trade_date": latest_day or "",
        }
        logger.info(
            "[KplFetcher] 资金流 %s: 当日=%.0f 覆盖%d个交易日",
            stock_code, result["main_net_inflow"], len(daily),
        )
        return result

    def _get_capital_flow_realtime(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """获取 KPL 当日主力资金实时快照。

        ``/big-money/stock-dp-realdata`` 的服务器键 ZLBuy/ZLSell/ZLJE 直接对应
        main_buy/main_sell/main_net，算术关系已由 kpl-unified-client 逐位锁死。
        该端点只返回当日快照，不能替代 chouma-history 做历史累计。
        """
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None

        try:
            data = self._client.get(f"/big-money/stock-dp-realdata/{code}")
        except KplError as exc:
            logger.debug("[KplFetcher] 当日资金流实时快照 %s 获取失败: %s", stock_code, exc)
            return None

        buy = _to_float(data.get("main_buy"))
        sell = _to_float(data.get("main_sell"))
        if buy is None and sell is None:
            logger.debug("[KplFetcher] %s 当日资金流实时快照无有效买卖额", stock_code)
            return None
        if not buy and not sell and _to_float(data.get("turnover")):
            logger.debug(
                "[KplFetcher] %s 有成交但无主力资金覆盖，跳过实时快照",
                stock_code,
            )
            return None

        trade_date = None
        try:
            orderbook = self._client.get(f"/orderbook/{code}")
            trade_date = str(orderbook.get("trade_date") or "").strip() or None
        except KplError as exc:
            logger.debug("[KplFetcher] 当日资金流实时快照读取交易日失败: %s", exc)

        return {
            "main_buy": buy if buy is not None else 0.0,
            "main_sell": sell if sell is not None else 0.0,
            "main_net": (buy or 0.0) + (sell or 0.0),
            "trade_date": trade_date or date.today().strftime("%Y%m%d"),
        }

    def get_auction_context(
        self,
        stock_code: str,
    ) -> Optional[Dict[str, Any]]:
        """获取前日盘后、当日盘前与当日盘后的竞价聚合上下文。

        前日盘后优先取 ``/kline/stock-fenbi2`` 的历史分笔记录（该接口虽然
        不是专门的盘后接口，但实测包含 15:05~15:29 的盘后切片，且有
        volume/amount）；当日 15:30 后另取
        ``/market-stats/afterhours-auction-trend`` 作为当日盘后。盘前来自
        ``/auction/stock-bid``，是当日 09:15~09:25 集合竞价序列的聚合摘要。
        三者都以摘要返回，不把原始逐条数据直接交给 LLM。

        Returns:
            None 或 {"stock_code", "prev_afterhours", "today_premarket",
            "today_afterhours"}；单个会话不可用时对应键为空 dict，不影响另一侧。
        """
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None

        current_afterhours = self._get_afterhours_auction_summary(code)
        prev_afterhours = self._get_historical_afterhours_auction_summary(code)
        premarket = self._get_premarket_auction_summary(code)
        today_str = date.today().strftime("%Y%m%d")
        today_afterhours = None
        if (
            current_afterhours
            and current_afterhours.get("session_date") == today_str
            and self._is_afterhours_complete()
        ):
            today_afterhours = current_afterhours
        elif current_afterhours and prev_afterhours is None:
            prev_afterhours = current_afterhours

        if not prev_afterhours and not premarket and not today_afterhours:
            logger.debug("[KplFetcher] %s 无盘前/盘后有效数据", code)
            return None

        return {
            "stock_code": code,
            "prev_afterhours": prev_afterhours or {},
            "today_premarket": premarket or {},
            "today_afterhours": today_afterhours or {},
        }

    @staticmethod
    def _is_afterhours_complete() -> bool:
        now = datetime.now()
        return (now.hour, now.minute) >= (15, 30)

    def _get_historical_afterhours_auction_summary(
        self,
        code: str,
    ) -> Optional[Dict[str, Any]]:
        """从历史分笔接口回找最近一个含盘后 15:05~15:29 记录的交易会话。"""
        cursor = date.today()
        for _ in range(10):
            day_str = cursor.strftime("%Y%m%d")
            cursor -= timedelta(days=1)
            try:
                data = self._client.get(
                    f"/kline/stock-fenbi2/{code}",
                    params={"day": day_str},
                )
            except KplError as exc:
                logger.debug("[KplFetcher] %s %s 历史分笔获取失败: %s", code, day_str, exc)
                continue
            items = data.get("items") if isinstance(data.get("items"), list) else []
            afterhours = [
                item
                for item in items
                if isinstance(item, dict)
                and "15:05" <= str(item.get("time") or "") < "15:30"
            ]
            if not afterhours:
                continue
            return self._aggregate_fenbi_afterhours(day_str, afterhours)
        logger.debug("[KplFetcher] %s 最近 10 个自然日无历史盘后分笔", code)
        return None

    @staticmethod
    def _aggregate_fenbi_afterhours(
        day_str: str,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        total_volume_hand = 0.0
        total_amount = 0.0
        buy_volume_hand = 0.0
        sell_volume_hand = 0.0
        active_records = 0
        last_price: Optional[float] = None
        for item in items:
            price = _to_float(item.get("price"))
            if price is not None:
                last_price = price
            volume_hand = _to_float(item.get("volume_hand")) or 0.0
            amount = _to_float(item.get("amount")) or 0.0
            if volume_hand > 0:
                active_records += 1
                total_volume_hand += volume_hand
                total_amount += amount
            trade_side = _to_int(item.get("trade_side"))
            if trade_side == 1:
                buy_volume_hand += volume_hand
            elif trade_side == 0:
                sell_volume_hand += volume_hand
        return {
            "session_date": day_str,
            "source": "stock_fenbi2",
            "record_count": len(items),
            "active_record_count": active_records,
            "price": last_price,
            "total_volume_hand": round(total_volume_hand, 2),
            "total_amount": round(total_amount, 2),
            "buy_volume_hand": round(buy_volume_hand, 2),
            "sell_volume_hand": round(sell_volume_hand, 2),
        }

    def _get_afterhours_auction_summary(
        self,
        code: str,
    ) -> Optional[Dict[str, Any]]:
        """聚合 KPL 盘后固定价格交易 15:05~15:30 的有效数据。"""
        try:
            data = self._client.get(
                "/market-stats/afterhours-auction-trend",
                params={"stock_id": code},
            )
        except KplError as exc:
            logger.debug("[KplFetcher] 盘后竞价 %s 获取失败: %s", code, exc)
            return None

        records = data.get("records") if isinstance(data.get("records"), list) else []
        if not records:
            logger.debug("[KplFetcher] %s 盘后竞价无记录", code)
            return None

        total_volume_hand = 0.0
        total_amount = 0.0
        active_records = 0
        peak_buy_unmatched_hand = 0.0
        peak_sell_unmatched_hand = 0.0
        last_price: Optional[float] = None

        def raw_float(raw: Any, key: str) -> Optional[float]:
            if not isinstance(raw, dict):
                return None
            value = raw.get(key)
            if value is None:
                value = raw.get(int(key))
            return _to_float(value)

        for record in records:
            if not isinstance(record, dict):
                continue
            raw = record.get("raw_fields")
            price = _to_float(record.get("price"))
            if price is not None:
                last_price = price
            volume_hand = raw_float(raw, "5")
            if volume_hand is not None:
                if volume_hand > 0:
                    active_records += 1
                    total_volume_hand += volume_hand
                    if price is not None and price > 0:
                        total_amount += volume_hand * price * 100
            buy_unmatched = raw_float(raw, "3") or 0.0
            sell_unmatched = raw_float(raw, "4") or 0.0
            peak_buy_unmatched_hand = max(peak_buy_unmatched_hand, buy_unmatched)
            peak_sell_unmatched_hand = max(peak_sell_unmatched_hand, sell_unmatched)

        if active_records == 0 and peak_buy_unmatched_hand <= 0 and peak_sell_unmatched_hand <= 0:
            logger.debug("[KplFetcher] %s 盘后竞价无有效成交或挂单", code)
            return None

        last = records[-1]
        last_raw = last.get("raw_fields") if isinstance(last, dict) else None
        return {
            "session_date": str(data.get("date") or "").strip() or None,
            "record_count": len(records),
            "active_record_count": active_records,
            "price": last_price,
            "total_volume_hand": round(total_volume_hand, 2),
            "total_amount": round(total_amount, 2),
            "peak_buy_unmatched_hand": round(peak_buy_unmatched_hand, 2),
            "peak_sell_unmatched_hand": round(peak_sell_unmatched_hand, 2),
            "last_buy_unmatched_hand": round(raw_float(last_raw, "3") or 0.0, 2),
            "last_sell_unmatched_hand": round(raw_float(last_raw, "4") or 0.0, 2),
        }

    def _get_premarket_auction_summary(
        self,
        code: str,
    ) -> Optional[Dict[str, Any]]:
        """聚合 KPL 当日 09:15~09:25 集合竞价序列的有效数据。"""
        try:
            data = self._client.get(f"/auction/stock-bid/{code}")
        except KplError as exc:
            logger.debug("[KplFetcher] 盘前竞价 %s 获取失败: %s", code, exc)
            return None

        bid = data.get("bid") if isinstance(data.get("bid"), list) else []
        if not bid:
            logger.debug("[KplFetcher] %s 盘前竞价无记录", code)
            return None

        preclose = _to_float(data.get("preclose"))
        open_price = _to_float(data.get("open"))
        first = bid[0] if isinstance(bid[0], dict) else {}
        last = bid[-1] if isinstance(bid[-1], dict) else {}
        first_price = _to_float(first.get("price"))
        last_price = _to_float(last.get("price"))
        last_volume_hand = _to_float(last.get("volume"))

        volumes = [
            _to_float(item.get("volume"))
            for item in bid
            if isinstance(item, dict)
        ]
        valid_volumes = [v for v in volumes if v is not None]
        peak_volume_hand = max(valid_volumes) if valid_volumes else None
        final_volume_hand = (
            last_volume_hand
            if last_volume_hand is not None
            else peak_volume_hand
        )
        withdraw_volume_hand = (
            max(0.0, peak_volume_hand - (final_volume_hand or 0.0))
            if peak_volume_hand is not None and final_volume_hand is not None
            else None
        )

        auction_price = last_price if last_price is not None else open_price
        estimated_turnover_amount = None
        if auction_price is not None and final_volume_hand is not None:
            estimated_turnover_amount = final_volume_hand * auction_price * 100

        bid_change_pct = None
        if preclose and preclose > 0 and open_price is not None:
            bid_change_pct = round((open_price / preclose - 1) * 100, 4)

        return {
            "auction_date": str(data.get("day") or "").strip() or None,
            "preclose": preclose,
            "open": open_price,
            "high": _to_float(data.get("high")),
            "low": _to_float(data.get("low")),
            "bid_count": len(bid),
            "first_price": first_price,
            "last_price": auction_price,
            "first_volume_hand": _to_float(first.get("volume")),
            "final_volume_hand": final_volume_hand,
            "peak_volume_hand": peak_volume_hand,
            "withdraw_volume_hand": withdraw_volume_hand,
            "bid_change_pct": bid_change_pct,
            "estimated_turnover_amount": (
                round(estimated_turnover_amount, 2)
                if estimated_turnover_amount is not None
                else None
            ),
        }

    # 说明：有意不实现 get_belong_board（个股所属板块）。
    #
    # 上游 /plate-list/stock-sector-v2 能返回数据，但排序语义与该能力的用途
    # 不匹配，接入反而是降级——DataFetcherManager 用 hasattr 探测这个方法名，
    # 不定义即自动回落到 EfinanceFetcher 的东财口径。
    #
    # 实测 002364（中恒电气，电源设备公司）对比：
    #   KPL  前5: 拼多多概念 | 杭州 | 电源 | 光储充一体化 | 通信
    #   东财 前5: 电力设备 | 其他电源设备Ⅲ | 其他电源设备Ⅱ | 浙江板块 | 换电概念
    #
    # 三个叠加的问题：
    #   1. 上游按**板块当日涨跌幅降序**排列，即「今天哪个板块涨得多」决定了
    #      「这只股票属于什么」，与所属板块的语义无关；
    #   2. 涨跌幅盘中实时变化，同一标的不同时刻取到的顺序不同，而下游
    #      src/notification.py 只取 belong_boards[:5]，导致分析结果不可复现；
    #   3. 返回的 44 条几乎全是概念标签，只有「电气设备」沾边，缺东财那种
    #      申万行业层级，而行业归属恰恰是该字段最有价值的部分。
    #
    # 若将来上游提供行业类型标识（可据此做「行业优先、概念次之」的重排），
    # 可以重新评估接入。

    # ------------------------------------------------------------------
    # 市场维度
    # ------------------------------------------------------------------

    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        """获取行业板块涨跌排行。

        用 /limit-up/weight-performance-realtime 而不是 /plate-list/pc-plate-ranking：
        后者是「板块」大杂烩（申万行业 + 概念 + 地域混杂，实测 002364 前5 里出现过
        「杭州」这种地域标签，见上方 get_belong_board 弃用说明的同一份实测数据），
        用它当"行业板块"会把地域/概念标签也当行业展示；前者是固定 30 个申万二级
        行业的扁平实时列表，字段干净（sector_name/change_pct 已是成品，无需再从
        raw_fields 按位置猜语义），不掺概念/地域。

        Returns:
            (领涨列表, 领跌列表)，元素为 {"name", "change_pct"}；失败返回 None
        """
        if not self.is_available():
            return None
        try:
            data = self._client.get("/limit-up/weight-performance-realtime", params={"page_size": 50})
        except KplError as exc:
            logger.warning("[KplFetcher] 获取行业板块排行失败: %s", exc)
            return None

        rows = []
        for item in data.get("items") or []:
            name = str(item.get("sector_name") or "").strip()
            change_pct = _to_float(item.get("change_pct"))
            if name and change_pct is not None:
                rows.append({"name": name, "change_pct": change_pct})

        if not rows:
            logger.debug("[KplFetcher] 行业板块排行无有效数据")
            return None

        # 上游按板块代码排序而非涨跌幅，这里自行排序取两端
        ordered = sorted(rows, key=lambda x: x["change_pct"], reverse=True)
        top = ordered[:n]
        bottom = list(reversed(ordered[-n:])) if len(ordered) > n else list(reversed(ordered))
        logger.info("[KplFetcher] 行业板块排行获取成功: %d 个行业", len(rows))
        return top, bottom

    def get_concept_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        """获取概念/题材涨跌排行。

        用 /market-stats/theme-list（Socket 专属，cmd 3009）：它是纯"题材"分类
        （如"AI硬件""人形机器人""DeepSeek概念"），与 get_sector_rankings 用的
        /plate-list/pc-plate-ranking（行业/概念混杂的"板块"分类）是两套独立编号
        体系，不会产生重复。``ratio`` 即涨跌幅（已换算，无需再除以 100）。
        未配置 Socket 签名时该接口返回 configured=false，此时判为不可用。

        Returns:
            (领涨列表, 领跌列表)，元素为 {"name", "change_pct"}；失败返回 None
        """
        if not self.is_available():
            return None
        try:
            data = self._client.get("/market-stats/theme-list")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取题材排行失败: %s", exc)
            return None

        if not data.get("configured", True):
            logger.debug("[KplFetcher] 题材排行 Socket 签名未配置")
            return None

        rows = []
        for item in data.get("items") or []:
            name = str(item.get("name") or "").strip()
            change_pct = _to_float(item.get("ratio"))
            if name and change_pct is not None:
                rows.append({"name": name, "change_pct": change_pct})

        if not rows:
            logger.debug("[KplFetcher] 题材排行无有效数据")
            return None

        ordered = sorted(rows, key=lambda x: x["change_pct"], reverse=True)
        top = ordered[:n]
        bottom = list(reversed(ordered[-n:])) if len(ordered) > n else list(reversed(ordered))
        logger.info("[KplFetcher] 题材排行获取成功: %d 个题材", len(rows))
        return top, bottom

    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """获取市场涨跌统计。

        用 /mood/market-daban-snapshot：它一条就覆盖契约要求的全部 6 个字段，
        而 /market-stats/mood-num-count 缺 flat_count、/market-stats/zhangfu-detail
        缺成交额。

        成交额单位换算依据：该端点的 ``market_turnover_wan`` 与
        ``mood-num-count.market_turnover`` 数值完全相同，而前者字段名显式带
        ``_wan``，故上游为「万元」；DSA 与 TickFlow 一致按「亿元」返回。

        Returns:
            {"up_count", "down_count", "flat_count", "limit_up_count",
             "limit_down_count", "total_amount"(亿元)}；失败返回 None
        """
        if not self.is_available():
            return None
        try:
            data = self._client.get("/mood/market-daban-snapshot")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取大盘统计失败: %s", exc)
            return None

        up = _to_int(data.get("rising_count"))
        down = _to_int(data.get("falling_count"))
        if up is None and down is None:
            logger.debug("[KplFetcher] 大盘统计无涨跌家数，判为无效")
            return None

        turnover_wan = _to_float(data.get("market_turnover_wan"))
        stats = {
            "up_count": up or 0,
            "down_count": down or 0,
            "flat_count": _to_int(data.get("flat_count")) or 0,
            "limit_up_count": _to_int(data.get("limit_up_count")) or 0,
            "limit_down_count": _to_int(data.get("limit_down_count")) or 0,
            "total_amount": round(turnover_wan / 1e4, 2) if turnover_wan else 0.0,
        }
        logger.info(
            "[KplFetcher] 大盘统计: 涨%d 跌%d 平%d 成交%.0f亿",
            stats["up_count"], stats["down_count"], stats["flat_count"],
            stats["total_amount"],
        )
        return stats

    # 说明：未实现 get_main_indices。上游 /market-stats/global-index 只有海外指数
    # （道指/纳指/恒生/日经）、期货、商品与汇率，没有上证/深证/创业板等 A 股指数，
    # 无法满足 get_main_indices(region="cn") 的语义。

    def get_limit_up_pool(
        self,
        date: Optional[str] = None,
        n: int = 20,
    ) -> Optional[List[Dict[str, Any]]]:
        """获取涨停池与连板梯队。

        上游按连板数分档查询（``pid_type`` 即连板数，实测 1=首板、2=二板…，
        传 0 会 502），因此这里逐档拉取再聚合，按连板数从高到低返回。

        Args:
            date: YYYYMMDD；传入则查历史，否则取实时
            n: 返回条数上限
        """
        if not self.is_available():
            return None

        pool: List[Dict[str, Any]] = []
        for board in range(_MAX_CONSECUTIVE_BOARD, 0, -1):
            if len(pool) >= n:
                break
            try:
                if date:
                    payload = self._client.get(
                        "/limit-up/daily-limit-perf-history",
                        params={"date": date, "pid_type": board},
                    )
                else:
                    payload = self._client.get(
                        "/limit-up/realtime", params={"pid_type": board}
                    )
            except KplError as exc:
                # 单档失败不影响其它档位，继续聚合已拿到的部分
                logger.warning("[KplFetcher] 涨停池 %d 板档位获取失败: %s", board, exc)
                continue

            for item in payload.get("items") or []:
                code = str(item.get("code") or "").strip()
                if not code:
                    continue
                pool.append({
                    "code": code,
                    "name": str(item.get("name") or "").strip(),
                    "change_pct": _to_float(item.get("change_pct")),
                    "consecutive_days": item.get("consecutive_days"),
                    "reason": str(item.get("reason") or "").strip(),
                    "sector_name": str(item.get("sector_code") or "").strip(),
                    "seal_amount": _to_float(item.get("seal_amount")),
                    "turnover": _to_float(item.get("turnover")),
                    "circ_mv": _to_float(item.get("circ_mv")),
                    "last_price": _to_float(item.get("last_price")),
                })

        if not pool:
            logger.debug("[KplFetcher] 涨停池无数据 (date=%s)", date or "realtime")
            return None
        logger.info("[KplFetcher] 涨停池获取成功: %d 只", len(pool))
        return pool[:n]

    # ------------------------------------------------------------------
    # 筹码分布
    # ------------------------------------------------------------------

    def get_chip_distribution(self, stock_code: str) -> Optional[ChipDistribution]:
        """获取个股筹码分布（持仓成本分布）。

        ⚠️ 上游是**本地计算**而非服务器接口：开盘啦对成本分布既无 HTTP 也无
        Socket 接口，App 自身也是拿日 K 本地算的，该端点用同族算法（三角形
        分布 + 换手衰减）基于 1000 根日线复现，上游自述与 App 实测偏差平均
        约 4%。因此它不受 KPL 凭证过期影响，但也不是「官方数据」。

        与另两个源的关系（2026-07-27 收盘时点对拍，四只标的）：三方均通过
        自洽性检验（70% 区间 ⊆ 90% 区间、均成本落在 90% 区间内、集中度等于
        (高-低)/(高+低)），本源与 AkShare 系统性接近而 Tushare 离群，例如
        收盘价在 90% 区间中的位置：浪潮信息 KPL 0.685 / AkShare 0.716 /
        Tushare 0.382。三者都是算法输出、无绝对真值，这里不主张谁更准；
        选它排在最前的理由是不依赖将失效的 Tushare 凭证、耗时 0.1 秒
        （AkShare 6~11 秒、Tushare 17~28 秒）、且结果稳定可复现。

        单位换算：上游 ``profit_pct`` 与 ``concentration`` 都是百分数，
        ChipDistribution 的 ``profit_ratio`` / ``concentration_*`` 是小数。

        Returns:
            ChipDistribution；不可用或数据无效时返回 None（由上层降级）
        """
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None

        try:
            # decay 有意不透传：0.8~1.2 会让获利比在 0.197~0.284 间摆动（差 45%），
            # 暴露成可调项会让同一标的不同调用给出不同结论。默认 1.0 是标准
            # 筹码算法（换手率直接衰减）。
            payload = self._client.get(f"/kline/stock-chip-distribution/{code}")
        except KplError as exc:
            logger.warning("[KplFetcher] 获取筹码分布失败 %s: %s", stock_code, exc)
            return None

        avg_cost = _to_float(payload.get("avg_cost"))
        if avg_cost is None or avg_cost <= 0:
            logger.debug("[KplFetcher] %s 筹码分布无有效均成本，跳过", stock_code)
            return None

        r90 = payload.get("range_90") or {}
        r70 = payload.get("range_70") or {}

        return ChipDistribution(
            code=code,
            date=self._latest_chip_date(code),
            source="kpl",
            profit_ratio=_pct_to_ratio(payload.get("profit_pct")),
            avg_cost=avg_cost,
            cost_90_low=_to_float(r90.get("low")) or 0.0,
            cost_90_high=_to_float(r90.get("high")) or 0.0,
            concentration_90=_pct_to_ratio(r90.get("concentration")),
            cost_70_low=_to_float(r70.get("low")) or 0.0,
            cost_70_high=_to_float(r70.get("high")) or 0.0,
            concentration_70=_pct_to_ratio(r70.get("concentration")),
        )

    def _latest_chip_date(self, code: str) -> str:
        """筹码所基于的最新交易日。

        上游只回 ``close`` 不回日期，这里取日线末条补上——下游报告要显示
        数据日期，缺了会让读者无法判断新鲜度。取数失败不影响主结果。
        """
        try:
            payload = self._client.get(f"/kline/daily/{code}")
        except KplError:
            return ""
        days = payload.get("days") or []
        if not days:
            return ""
        return kpl_date_to_iso(days[-1].get("date")) or ""

    # ------------------------------------------------------------------
    # 基本面：成长 / 业绩 / 机构
    # ------------------------------------------------------------------

    def get_fundamental_bundle(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """获取成长、业绩、机构三块基本面字段。

        字段语义与 AkshareFundamentalAdapter.get_fundamental_bundle 保持一致，
        供其作为优先源合并；本方法只填能确证的字段，拿不到的留 None 由
        AkShare 兜底，不做猜测性填充。

        与 AkShare 路径的关键差异：AkShare 走「无参拉全市场默认报告期 + 本地
        捞行 + 关键词猜列名」，命中率低；KPL 全部按股票代码直查、字段名固定。

        Returns:
            {"growth": {...}, "earnings": {...}, "institution": {...}}；
            数据源不可用时返回 None
        """
        if not self.is_available():
            return None
        code = normalize_stock_code(stock_code)
        if not code or not code.isdigit() or len(code) != 6:
            return None

        return {
            "growth": self._kpl_growth(code),
            "earnings": self._kpl_earnings(code),
            "institution": self._kpl_institution(code),
        }

    def _kpl_growth(self, code: str) -> Dict[str, Any]:
        """成长性四字段，取自 /finance/finance-info（105 期财报 + 同比）。

        上游给的是带单位的字符串（"-19.59%" / "2.75%"），这里统一剥成数值。
        """
        try:
            payload = self._client.get(f"/finance/finance-info/{code}")
        except KplError as exc:
            logger.debug("[KplFetcher] %s 财务摘要获取失败: %s", code, exc)
            return {}

        reports = payload.get("reports") or []
        yoy = payload.get("yoy_reports") or []
        if not reports:
            return {}
        latest = reports[0] if isinstance(reports[0], dict) else {}
        latest_yoy = yoy[0] if yoy and isinstance(yoy[0], dict) else {}

        return {
            "revenue_yoy": _pct_to_float(latest_yoy.get("GJZB_YYSR")),
            "net_profit_yoy": _pct_to_float(latest_yoy.get("GJZB_JLR")),
            "roe": _pct_to_float(latest.get("YLNL_JZCSYL")),
            "gross_margin": _pct_to_float(latest.get("YLNL_XSMLL")),
        }

    def _kpl_earnings(self, code: str) -> Dict[str, Any]:
        """业绩预告摘要，取自 /company-info/stock-big-reminder。

        ⚠️ 该接口的 ``type=5`` 是**财报类公告混合流**，不是业绩预告专属：实测
        6 只标的里只有 2 只首条是业绩预告，其余是季报/年报/年报摘要/H股公告。
        伴生的 ``tag`` 只能区分「财报类(1) / 减持类(2)」，区分不了预告与正式
        财报，所以必须再按标题关键词筛，否则会把年报当成业绩预告。

        业绩快报（quick_report_summary）不在这里填：KPL 的业绩披露走 Socket
        长连接（命令码 2113，Protobuf），HTTP 侧没有对应能力，已确认为结构性
        缺口，保留 AkShare stock_yjkb_em 兜底。
        """
        try:
            payload = self._client.get(f"/company-info/stock-big-reminder/{code}")
        except KplError as exc:
            logger.debug("[KplFetcher] %s 重要事项提醒获取失败: %s", code, exc)
            return {}

        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            raw = item.get("raw") or {}
            if raw.get("type") != 5 or raw.get("tag") != 1:
                continue
            title = str(item.get("title") or "").strip()
            if "业绩预告" not in title:
                continue
            date = str(item.get("date") or "")[:10]
            summary = f"{title}（{date}）" if date else title
            return {"forecast_summary": summary[:200]}
        return {}

    def _kpl_institution(self, code: str) -> Dict[str, Any]:
        """十大流通股东合计持股变动百分比。

        ⚠️ 上游 ``change_from_last``（SJJZC）是**较上期变化百分比**，不是股数。
        已用相邻两期持股数交叉验算确认：香港中央结算 3526.77→3383.30 万股
        （-143.47 万股）对应 -4.07，正是 -4.07%；华泰柏瑞沪深300ETF
        1694.43→826.90（-867.53 万股）对应 -51.20，正是 -51.20%。按万股解读
        会得到完全错误的量级。

        取值有三种形态：``"不变"`` / ``"新进"`` / 数值百分比。这里由各家本期
        持股数与其变化率反推上期持股，再按十家合计口径算净变动百分比——比
        取单一股东更能反映机构整体增减，且能正确容纳「新进」（上期为 0）。
        """
        for day, sq_day in _recent_quarter_pairs():
            try:
                payload = self._client.get(
                    f"/institution/gudong-info-ten-by-date/{code}",
                    params={"day": day, "sq_day": sq_day, "holder_type": 1},
                )
            except KplError as exc:
                logger.debug("[KplFetcher] %s 十大股东 %s 获取失败: %s", code, day, exc)
                continue

            items = payload.get("items") or []
            if not items:
                continue  # 该报告期尚未披露，往前找

            now_total = 0.0
            old_total = 0.0
            counted = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                now = _to_float(item.get("holding_shares_wan"))
                if now is None:
                    continue
                change = str(item.get("change_from_last") or "").strip()
                if change == "不变":
                    old = now
                elif change == "新进":
                    old = 0.0
                else:
                    pct = _to_float(change)
                    if pct is None or pct <= -100:
                        continue  # 无法反推上期持股，跳过该家而不是猜
                    old = now / (1.0 + pct / 100.0)
                now_total += now
                old_total += old
                counted += 1

            if counted == 0 or old_total <= 0:
                continue
            change_pct = (now_total - old_total) / old_total * 100.0
            return {
                "top10_holder_change": round(change_pct, 4),
                "top10_report_date": day,
            }
        return {}

    # ------------------------------------------------------------------
    # 资源释放
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()


def _to_float(value: Any) -> Optional[float]:
    """把上游可能给出的 str/None/空串统一成 float，无法解析返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    """把上游可能给出的 str/float/None 统一成 int，无法解析返回 None。"""
    f = _to_float(value)
    return int(f) if f is not None else None


def _pct_to_float(value: Any) -> Optional[float]:
    """剥掉上游财务字段的单位后缀，返回数值。

    上游把数值和单位放在同一个字符串里（``"-19.59%"`` / ``"2.75%"``），
    直接 float() 会失败。这里只处理百分号与空白，其它单位（如 "354.70亿"）
    不在本函数职责内——那类字段量纲不同，需要各自换算，不能笼统剥离。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    return _to_float(text)


def _pct_to_ratio(value: Any) -> float:
    """上游百分数转小数（24.11 → 0.2411）；无法解析按 0.0 处理。

    ChipDistribution 的 profit_ratio / concentration_* 都是 0~1 的小数，
    而上游给的是百分数，直接透传会让下游把 24.11 当成 2411% 的获利盘。
    """
    parsed = _pct_to_float(value)
    return parsed / 100.0 if parsed is not None else 0.0


def _recent_quarter_pairs(today: Optional[date] = None, limit: int = 4):
    """由近及远生成 (报告期, 上一报告期) 的日期串，用于回溯已披露的季报。

    十大股东按报告期归档，最新一期常常尚未披露（实测 2026-06-30 返回空，
    因为半年报要到 8 月底才出），需要依次往前找到第一个有数据的报告期。
    """
    today = today or date.today()
    ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    quarters = []
    year = today.year
    for y in (year, year - 1, year - 2):
        for month, day_ in ends:
            d = date(y, month, day_)
            if d <= today:
                quarters.append(d)
    quarters.sort(reverse=True)

    pairs = []
    for i, d in enumerate(quarters[:limit]):
        if i + 1 >= len(quarters):
            break
        pairs.append((d.isoformat(), quarters[i + 1].isoformat()))
    return pairs


def _epoch_to_date(value: Any) -> Optional[str]:
    """秒级 epoch 转 ``YYYY-MM-DD``；无法解析返回 None。"""
    if value in (None, "", 0):
        return None
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


def _notify_credential_expired(reason: str) -> None:
    """KPL 凭证失效时推送系统错误通知。

    走 DSA 既有的 ``system_error`` 路由，渠道由 NOTIFICATION_SYSTEM_ERROR_CHANNELS
    控制——未配置则只留日志，符合「不配置也可运行」。dedup_key 固定，让通知层
    自带的去重与冷却接管，避免每次探针 TTL 到期都重复推送。

    整体 fail-open：通知栈不可用不能影响数据源降级本身。
    """
    try:
        from src.notification import NotificationService

        content = (
            "## ⚠️ KPL 数据源凭证失效\n\n"
            f"{reason}\n\n"
            "**影响**：KPL 已自动退出数据源调用链，A 股行情/板块/资讯将回落到"
            "东财、AkShare 等既有数据源，分析不会中断，但数据质量可能下降。\n\n"
            "**处理**：重新抓包更新 kpl-unified-client 的 `.env`"
            "（KPL_USER_ID / KPL_TOKEN / KPL_DEVICE_ID），然后重启该服务。"
        )
        NotificationService().send(
            content,
            route_type="system_error",
            severity="error",
            dedup_key="kpl_credential_expired",
            cooldown_key="kpl_credential_expired",
        )
        logger.info("[KplFetcher] 已推送凭证失效告警")
    except Exception as exc:
        logger.warning("[KplFetcher] 推送凭证失效告警失败: %s", exc)
