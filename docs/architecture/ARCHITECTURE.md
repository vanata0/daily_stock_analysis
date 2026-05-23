# 系统架构总览

本文档是 daily-stock-analysis（DSA）系统的架构参考。面向贡献者和二次开发者，解释各层边界、数据流向和核心设计决策。用户配置和部署见 [完整配置与部署指南](../full-guide.md)。

---

## 系统定位

DSA 是一个以"LLM 驱动分析 + 多通道推送"为核心的股票智能分析系统，覆盖 A 股、港股、美股。整体由三条主线构成：

1. **批量任务线**（`python main.py`）：定时拉取数据 → LLM 分析 → 生成报告 → 推送通知
2. **实时请求线**（FastAPI + 任务队列）：用户触发单股分析 → SSE 实时回流进度
3. **告警监控线**（`AlertWorker`）：定期评估用户规则 → 触发通知

---

## 顶层数据流

```
外部数据源
  ├─ efinance / akshare / tushare / pytdx / baostock
  ├─ yfinance / LongBridge / Finnhub / AlphaVantage
  └─ 搜索引擎: Tavily / SerpAPI / SearXNG / Anspire
         │
         ▼
  DataFetcherManager          ← data_provider/base.py
  (优先级 fallback 调度器)
         │
         ▼
  StockAnalysisPipeline       ← src/core/pipeline.py
  ┌──────────────────────────────────────┐
  │  1. fetch_and_save_stock_data        │  → SQLite (stock_daily)
  │  2. _enhance_context                 │  → 基本面 / 新闻 / 筹码
  │  3. analyze_stock                    │  → LLM / Agent
  │  4. _send_notifications              │  → 多渠道推送
  └──────────────────────────────────────┘
         │
         ▼
  通知渠道
  ├─ 飞书 / 钉钉 / Discord / Telegram
  ├─ 企业微信 / Slack / Email / Gotify / Ntfy
  └─ 自定义 Webhook / PushPlus / Pushover
```

---

## 目录边界

```
daily_stock_analysis/
├── main.py                  CLI 入口（批量分析 / 服务启动 / 调度）
├── server.py                uvicorn 服务入口 (create_app)
├── src/
│   ├── config.py            全局配置单例 (Config dataclass)
│   ├── storage.py           SQLite ORM + DatabaseManager
│   ├── analyzer.py          LLM 分析器 (GeminiAnalyzer / litellm)
│   ├── search_service.py    新闻/情报搜索服务
│   ├── notification.py      通知编排 (NotificationService)
│   ├── market_analyzer.py   大盘分析
│   ├── core/
│   │   ├── pipeline.py      主分析流水线 (StockAnalysisPipeline)
│   │   ├── config_registry.py  配置字段元数据注册表
│   │   ├── backtest_engine.py  策略回测引擎
│   │   └── trading_calendar.py 交易日历
│   ├── agent/
│   │   ├── orchestrator.py  多 Agent 流水线协调器
│   │   ├── executor.py      单 Agent 工具调用执行器
│   │   ├── factory.py       Agent 构建工厂
│   │   ├── agents/          各专项 Agent 实现
│   │   ├── tools/           Agent 可调用工具集
│   │   ├── skills/          策略技能模块
│   │   └── llm_adapter.py   LLM 调用适配层
│   ├── services/            业务服务层
│   │   ├── task_queue.py    异步任务队列 + SSE 广播
│   │   ├── alert_service.py 告警规则 CRUD
│   │   ├── alert_worker.py  告警轮询 Worker
│   │   ├── portfolio_service.py 组合管理
│   │   ├── system_config_service.py 配置读写服务
│   │   └── history_service.py 历史报告服务
│   ├── repositories/        数据访问层 (SQLAlchemy models)
│   ├── schemas/             报告 Schema 定义
│   └── notification_sender/ 各渠道发送器实现
├── data_provider/           多数据源适配层 (10+ fetchers)
├── api/
│   ├── app.py               FastAPI 应用工厂
│   ├── deps.py              依赖注入 (DB session / Config)
│   └── v1/endpoints/        REST API 端点
├── apps/
│   ├── dsa-web/             React 19 + Vite 前端
│   └── dsa-desktop/         Electron 桌面端
├── bot/                     IM Bot 接入层 (飞书 / 钉钉 / Discord)
├── tests/                   pytest 单元测试 (135 个文件)
└── .github/workflows/       CI + 每日分析 + 发布流水线
```

---

## 核心组件详解

### 1. 配置管理（Config）

**文件：** `src/config.py`

`Config` 是全局单例 dataclass，通过 `get_config()` 获取。所有配置从 `.env` 文件加载，运行时通过 `SystemConfigService` 热更新。

```python
from src.config import get_config
config = get_config()
print(config.stock_list)        # ['600519', 'hk00700', 'AAPL']
print(config.llm_channel)       # 'gemini'
```

**重要：** 新代码优先通过 FastAPI 依赖注入获取配置，不要在函数内部直接调用 `get_config()`：

