# 数据源系统

本文档描述 `data_provider/` 层的架构、fallback 机制、各 fetcher 的能力边界，以及如何添加新的数据源。

---

## 整体架构

```
DataFetcherManager          ← data_provider/base.py（2741 行）
    │
    ├─ 按优先级排列 fetcher 列表
    ├─ 依次尝试，首个成功则返回
    ├─ 失败则降级到下一个
    └─ 全部失败则抛出 DataFetchError
         │
         ├─ EfinanceFetcher      Priority 0（无 token 时最高优先级）
         ├─ AkshareFetcher       Priority 1
         ├─ TushareFetcher       Priority 0/2（有 token 时提升到 0）
         ├─ PytdxFetcher         Priority 2
         ├─ BaostockFetcher      Priority 3
         ├─ YfinanceFetcher      Priority 4（Yahoo Finance 全球兜底）
         ├─ LongbridgeFetcher    Priority 5（美股/港股专用兜底）
         ├─ FinnhubFetcher       Priority 6（美股实时行情）
         └─ AlphaVantageFetcher  Priority 6（美股历史）
```

**核心原则：** 单一数据源失败不影响整体分析流程。每个方法（日线数据、实时行情、基本面等）独立走 fallback 链，互不依赖。

---

## 数据源能力矩阵

| 数据源 | A股日线 | A股实时 | 港股 | 美股 | 基本面 | 筹码 | 需 Token |
|---|---|---|---|---|---|---|---|
| EfinanceFetcher | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ |
| AkshareFetcher | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ |
| TushareFetcher | ✅ | ❌ | 部分 | ❌ | ✅ | ❌ | ✅ |
| PytdxFetcher | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| BaostockFetcher | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| YfinanceFetcher | 部分 | 部分 | ✅ | ✅ | 部分 | ❌ | ❌ |
| LongbridgeFetcher | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| FinnhubFetcher | ❌ | ✅ | ❌ | ✅ | 部分 | ❌ | ✅ |
| AlphaVantageFetcher | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |
| TickflowFetcher | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ |

---

## BaseFetcher 接口

**文件：** `data_provider/base.py` → `BaseFetcher`

所有 fetcher 继承 `BaseFetcher` 并实现以下方法（未实现则返回 `None` 或抛出 `NotImplementedError`）：

```python
class BaseFetcher:
    def get_daily_data(
        self,
        stock_code: str,
        start_date: str,       # 格式: "YYYYMMDD"
        end_date: str,
        adjust: str = "qfq",   # 前复权: "qfq" | 后复权: "hfq" | 不复权: ""
    ) -> Optional[pd.DataFrame]:
        """返回标准化日线 DataFrame，列名见 STANDARD_COLUMNS"""

    def get_realtime_quote(
        self,
        stock_code: str,
    ) -> Optional[UnifiedRealtimeQuote]:
        """返回实时行情，含价格/涨跌幅/成交量等"""

    def get_chip_distribution(
        self,
        stock_code: str,
    ) -> Optional[ChipDistribution]:
        """返回筹码分布：获利比例/平均成本/集中度"""

    def get_fundamental(
        self,
        stock_code: str,
    ) -> Optional[Dict[str, Any]]:
        """返回基本面数据：PE/PB/市值/ROE 等"""
```

**标准列名（STANDARD_COLUMNS）：**

```python
STANDARD_COLUMNS = [
    'date', 'open', 'high', 'low', 'close', 'volume', 'amount',
    'change_pct', 'turnover_rate', 'pe_ratio', 'pb_ratio',
    'market_cap', 'circulating_market_cap',
    'ma5', 'ma10', 'ma20', 'ma60',
    'macd', 'macd_signal', 'macd_hist',
    'rsi', 'kdj_k', 'kdj_d', 'kdj_j',
    'cci', 'boll_upper', 'boll_mid', 'boll_lower',
    'volume_ratio', 'amplitude',
]
```

fetcher 返回的 DataFrame 必须包含 `date` 和 `close` 列，其余列不足时由 `DataFetcherManager` 在 fallback 合并时补全。

---

## DataFetcherManager

**文件：** `data_provider/base.py` → `DataFetcherManager`

单例模式，通过 `DataFetcherManager.get_instance()` 获取。

### 优先级动态调整

优先级由 `REALTIME_SOURCE_PRIORITY` 配置控制（`.env` 中的 `REALTIME_SOURCE_PRIORITY`），或根据是否配置了 `TUSHARE_TOKEN` 自动决定：

```
有 TUSHARE_TOKEN：
  Priority 0: TushareFetcher, EfinanceFetcher（并列）
  Priority 1: AkshareFetcher
  ...

无 TUSHARE_TOKEN：
  Priority 0: EfinanceFetcher
  Priority 1: AkshareFetcher
  Priority 2: PytdxFetcher, TushareFetcher（不可用，跳过）
  ...
```

### 熔断机制（Circuit Breaker）

每个 fetcher 内置熔断器（`realtime_types.py` → `get_realtime_circuit_breaker`）。当一个 fetcher 连续失败超过阈值（默认 3 次），自动进入冷却期（默认 60 秒），期间跳过该 fetcher 降级到下一个。

```python
# AkshareFetcher 内部的熔断检查
cb = get_realtime_circuit_breaker("akshare_realtime")
if not cb.can_request():
    raise RateLimitError("熔断冷却中")
```

### 实时行情缓存

