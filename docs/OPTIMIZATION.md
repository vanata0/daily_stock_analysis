# 技术优化路线图

本文档记录对 DSA 代码库的系统性评审结论，按优先级排列各优化项。评审时间：2026-05-24。

所有优化均采用渐进式推进原则：不破坏现有接口，不引入不必要的依赖，不做跨越多个 PR 的大型重构。

---

## P0：立即修复（影响生产可用性）

### P0-1：API 层同步函数阻塞事件循环

**现状：** `api/v1/endpoints/analysis.py` 中的主要触发端点（`trigger_analysis`、`trigger_market_review`）是同步 `def`，直接运行在 uvicorn 的 async 事件循环中。同时，`notification_sender/` 里多处 `time.sleep(1)` 被从这些路径调用，导致事件循环阻塞——其他请求在 sleep 结束前无法被处理。

**影响：** 批量分析（10+ 股票）时，Web 界面会出现请求长时间无响应。alert 评估请求可能超时。

**修复方案：**

```python
# 1. 将触发端点改为 async def
@router.post("/analyze")
async def trigger_analysis(...):
    # 提交到任务队列时已有正确实现，关键是入口本身需要是 async
    ...

# 2. notification_sender 中的 time.sleep 替换
# 旧
time.sleep(1)
# 新（在 async 函数内）
await asyncio.sleep(1)

# 3. 如果 sender 是 sync 函数，通过 asyncio.to_thread 卸载
result = await asyncio.to_thread(sync_notification_sender.send, payload)
```

**参考：** `api/v1/endpoints/agent.py:173` 已有正确示范（`loop.run_in_executor`），照此推广到 `analysis.py`。

**改动面：** `api/v1/endpoints/analysis.py`、`src/notification_sender/*.py`（8 个文件中的 `time.sleep` 调用）

---

### P0-2：`Config._load_from_env` 703 行巨函数

**现状：** `src/config.py` 的 `_load_from_env` 是单个方法，承担了解析所有配置类别的职责：LLM 渠道、通知渠道、数据源、调度、agent 参数等，共 703 行。

**影响：** 任何新增配置项都需要修改这一个方法，PR 冲突频繁。任何 bug 都难以定位（函数太长，很难理解状态流）。无法对单一配置类别写隔离的单元测试。

**修复方案（渐进式，不破坏接口）：**

```python
# src/config.py - 将 _load_from_env 拆为私有子方法
@classmethod
def _load_from_env(cls) -> 'Config':
    return cls(
        **cls._load_base_config(),
        **cls._load_llm_config(),
        **cls._load_notification_config(),
        **cls._load_data_source_config(),
        **cls._load_agent_config(),
        **cls._load_schedule_config(),
    )

@classmethod
def _load_llm_config(cls) -> Dict[str, Any]:
    """解析所有 LLM 相关配置（~150 行）"""
    ...

@classmethod
def _load_notification_config(cls) -> Dict[str, Any]:
    """解析所有通知渠道配置（~120 行）"""
    ...
```

**改动面：** 仅 `src/config.py`，不影响 `get_config()` 的外部调用者。

---

## P1：高价值优化（1-2 周内推进）

### P1-1：多 Agent 流水线并行化

**现状：** `AgentOrchestrator._execute_pipeline()` 串行执行所有 stage：Technical → Intel → Risk → Decision。

**分析：** TechnicalAgent（纯本地计算）和 IntelAgent（主要是网络搜索 + LLM）互不依赖，可以并行运行。

**修复方案：**

```python
async def _execute_pipeline_parallel(self, ctx: AgentContext):
    # Stage 1 和 Stage 2 并行
    technical_task = asyncio.create_task(
        asyncio.to_thread(self._run_stage_agent, TechnicalAgent(), ctx)
    )
    intel_task = asyncio.create_task(
        asyncio.to_thread(self._run_stage_agent, IntelAgent(), ctx)
    )
    await asyncio.gather(technical_task, intel_task)

    # Stage 3 需要前两个结果
    if self.mode in ("full", "specialist"):
        await asyncio.to_thread(self._run_stage_agent, RiskAgent(), ctx)

    # 最终决策
    await asyncio.to_thread(self._run_stage_agent, DecisionAgent(), ctx)
```

**收益：** standard 模式分析耗时可降低 30-50%（取决于 Intel 搜索耗时）。

**风险：** AgentContext 需确认线程安全（各 stage 写入不同字段，应无冲突）。

---

### P1-2：引入内存缓存层

**现状：** 整个后端只有 2 处 `lru_cache`，无系统性缓存。每次请求都重新计算/查询：
- `get_schema()` 遍历整个 `_FIELD_DEFINITIONS` 字典（结果固定不变）
- 股票名称解析（`NameToCodeResolver`）反复查询 DB
- 历史分析报告查询（相同参数的重复查询）

**修复方案（不引入新依赖，用 functools + cachetools 即可）：**

```python
# 1. 固定不变的 Schema 直接用 @functools.cache
from functools import cache

class SystemConfigService:
    @cache
    def get_schema(self) -> Dict[str, Any]:
        return build_schema_response(...)

# 2. 名称解析用 TTLCache（已在 requirements 范围内的 cachetools）
from cachetools import TTLCache
_name_cache: TTLCache = TTLCache(maxsize=5000, ttl=3600)

def resolve_name_to_code(name: str) -> Optional[str]:
    if name in _name_cache:
        return _name_cache[name]
    result = _resolve_from_db(name)
    _name_cache[name] = result
    return result

# 3. 历史查询 TTLCache
_history_cache: TTLCache = TTLCache(maxsize=200, ttl=300)  # 5 分钟
```

