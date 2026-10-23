# 工具可靠性与证据冲突处理 — 变更摘要

> 提交：`ad8705c feat: tool timeout + error classification + evidence conflict handling`
> 日期：2026-10-24
> 涉及文件：`app/config.py` / `app/tools/registry.py` / `app/agent/prompts.py` / `app/agent/state.py` / `app/agent/outcome.py` / `app/tools/selftest.py` / `README.md`

---

## 一、变更背景

对照作业的 17 项验收清单核查时发现两项部分达标缺口：

1. **工具执行可靠性不足** — `safe_execute` 只兜底异常，无超时控制、无错误类型区分、空结果被当作成功返回
2. **证据冲突无处理机制** — 多数据源矛盾时无规则指导 Agent 裁决，`resolved` 状态也可能携带未解决冲突

本次修复目标：让工具失败可分级处理、可超时熔断，让证据冲突被显式记录并被代码护栏拦截。

---

## 二、工具超时实现逻辑

**核心思路**：用 `ThreadPoolExecutor` 包裹 `tool.run()`，通过 `future.result(timeout=N)` 施加超时，超时则返回结构化错误而非阻塞整轮。

### 配置入口 — `app/config.py`

```python
# ---- 工具执行护栏 ----
tool_timeout_seconds: int = 15  # 单个工具调用最大耗时，超时返回 timeout 错误
```

### 执行逻辑 — `app/tools/registry.py`

```python
timeout = getattr(ctx.settings, "tool_timeout_seconds", 15)
try:
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(tool.run, ctx, **tc.arguments)
        data = fut.result(timeout=timeout)
except FuturesTimeout:
    return {"status": "error", "error_type": "timeout",
            "error": f"工具 {tc.name} 执行超时（>{timeout}s）"}
```

### 设计要点

- 超时时间从 `Settings` 读取，默认 15s，可通过 `.env` 的 `TOOL_TIMEOUT_SECONDS` 覆盖
- 用线程池而非 `signal.alarm`（Windows 不支持信号超时，线程池可跨平台）
- 超时返回 `status: error`，`loop._execute_tool` 能将其记入 `ToolRecord` 并注入提示，Agent 可据此换工具或升级

---

## 三、错误分类实现逻辑

**核心思路**：所有失败路径统一返回 `{status: error, error_type: <类型>, error: <描述>}`，让 LLM 能区分失败原因并采取不同策略。

### 分类表 — `app/tools/registry.py`

| error_type | 触发条件 | Agent 应采取的策略 |
|---|---|---|
| `unknown_tool` | `self._tools` 中找不到工具名 | 换一个已有工具 |
| `service_error` | chaos 注入故障 / 工具抛通用异常 | 该数据源暂时不可信，换替代工具 |
| `timeout` | 执行超过 `tool_timeout_seconds` | 同上，换工具 |
| `argument_error` | `TypeError`（参数名/数量不对） | 按返回的 schema 提示修正参数重试 |
| `not_found` | 检索工具返回 0 条结果 | 该源无相关信息，**不等于"一切正常"**，结合其他源判断 |

### 提示词侧分级处理 — `app/agent/prompts.py`

```
工具错误分级处理：工具返回 error 时看 error_type 区分处理——
timeout / service_error → 该数据源暂时不可信，换替代工具继续调查；
not_found → 该数据源没有相关信息（不等于"一切正常"），需结合其他数据源判断；
argument_error → 按 schema 提示修正参数后重试；
unknown_tool → 换一个已有工具。多个关键数据源均失败或无结果时升级 tool_failure
```

### 兼容性

`error` 字段保留，`loop._execute_tool` 现有的 `str(result.get("error", ""))[:120]` 摘要逻辑不受影响；`error_type` 是新增的可选字段。

---

## 四、空结果处理

**问题**：`search_kb` 无匹配时返回 `{"results": []}`，原逻辑把它当 `status: ok`，LLM 容易把"没查到"误判为"这个问题没有已知方案"或"一切正常"。

### 修复 — `app/tools/registry.py`

```python
_EMPTY_RESULT_TOOLS = {"search_kb", "search_tickets"}  # lookup_user 不在此集合

# 执行成功后判定是否空结果
if tc.name in self._EMPTY_RESULT_TOOLS and self._is_empty(data):
    return {"status": "error", "error_type": "not_found",
            "error": f"{tc.name} 未返回匹配结果（无该问题的已知信息）"}

@staticmethod
def _is_empty(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    results = data.get("results") or data.get("users") or []
    return len(results) == 0
```

