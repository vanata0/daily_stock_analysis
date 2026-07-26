#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个股资金流口径对拍：KPL / Mairuiapi vs Tushare moneyflow。

以 Tushare ``moneyflow`` 的「特大单 + 大单」净额作为仲裁基准，比较各数据源
逐交易日的主力净流入方向一致率与相关系数。

2026-07-26 首次运行结果（8 只标的 × 12 个交易日 = 96 样本）：

    KPL        方向一致率 88.5%   平均相关系数 +0.762
    Mairuiapi  方向一致率 52.1%   平均相关系数 +0.102

Mairuiapi 接近随机，且在招商银行(1/12, -0.677)、工商银行(2/12, -0.631) 等
标的上系统性反向；据此把资金流主源切到 KPL，Mairuiapi 降为兜底。

⚠️ 时效性：仲裁依赖 Tushare 代理接入，该接入即将下线。下线后本脚本无法再
提供基准，请在下线前保留一份输出作为历史留档。

用法：
    python3 scripts/check_capital_flow_parity.py
    python3 scripts/check_capital_flow_parity.py --stocks 000977,600519 --days 20
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_STOCKS = [
    ("000977", "浪潮信息", "SZ"), ("600519", "贵州茅台", "SH"),
    ("300750", "宁德时代", "SZ"), ("601398", "工商银行", "SH"),
    ("002261", "拓维信息", "SZ"), ("688981", "中芯国际", "SH"),
    ("000001", "平安银行", "SZ"), ("600036", "招商银行", "SH"),
]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass


def _tushare(api: str, params: dict, token: str, api_url: str) -> dict:
    body = {"api_name": api, "token": token, "params": params, "fields": ""}
    req = urllib.request.Request(
        api_url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode())


