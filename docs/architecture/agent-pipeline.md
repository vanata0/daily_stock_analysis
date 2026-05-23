# Agent 流水线

本文档描述 DSA 多 Agent 系统的架构、运行模式、各阶段职责，以及 Agent 输出的数据结构。

---

## 为什么需要多 Agent？

单一大 Prompt 分析复杂个股存在两个问题：

1. **上下文稀释：** 把技术面、新闻情报、风险评估和最终建议塞进一个 Prompt，每个维度的分析深度都受限于模型的注意力分布。
2. **结论表面化：** 没有专注于某一维度的"专家角色"，模型倾向于给出四平八稳的通用结论。

多 Agent 方案的核心是**分治**：每个 Agent 专注一个维度，不知道其他 Agent 的分析，最后由 Decision Agent 综合各方意见给出最终判断。

---

## 整体架构

```
AgentOrchestrator（协调器）
    │
    ├─ Step 1: TechnicalAgent    → 技术面分析（K线形态/指标/支撑阻力）
    │
    ├─ Step 2: IntelAgent        → 情报分析（新闻/公告/舆情）
    │          （standard/full/specialist 模式才运行）
    │
    ├─ Step 3: RiskAgent         → 风险评估（流动性/估值/政策风险）
    │          （full/specialist 模式才运行）
    │
    ├─ Step 4: SpecialistAgent   → 专项分析（策略 Skill / 行业分析）
    │          （specialist 模式才运行）
    │
    └─ Step 5: DecisionAgent     → 综合决策（汇总所有 Agent 意见）
```

每个 Agent 读取共享的 `AgentContext`（含股票数据、已有分析结果、历史背景），向 context 写入自己的分析结论，传递给下一个 Agent。

---

## 运行模式

通过 `AGENT_MODE` 配置（默认 `standard`）或 Web 分析请求参数指定：

| 模式 | 运行阶段 | LLM 调用次数 | 适用场景 |
|---|---|---|---|
| `quick` | Technical → Decision | ~2 次 | 快速概览、高频扫描 |
| `standard` | Technical → Intel → Decision | ~3 次 | 日常分析（默认） |
| `full` | Technical → Intel → Risk → Decision | ~4 次 | 深度评估 |
| `specialist` | Technical → Intel → Risk → Specialist → Decision | ~5+ 次 | 策略回测联动、行业专项 |

**如何选择：** 默认 `standard` 适合大多数场景。分析结论信心不足时用 `full`；有特定策略（如均线策略、成长股筛选）时用 `specialist`。

---

## AgentContext（共享上下文）

**文件：** `src/agent/protocols.py` → `AgentContext`

```python
@dataclass
class AgentContext:
    # 输入
    stock_code: str             # 股票代码
    task: str                   # 分析任务描述
    report_language: str        # 报告语言

    # 市场数据
    kline_data: Optional[pd.DataFrame]     # 历史 K线
    realtime_quote: Optional[dict]         # 实时行情
    fundamental: Optional[dict]            # 基本面
    chip_distribution: Optional[dict]      # 筹码分布
    news_intel: Optional[list]             # 新闻情报

    # 各阶段 Agent 写入
    stage_results: List[StageResult]       # 各阶段原始输出
    technical_summary: Optional[str]       # TechnicalAgent 结论
    intel_summary: Optional[str]           # IntelAgent 结论
    risk_summary: Optional[str]            # RiskAgent 结论
    specialist_opinions: List[dict]        # SpecialistAgent 意见列表

    # 元数据
    meta: Dict[str, Any]                   # 运行时元数据
    started_at: float                      # 开始时间戳
```

---

## 各 Agent 职责

### TechnicalAgent

**文件：** `src/agent/agents/technical_agent.py`

**职责：** 技术面量化分析，不引入主观判断。

分析内容：
- K 线形态识别（双底、头肩顶、旗形等）
- 均线系统（MA5/10/20/60 多空排列）
- MACD 金叉/死叉，RSI 超买超卖区间
- KDJ、CCI、BOLL 通道
- 支撑位/阻力位（历史高低点、整数关口）
- 量价关系（量比、换手率趋势）

**工具调用：** 调用 `data_tools.get_kline_data`、`analysis_tools.compute_technical_indicators`

输出写入：`ctx.technical_summary`（字符串形式的技术面结论）

---

### IntelAgent

**文件：** `src/agent/agents/intel_agent.py`

**职责：** 信息面和情报分析，聚焦对股价的潜在影响。

分析内容：
- 近期重要公告（业绩预告、定增、股权变动）
- 行业政策动态
- 市场舆情倾向
- 重大事件影响评估

**工具调用：** 调用 `search_tools.search_news`、`data_tools.get_news_intel`

**特殊机制：** 搜索新闻时会过滤明显无关内容（娱乐、无关行业），使用 `SearchService.search_comprehensive_intel()` 的结果。

输出写入：`ctx.intel_summary`

---

### RiskAgent

**文件：** `src/agent/agents/risk_agent.py`

**职责：** 识别持有或买入该股票的风险因子。

分析维度：
- 估值风险（PE/PB 历史分位）
- 流动性风险（换手率、成交量萎缩）
- 政策敏感度（所属行业的监管风险）
- 技术面风险（接近重要阻力位、趋势破位风险）
- 基本面恶化信号（毛利率下降、应收账款扩张）

输出：风险等级（低/中/高）+ 具体风险因子列表

写入：`ctx.risk_summary`

---