```python
# ✅ API 层推荐方式
from api.deps import get_config_dep
from fastapi import Depends

@router.get("/foo")
async def endpoint(config = Depends(get_config_dep)):
    ...

# ✅ 长生命周期对象推荐方式
class MyService:
    def __init__(self, config: Config):
        self.config = config
```

配置热更新路径：Web 设置页 → `POST /api/v1/system-config/` → `SystemConfigService.update()` → `setup_env(override=True)` → `Config.reset_instance()` → `_reload_runtime_singletons()`。

---

### 2. 数据库（Storage）

**文件：** `src/storage.py`

使用 SQLAlchemy 2.0 + SQLite（默认路径 `data/stock_data.db`）。`DatabaseManager` 是单例，通过 `DatabaseManager.get_instance()` 获取。

主要数据表：

| 表名 | 用途 |
|---|---|
| `stock_daily` | 每日 OHLCV + 技术指标 |
| `analysis_history` | LLM 分析结果历史 |
| `news_intel` | 新闻情报缓存 |
| `fundamental_snapshot` | 基本面数据快照 |
| `portfolio_*` | 组合持仓和历史 |
| `alert_rules` | 告警规则 |
| `alert_triggers` | 告警触发记录 |
| `llm_usage` | LLM Token 用量统计 |

Session 使用示例：

```python
db = DatabaseManager.get_instance()
session = db.get_session()
try:
    # 操作
    session.commit()
finally:
    session.close()
```

FastAPI 端点通过 `Depends(get_db)` 获取 session（自动关闭）。

---

### 3. 分析流水线（Pipeline）

**文件：** `src/core/pipeline.py` → `StockAnalysisPipeline`

这是系统的核心协调器。单股分析的完整生命周期：

```
analyze_stock(code, report_type, query_id)
    │
    ├─ 1. fetch_and_save_stock_data()
    │      ├─ DataFetcherManager.get_daily_data()   → K线/技术指标
    │      ├─ DataFetcherManager.get_realtime_quote() → 实时行情
    │      └─ storage.save_daily_data()             → SQLite
    │
    ├─ 2. _enhance_context()
    │      ├─ SearchService.search_comprehensive_intel() → 新闻搜索
    │      ├─ DataFetcherManager.get_chip_distribution() → 筹码分布
    │      └─ 基本面快照 / 所属板块                  → 上下文组装
    │
    ├─ 3. LLM 分析（两条路径）
    │      ├─ 路径 A：_analyze_with_agent()          → AgentOrchestrator
    │      │         (多 Agent 分阶段深度分析)
    │      └─ 路径 B：GeminiAnalyzer.analyze()       → 单次 LLM 调用
    │                 (传统单步分析)
    │
    └─ 4. _send_notifications()
           ├─ _send_single_stock_notification()      → 各渠道
           └─ _send_email_group()                    → 邮件分组
```

**选择路径 A 还是 B** 取决于 `AGENT_MODE` 配置。路径 A（Agent 模式）支持工具调用、多步推理和专项 Agent 协作，路径 B 是单次 Prompt 调用，速度更快。

---

### 4. 多 Agent 系统

见 [Agent 流水线文档](agent-pipeline.md) 获取完整说明。

Agent 模式的调用链：

```
StockAnalysisPipeline._analyze_with_agent()
    │
    └─ build_agent_executor(config, ...)
           │
           ├─ 返回 AgentOrchestrator   (多 Agent 模式，默认)
           └─ 返回 AgentExecutor       (单 Agent ReAct 模式)
```

---

### 5. 任务队列（TaskQueue）

**文件：** `src/services/task_queue.py` → `AnalysisTaskQueue`

Web 触发的分析任务通过异步任务队列管理，不阻塞 HTTP 请求。

```
POST /api/v1/analysis/analyze
    │
    └─ task_queue.submit_task(stock_code, ...)
           │
           ├─ 防重复检查（相同股票正在分析 → 409）
           ├─ ThreadPoolExecutor 异步执行
           └─ 进度广播（SSE）

GET /api/v1/analysis/tasks/stream   ← SSE 长连接
    └─ asyncio.Queue → event_generator() → text/event-stream
```

任务状态流转：`PENDING → PROCESSING → COMPLETED / FAILED`

---

### 6. 告警系统

**文件：** `src/services/alert_service.py` + `alert_worker.py`

告警系统采用**规则 + 轮询**模式：

```
AlertWorker.run_once()              ← 定时调用（通常每 5 分钟）
    │
    ├─ _load_runtime_rules()        ← 从配置读取内存规则
    ├─ _load_legacy_rules()         ← 从配置读取旧格式规则
    ├─ 对每条规则：_evaluate_rule() → AlertService
    │      ├─ 价格规则              → EventMonitor 实时行情
    │      ├─ 涨跌幅规则            → 实时行情计算
    │      ├─ 成交量规则            → 日线数据
    │      └─ 技术指标规则          → MA/RSI/MACD/KDJ/CCI 计算
    │
    ├─ _should_notify()             ← 冷却检查（默认 24h）
    └─ _send_notification()         ← NotificationService
```