def _jget(url: str, timeout: int = 25) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _corr(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 2:
        return float("nan")
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else float("nan")


def _tushare_baseline(code: str, ex: str, days: List[str], token: str, url: str) -> Dict[str, float]:
    """主力净额 = (特大单买 + 大单买) - (特大单卖 + 大单卖)，万元转元。"""
    data = _tushare("moneyflow", {"ts_code": f"{code}.{ex}",
                                  "start_date": days[-1], "end_date": days[0]}, token, url)
    if data.get("code") != 0:
        raise RuntimeError(f"tushare moneyflow failed: {data.get('msg')}")
    idx = {c: i for i, c in enumerate(data["data"]["fields"])}
    out = {}
    for row in data["data"]["items"]:
        buy = (row[idx["buy_elg_amount"]] or 0) + (row[idx["buy_lg_amount"]] or 0)
        sell = (row[idx["sell_elg_amount"]] or 0) + (row[idx["sell_lg_amount"]] or 0)
        out[row[idx["trade_date"]]] = (buy - sell) * 1e4
    return out


def _kpl_flow(code: str, days: List[str], base: str) -> Dict[str, float]:
    out = {}
    for day in days:
        try:
            items = (_jget(f"{base}/big-money/chouma-history/{code}?day={day}").get("items") or [])
        except Exception:
            continue
        if items:
            last = items[-1]
            out[day] = (last.get("main_buy") or 0) + (last.get("main_sell") or 0)
    return out


def _mairui_flow(code: str, licence: str, count: int) -> Dict[str, float]:
    try:
        data = _jget(
            f"https://api.mairuiapi.com/hsstock/history/transaction/{code}/{licence}?lt={count}"
        )
    except Exception:
        return {}
    out = {}
    for rec in data if isinstance(data, list) else []:
        day = str(rec.get("t", "")).replace("-", "")
        buy = (rec.get("zmbtdcje", 0) or 0) + (rec.get("zmbddcje", 0) or 0)
        sell = (rec.get("zmstdcje", 0) or 0) + (rec.get("zmsddcje", 0) or 0)
        out[day] = buy - sell
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="个股资金流口径对拍")
    parser.add_argument("--stocks", help="逗号分隔的股票代码；缺省用内置样本集")
    parser.add_argument("--days", type=int, default=12, help="回溯交易日数（默认 12）")
    args = parser.parse_args(argv)

    _load_env()
    token = (os.getenv("TUSHARE_TOKEN") or "").strip()
    if not token:
        print("ERROR: 需要 TUSHARE_TOKEN 作为仲裁基准", file=sys.stderr)
        return 2
    ts_url = (os.getenv("TUSHARE_API_URL") or "http://api.tushare.pro").strip()
    licence = (os.getenv("MAIRUI_API_KEY") or "").strip()
    kpl_base = (os.getenv("KPL_API_BASE") or "http://127.0.0.1:8010").strip()

    if args.stocks:
        stocks = [(c.strip(), c.strip(), "SH" if c.strip()[0] == "6" else "SZ")
                  for c in args.stocks.split(",") if c.strip()]
    else:
        stocks = DEFAULT_STOCKS

    try:
        cal = _tushare("trade_cal", {"exchange": "SSE", "is_open": "1"}, token, ts_url)
        ci = {c: i for i, c in enumerate(cal["data"]["fields"])}
        import datetime as _dt
        today = _dt.date.today().strftime("%Y%m%d")
        days = sorted({r[ci["cal_date"]] for r in cal["data"]["items"]
                       if r[ci["cal_date"]] <= today}, reverse=True)[: args.days]
    except Exception as exc:
        print(f"ERROR: 获取交易日历失败: {exc}", file=sys.stderr)
        return 1

    print(f"仲裁基准: Tushare moneyflow (特大单+大单) @ {ts_url}")
    print(f"交易日 {len(days)} 天: {days[-1]} ~ {days[0]}\n")
    header = f"{'标的':<12}{'样本':>5}{'KPL方向':>10}{'迈瑞方向':>11}{'KPL相关':>10}{'迈瑞相关':>10}"
    print(header)
    print("-" * 62)

    tot = {"n": 0, "k_ok": 0, "m_ok": 0, "kc": [], "mc": []}
    for code, name, ex in stocks:
        try:
            base = _tushare_baseline(code, ex, days, token, ts_url)
        except Exception as exc:
            print(f"{name:<12} 基准获取失败: {exc}")
            continue
        kpl = _kpl_flow(code, days, kpl_base)
        mai = _mairui_flow(code, licence, args.days + 3) if licence else {}

        common = [d for d in days if d in base and d in kpl and d in mai]
        if len(common) < 3:
            print(f"{name:<12} 共同样本不足 ({len(common)})")
            continue
        t = [base[d] for d in common]
        k = [kpl[d] for d in common]
        m = [mai[d] for d in common]
        k_ok = sum((a > 0) == (b > 0) for a, b in zip(t, k))
        m_ok = sum((a > 0) == (b > 0) for a, b in zip(t, m))
        kc, mc = _corr(t, k), _corr(t, m)
        tot["n"] += len(common); tot["k_ok"] += k_ok; tot["m_ok"] += m_ok
        tot["kc"].append(kc); tot["mc"].append(mc)
        print(f"{name:<12}{len(common):>5}{k_ok:>7}/{len(common):<3}"
              f"{m_ok:>8}/{len(common):<3}{kc:>+10.3f}{mc:>+10.3f}")

    if not tot["n"]:
        print("\n没有可比样本")
        return 1
    print("-" * 62)
    n = tot["n"]
    print(f"{'合计':<12}{n:>5}{tot['k_ok']:>7}/{n:<3}{tot['m_ok']:>8}/{n:<3}"
          f"{statistics.mean(tot['kc']):>+10.3f}{statistics.mean(tot['mc']):>+10.3f}")
    print(f"\n方向一致率: KPL {tot['k_ok']/n:.1%}   迈瑞 {tot['m_ok']/n:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