`data_provider/akshare_fetcher.py` 维护一个进程内 TTL 字典（`_realtime_cache`，TTL 20 分钟），避免批量分析场景下对同一股票重复请求实时行情。

---

## 防封禁策略

AkshareFetcher 是主要爬虫数据源，采用以下策略防封禁：

1. **随机休眠：** 每次请求前随机等待 2-5 秒（可通过 `AKSHARE_SLEEP_MIN/MAX` 配置）
2. **User-Agent 轮换：** 从 5 个预设 UA 中随机选择
3. **指数退避重试：** 使用 tenacity，最多 3 次，初始等待 4 秒
4. **熔断器：** 连续失败后自动冷却（避免持续触发封禁）

---

## 股票代码规范

**文件：** `data_provider/base.py` → `normalize_stock_code` / `canonical_stock_code`

系统内部使用不带市场后缀的纯数字代码（`600519`），跨市场时加后缀：

```python
normalize_stock_code("600519.SH")   # → "600519"
normalize_stock_code("hk00700")     # → "hk00700"（港股保留前缀）
normalize_stock_code("AAPL")        # → "AAPL"（美股保留原样）

canonical_stock_code("600519")      # → "600519"（用于去重键）
```

辅助工具函数：

```python
is_bse_code("000001")      # True（北交所）
is_st_stock("*ST银行")      # True
is_kc_cy_stock("688001")   # True（科创板）
is_hk_stock_code("hk00700") # True
is_us_stock_code("AAPL")   # True
```

---

## 如何添加新的数据源

### Step 1：创建 fetcher 文件

在 `data_provider/` 下新建 `my_fetcher.py`：

```python
from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
import pandas as pd
from typing import Optional

class MyFetcher(BaseFetcher):
    """My data source fetcher."""

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    def get_daily_data(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> Optional[pd.DataFrame]:
        try:
            # 调用你的数据源 API
            raw = my_api.get_ohlcv(stock_code, start_date, end_date)
            df = pd.DataFrame(raw)
            # 确保列名与 STANDARD_COLUMNS 对齐
            df = df.rename(columns={"ts": "date", "pct_chg": "change_pct"})
            df["date"] = pd.to_datetime(df["date"])
            return df
        except Exception as e:
            raise DataFetchError(f"MyFetcher.get_daily_data failed: {e}") from e

    def get_realtime_quote(self, stock_code: str):
        # 不支持实时行情时返回 None
        return None
```

### Step 2：注册到 DataFetcherManager

在 `data_provider/__init__.py` 导入：

```python
from .my_fetcher import MyFetcher
```

在 `data_provider/base.py` 的 `DataFetcherManager.__init__` 中，按期望优先级插入：

```python
from .my_fetcher import MyFetcher

# 在 fetcher 列表中按优先级插入
if config.my_api_key:
    self._fetchers.insert(2, MyFetcher(api_key=config.my_api_key))
```

### Step 3：添加配置项

在 `.env.example` 添加：

```
MY_API_KEY=                    # MyFetcher API Key（留空则不启用）
```

在 `src/config.py` 的 `Config` dataclass 添加字段：

```python
my_api_key: str = field(default="")
```

在 `_load_from_env()` 中加载：

```python
my_api_key = os.getenv("MY_API_KEY", "")
```

在 `src/core/config_registry.py` 的 `_FIELD_DEFINITIONS` 中添加元数据（供 Web 设置页显示）。

### Step 4：写测试

在 `tests/` 添加 `test_my_fetcher.py`，至少覆盖：

```python
def test_get_daily_data_returns_standard_columns():
    """Verify output has required STANDARD_COLUMNS subset."""
    ...

def test_get_daily_data_on_network_error_raises_DataFetchError():
    ...
```

---

## 实时行情类型

**文件：** `data_provider/realtime_types.py` → `UnifiedRealtimeQuote`

```python
@dataclass
class UnifiedRealtimeQuote:
    code: str
    name: str
    current: float              # 当前价
    open: float                 # 今开
    high: float                 # 今高
    low: float                  # 今低
    prev_close: float           # 昨收
    change: float               # 涨跌额
    change_pct: float           # 涨跌幅 (%)
    volume: float               # 成交量（手）
    amount: float               # 成交额（元）
    turnover_rate: float        # 换手率 (%)
    volume_ratio: float         # 量比
    pe_ratio: float             # 市盈率
    pb_ratio: float             # 市净率
    market_cap: float           # 总市值（元）
    circulating_market_cap: float  # 流通市值
    source: RealtimeSource      # 数据来源标记
    timestamp: datetime
```

---

## 故障排查

**数据源全部失败：**
- 检查 `logs/` 目录下的日志，搜索 `DataFetchError`
- 确认网络连通性（eastmoney.com、finance.sina.com.cn）
- 检查熔断状态：日志中搜索 `熔断冷却中` 或 `Circuit breaker`

**某数据源返回空数据：**
- 确认股票代码格式正确（`600519` 不是 `600519.SH`）
- 非交易日不会有日线数据，属正常

**Tushare 数据不准确：**
- 确认 `TUSHARE_TOKEN` 有效且有足够积分
- Tushare 不支持实时行情，优先使用 EfinanceFetcher

---

## 相关文档

- [系统架构总览](ARCHITECTURE.md)
- [完整配置与部署指南](../full-guide.md)
- [FAQ](../FAQ.md)