告警规则持久化在 `alert_rules` 表（通过 Web 管理），冷却状态保存在 `alert_triggers` 表。

---

### 7. API 层

**文件：** `api/app.py` + `api/v1/endpoints/`

FastAPI 应用通过 `create_app()` 工厂创建，支持 SPA 前端托管（生产）和 API 独立运行（开发）。

主要路由组：

| 路由前缀 | 文件 | 说明 |
|---|---|---|
| `/api/v1/analysis` | `analysis.py` | 触发分析 / 任务状态 / SSE |
| `/api/v1/history` | `history.py` | 历史报告查询 |
| `/api/v1/stocks` | `stocks.py` | 股票信息 / 行情 |
| `/api/v1/system-config` | `system_config.py` | 配置读写 |
| `/api/v1/portfolio` | `portfolio.py` | 组合管理 |
| `/api/v1/alerts` | `alerts.py` | 告警规则 CRUD |
| `/api/v1/agent` | `agent.py` | Agent 对话 |
| `/api/v1/backtest` | `backtest.py` | 策略回测 |
| `/api/health` | — | 健康检查 |

认证：可选 Cookie/Token 认证，通过 `src/auth.py` 实现，由 `add_auth_middleware(app)` 注入。

---

## 前端架构

**路径：** `apps/dsa-web/`

技术栈：React 19 + TypeScript + Vite + Tailwind CSS + Zustand + Recharts

```
apps/dsa-web/src/
├── pages/          路由页面
├── components/     通用组件
├── stores/         Zustand 状态
├── api/            Axios 请求封装
└── hooks/          自定义 Hook（含 SSE useTaskStream）
```

桌面端（`apps/dsa-desktop/`）是 Electron 壳，内嵌 Web 产物，通过 FastAPI 服务通信。

---

## CI 与发布流程

主要 Workflow：

| 文件 | 触发 | 说明 |
|---|---|---|
| `ci.yml` | PR / push | ai-governance + backend-gate + docker-build + web-gate |
| `daily_analysis.yml` | cron 每日 | 自动执行分析任务并推送报告 |
| `auto-tag.yml` | push to main | 提取 `#patch/#minor/#major` 自动打 tag |
| `docker-publish.yml` | tag | 发布 Docker 镜像到 GHCR + DockerHub |
| `desktop-release.yml` | tag | 打包并发布 Electron 桌面端 |

CI 阻断规则：`ai-governance`（AGENTS.md 治理）、`backend-gate`（`./scripts/ci_gate.sh`）、`docker-build`（镜像构建）、`web-gate`（前端构建）任一失败即阻断合入。

---

## 关键设计决策

### 为什么用 SQLite 而非 PostgreSQL？

SQLite 零配置、单文件、易于备份，适合个人和小团队部署场景。系统的写入量（每天几十到几百条分析记录）和查询量（主要是历史查询）都在 SQLite 的舒适区间内。切换 PostgreSQL 只需修改 `Config.get_db_url()` 的连接字符串，SQLAlchemy 屏蔽了差异。

### 为什么 LLM 调用用 litellm？

litellm 统一了 OpenAI-compatible、Anthropic、Gemini、DeepSeek、Ollama 等 20+ 提供商的调用接口，使系统的模型层可以在不改动业务代码的情况下切换 LLM 供应商。`src/llm/llm_adapter.py` 是对 litellm 的二次封装，处理重试、参数适配和流式回调。

### 为什么多数据源而非单一数据源？

A 股数据源具有高不稳定性（爬虫封禁、API 限额、数据延迟），任何单一来源都无法保证 99% 可用性。DataFetcherManager 的优先级 fallback 链保证单一数据源失败不影响整体分析流程。详见 [数据源文档](data-provider.md)。

### 为什么 Agent 分多个 stage？

单一大 Prompt 在复杂个股分析中容易产生表面化结论。将分析拆分为 Technical（技术面）→ Intel（情报/新闻）→ Risk（风险）→ Decision（决策）的专项阶段，每个 Agent 可以深度聚焦，最终由 Decision Agent 综合各阶段结论。详见 [Agent 流水线文档](agent-pipeline.md)。

---

## 相关文档

- [数据源参考](data-provider.md) — 所有 fetcher 的说明、优先级规则和新增方法
- [Agent 流水线](agent-pipeline.md) — 多 Agent 架构和模式说明
- [优化路线图](../OPTIMIZATION.md) — 已识别的技术债和优先级
- [API 规格](api_spec.json) — OpenAPI JSON 原始规格
- [完整配置与部署指南](../full-guide.md) — 面向用户的部署和配置说明
- [贡献指南](../../docs/CONTRIBUTING.md) — 开发环境搭建和 PR 规范