### 为什么排除 `lookup_user`

它返回 `match_count: 0` 的语义就是"未找到用户"，已在 `summarize_tool_result` 中处理为"未找到用户"，不应再包装为 `not_found` 错误，否则会干扰身份核验流程（如 `identity-fail-011`）。

---

## 五、证据冲突处理实现逻辑

**核心思路**：三层防护 — 提示词定义冲突处理规则 + 状态结构承载冲突字段 + 代码护栏强制拦截"带冲突的 resolved"。

### 5.1 状态层：承载冲突 — `app/agent/state.py`

`IssueContext` 新增 `conflicts` 字段：

```python
conflicts: List[str] = field(default_factory=list)
# 数据源矛盾点，如 "kb: 建议重启 vs system_status: 维护中"
```

- `merge()` 方法将 `conflicts` 纳入列表合并逻辑（去重追加）
- `to_prompt()` 把冲突注入系统提示，让 Agent 每轮都能看到未解决矛盾
- `Hypothesis.status` 枚举增加 `conflicted` 值

### 5.2 提示词层：定义裁决规则 — `app/agent/prompts.py`

```
证据冲突处理：当多个数据源给出矛盾结论时，不得强行选边站给出确定性结论。应：
① 在 issue_update.conflicts 中记录矛盾点（格式 "源A: 结论 vs 源B: 结论"）；
② 优先采信更权威的源：system_status 实时状态 > 当前 policy > KB 文章 > 历史工单先例；
③ 若无法裁决，置 state=escalated（reason=tool_failure）或 need_info 向用户补充信息；
resolved 状态下不允许存在未解决的 conflicts
```

输出契约的 `issue_update` schema 同步增加 `conflicts` 字段。

### 5.3 代码层：规则 0 冲突护栏 — `app/agent/outcome.py`

在硬边界（规则 1）之前插入：

```python
# 规则 0：证据冲突护栏
if state_name in ("resolved", "pending_user") and state.issue.conflicts:
    rewritten = dict(final)
    rewritten["state"] = "need_info"
    rewritten["confidence"] = min(conf, 0.5)
    rewritten["user_message"] = (
        "我查到的信息之间有矛盾，需要再确认一下："
        + "；".join(state.issue.conflicts[:2])
        + "。请告诉我哪个更符合你实际看到的情况？"
    )
    return Verdict("rewrite", rewritten, reason="conflict_guard", ...)
```

### 设计要点

- 优先级高于权限硬边界，冲突必须先于一切被澄清
- 只拦截 `resolved` 和 `pending_user`（确定性结论态），`escalated` 允许带冲突交接给人工
- 改写为 `need_info` 而非直接升级，给用户一次澄清机会；若用户也无法裁决，下一轮 Agent 可自行 escalate

### 5.4 升级摘要携带冲突 — `app/agent/outcome.py`

`complete_summary` 模板新增"证据冲突"行，确保人工交接时专家能看到矛盾点。

---

## 六、验证结果

| 验证项 | 结果 |
|---|---|
| selftest | ✅ 19 断言（新增 3 条守卫：`证据冲突处理` / `error_type` / `resolved 状态下不允许存在未解决的 conflicts`） |
| mock 评测 | ✅ 13/13，exit 0 |
| deepseek 评测 | ✅ **13/13**，4.8s/轮，192,387 tokens |
| 工作区 | ✅ 干净（已提交 `ad8705c`） |

### 影响评估

提示词加长导致单轮 token 从 157k→192k、延迟 4.0s→4.8s，仍在"2 分钟内解决"预算内（典型 2-3 轮对话 < 15s）。现有 13 用例无退化。

---

## 七、变更文件清单

| 文件 | 改动行数 | 内容 |
|---|---|---|
| `app/config.py` | +3 | 新增 `tool_timeout_seconds` 配置 |
| `app/tools/registry.py` | +42/-6 | 超时包装、错误分类、空结果 not_found |
| `app/agent/prompts.py` | +13/-1 | 工具错误分级规则、证据冲突处理规则、schema 字段 |
| `app/agent/state.py` | +5/-3 | `conflicts` 字段、`conflicted` 状态、merge/to_prompt 同步 |
| `app/agent/outcome.py` | +17/-1 | 规则 0 冲突护栏、摘要冲突行、规则说明更新 |
| `app/tools/selftest.py` | +3 | 3 条提示词守卫 |
| `README.md` | 指标更新 | deepseek 4.8s/192k tokens |