### SpecialistAgent（策略专项）

**文件：** `src/agent/agents/`、`src/agent/skills/`

仅在 `specialist` 模式下运行。根据用户在分析请求中指定的策略 Skill（如均线策略、成长股策略、价值投资策略），从 `src/agent/skills/` 加载对应的策略描述，由专项 Agent 执行该策略维度的评估。

多个策略 Skill 并发执行，结果由 `SkillAggregator` 聚合后传入 DecisionAgent。

---

### DecisionAgent

**文件：** `src/agent/agents/decision_agent.py`

**职责：** 综合所有 Agent 意见，输出最终操作建议和仪表盘数据。

输出 Dashboard JSON：

```json
{
  "decision": "买入 | 持有 | 观望 | 减仓 | 卖出",
  "confidence": 75,            // 置信度 0-100
  "target_price": 185.0,       // 目标价（元）
  "stop_loss": 168.0,          // 止损价
  "trend_score": 68,           // 趋势评分 0-100
  "trend_label": "上升趋势",
  "advice": "当前 MACD 金叉确认，...",
  "key_support": 170.0,
  "key_resistance": 190.0,
  "risk_level": "中",
  "risk_factors": ["估值偏高", "成交量萎缩"],
  "catalysts": ["行业景气度回升", "半年报预期向好"]
}
```

DecisionAgent 在读取所有前置 Agent 结论时不知道它们的具体 Prompt，只接收结构化的 `summary` 字段，保证决策独立性。

---

## 工具系统

**文件：** `src/agent/tools/`

Agent 可调用以下工具（通过 `ToolRegistry` 注册）：

| 工具 | 文件 | 说明 |
|---|---|---|
| `get_kline_data` | `data_tools.py` | 获取历史 K 线 DataFrame |
| `get_realtime_quote` | `data_tools.py` | 获取实时行情 |
| `get_fundamental` | `data_tools.py` | 获取基本面数据 |
| `get_chip_distribution` | `data_tools.py` | 获取筹码分布 |
| `get_daily_history_cache` | `data_tools.py` | 从 DB 读取历史数据（带缓存） |
| `search_news` | `search_tools.py` | 搜索近期新闻情报 |
| `compute_technical_indicators` | `analysis_tools.py` | 计算技术指标 |
| `run_backtest` | `backtest_tools.py` | 执行策略回测 |
| `get_market_overview` | `market_tools.py` | 获取大盘概况 |

工具调用通过 `LLMToolAdapter` 执行，支持流式输出回调和超时控制。

---

## 时间预算（Timeout）

`AgentOrchestrator` 维护整体流水线的时间预算：

```python
# 配置项: AGENT_TIMEOUT_SECONDS（默认 300s）
# 每个 stage 开始前检查剩余时间
if remaining_budget < min_stage_budget:
    # 跳过该 stage，直接进入 DecisionAgent
    return _build_budget_skip_result(...)
```

当时间预算不足时，跳过尚未运行的 stage（不是强制中断），保证 DecisionAgent 始终能基于已有结论给出输出。

---

## 与 AgentExecutor 的区别

系统有两个 Agent 运行时：

| | `AgentOrchestrator` | `AgentExecutor` |
|---|---|---|
| **文件** | `orchestrator.py` | `executor.py` |
| **模式** | 多 Agent 流水线 | 单 Agent ReAct 循环 |
| **工具调用** | 各阶段 Agent 各自调用 | 单 Agent 循环调用多工具 |
| **适用** | 深度个股分析（默认） | 对话式问答 / Agent Chat |
| **接口** | `.run(task, context)` | `.run(task, context)` |

两者通过 `api/v1/endpoints/agent.py` 的 `/api/v1/agent/chat` 端点公开，前端统一通过该端点交互。分析报告任务走 `AgentOrchestrator`，用户对话走 `AgentExecutor`。

工厂函数 `build_agent_executor()` 根据配置决定返回哪个：

```python
# src/agent/factory.py
def build_agent_executor(config, ...):
    if config.agent_orchestrator_mode:
        return AgentOrchestrator(mode=config.agent_mode, ...)
    return AgentExecutor(...)
```

---

## 扩展：添加新的 Agent

1. 在 `src/agent/agents/` 创建 `my_agent.py`，继承 `BaseAgent`：

```python
from .base_agent import BaseAgent
from src.agent.protocols import AgentContext, StageResult

class MyAgent(BaseAgent):
    name = "my_agent"

    def run(self, ctx: AgentContext, **kwargs) -> StageResult:
        # 读取 ctx 数据，调用工具，写入 ctx
        result = self._call_llm(
            system_prompt="你是...",
            user_message=self._build_prompt(ctx),
        )
        ctx.meta["my_agent_result"] = result
        return StageResult(status=StageStatus.DONE, summary=result)
```

2. 在 `orchestrator.py` 的 `_build_agent_chain()` 中按需插入：

```python
def _build_agent_chain(self, ctx: AgentContext) -> list:
    chain = [TechnicalAgent(...)]
    if self.mode in ("standard", "full", "specialist"):
        chain.append(IntelAgent(...))
    if self.mode in ("full", "specialist"):
        chain.append(MyAgent(...))   # 插入到 RiskAgent 之前
        chain.append(RiskAgent(...))
    chain.append(DecisionAgent(...))
    return chain
```

---

## 相关文档

- [系统架构总览](ARCHITECTURE.md)
- [数据源系统](data-provider.md)
- [优化路线图](../OPTIMIZATION.md)