**收益：** 设置页加载速度提升（`get_schema()` 免重复计算）；高频名称解析 QPS 提升；相同股票短时间内多次查询历史减少 DB 压力。

---

## P2：结构优化（可分多个 PR 推进）

### P2-1：`config_registry.py` 外置为 YAML

**现状：** `src/core/config_registry.py` 有 2,978 行，全部是 Python 字典形式的配置字段元数据（category、title、description、default_value 等），代码可读性低，PR 冲突频率高。

**修复方案：**

```yaml
# src/core/config_registry.yaml（新文件）
fields:
  STOCK_LIST:
    title: Stock List
    description: Comma-separated watchlist stock codes.
    category: base
    data_type: array
    ui_control: textarea
    is_required: false
    default_value: "600519,300750,002594"
  LLM_CHANNEL:
    title: LLM Channel
    ...
```

```python
# src/core/config_registry.py（精简后）
import yaml
from pathlib import Path

_REGISTRY_PATH = Path(__file__).parent / "config_registry.yaml"

def _load_registry() -> Dict[str, Any]:
    with open(_REGISTRY_PATH) as f:
        return yaml.safe_load(f)

_FIELD_DEFINITIONS = _load_registry()["fields"]
```

**收益：** config_registry.py 从 3000 行缩减到 ~50 行；YAML 更易读、更易 diff；非开发者可以直接修改字段说明。

---

### P2-2：异常处理收敛

**现状：** 全代码库 426 处 `except Exception`，大量静默失败：

```python
# 常见模式（遍布代码库）
try:
    result = do_important_thing()
except Exception as e:
    logger.error(f"failed: {e}")
    return None  # 调用方不知道失败
```

**修复原则：**

1. 在 `data_provider/` 层：保持 `except Exception → DataFetchError`（降级逻辑需要）
2. 在 `src/services/` 层：预期外的异常应向上传播或触发告警，不能静默
3. 在 API 层：所有 endpoint 统一由 `add_error_handlers` 处理，service 层不需要自己 catch

```python
# 目标模式
try:
    result = fetch_data(code)
except (NetworkError, TimeoutError) as e:
    # 预期失败：降级到下一个数据源
    raise DataFetchError(str(e)) from e
# 非预期异常：让它向上传播，让 API 错误处理器捕获并返回 500
```

---

### P2-3：`data_provider/base.py` 职责分离

**现状：** `data_provider/base.py` 2,741 行，混合了四个职责：
- `BaseFetcher` 抽象基类
- `DataFetcherManager` 调度逻辑
- 数据规范化函数
- 股票代码工具函数

**目标结构：**

```
data_provider/
  base_fetcher.py     # BaseFetcher 基类（~200 行）
  fetcher_manager.py  # DataFetcherManager（~400 行）
  normalize.py        # 数据标准化（~300 行）
  code_utils.py       # normalize_stock_code 等工具（~200 行）
  base.py             # 向后兼容的 re-export（保留空壳）
```

`base.py` 空壳保留向后兼容的导入，逐步迁移调用方：

```python
# data_provider/base.py（迁移期间的向后兼容层）
from .base_fetcher import BaseFetcher
from .fetcher_manager import DataFetcherManager
from .normalize import STANDARD_COLUMNS
from .code_utils import normalize_stock_code, canonical_stock_code, is_bse_code
```

---

## P3：长期改进

### P3-1：Config 依赖注入化

**现状：** `get_config()` 被 50+ 个函数/方法直接调用，使测试和配置隔离困难。

**目标：** 新增的 Service / API endpoint 统一通过构造函数或 `Depends()` 接收 config，不在函数体内调用 `get_config()`。

```python
# 目标写法（Service）
class AlertWorker:
    def __init__(self, config: Config, db_manager: DatabaseManager):
        self.config = config
        ...

# 目标写法（API endpoint）
@router.get("/alerts")
async def list_alerts(config: Config = Depends(get_config_dep)):
    ...
```

**推进方式：** 新文件强制采用注入模式；存量代码随功能迭代逐步迁移，不做整体重构。

---

### P3-2：数据库升级 PostgreSQL（可选）

**当前状态：** SQLite 完全满足当前负载（每天百级写入，千级查询）。以下情况才需要升级：

- 并发写入冲突（SQLite 写锁）
- 数据量超过 10GB
- 需要多实例部署（SQLite 无法共享）

**升级路径：** 修改 `Config.get_db_url()` 返回 PostgreSQL 连接字符串，SQLAlchemy 屏蔽差异，其余代码无需修改。需要同步迁移 SQLite 历史数据（`scripts/migrate_db.py`，待创建）。

---

## 优先级总结

| 优先级 | 问题 | 理由 | 参考文件 |
|---|---|---|---|
| P0 | API sync 阻塞事件循环 | 直接影响并发可用性 | `api/v1/endpoints/analysis.py` |
| P0 | `_load_from_env` 703行拆分 | 高风险改动集中，PR 冲突频繁 | `src/config.py` |
| P1 | 多 Agent 并行化 | 用户可见延迟，改动面小 | `src/agent/orchestrator.py` |
| P1 | 引入内存缓存 | 无新依赖，立竿见影 | `src/services/*.py` |
| P2 | config_registry 外置 YAML | 减少 3000 行代码 | `src/core/config_registry.py` |
| P2 | 异常处理收敛 | 渐进式，不影响功能 | 全代码库 |
| P2 | `base.py` 职责分离 | 结构性重构，需配套测试 | `data_provider/base.py` |
| P3 | Config 依赖注入 | 测试隔离改善 | `src/config.py` + 调用方 |

---

## 相关文档

- [系统架构总览](architecture/ARCHITECTURE.md)
- [Agent 流水线](architecture/agent-pipeline.md)
- [数据源系统](architecture/data-provider.md)
