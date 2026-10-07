# paperflow/tools/

## Scope

- 本文件覆盖 `tools/` 原子工具层；一工具一文件，按域分包。

## Directory Structure

```text
tools/
├─ file/           # 读/写/编辑/glob/grep/read_pdf/format_check + atomic.py 原子写盘
├─ search/         # fetch_pdf(SSRF 校验+写盘后索引热更新) + _common.py SearchRunState
├─ review/         # submit_review / submit_download_review（reviewer 裁决工具）
├─ rank/           # lookup_venue_rank（期刊/会议等级）
├─ citations/      # 4 引用工具（lookup/add/format/list）
├─ rag/            # rag_retrieve
├─ vision/         # analyze_figures（needs_parent=True，视觉调用归属父轮次审计）
├─ memory/         # 11 个记忆工具：get_memory_tools() 惰性单例 + set/get_memory_context
├─ orchestration/  # spawn_sub_agent + ask_user_question + confirm 原语
├─ skills/         # load_skill（渐进披露 L2/L3）
└─ common/         # make_tools 装配工厂 + _http.py 共享 HTTP 基础设施
```

## Core Rules

- **`__init__.py` 统一再导出**：消费方从 `paperflow.tools` 导入，符号名稳定、内部路径随便拆。
- **装配经 `make_tools`**：按 `root_hints` 生成目录提示、`default_write_root` 盖章到写工具、注入 `_config`；agent 拿到什么工具由 `cli.py` 装配层决定，不在工具内自作主张。
- **安全元数据必填**：每个 Tool 声明 `risk_level`/`side_effects`/`requires_confirm` 等，注册表加载时校验合法值；写类操作默认要求确认。
- **spawn 是唯一子 agent 通道**：意图派发门禁 → spawn 白名单 → 去重注册表 → 审稿预算 → 超时预算，五道闸都在 `orchestration/spawn.py`，轮数预算下沉代码不靠 LLM 自律。
- **失败降级为文本**：工具异常转错误 ToolResult 回传 LLM 自行决策；ask_user 回调缺失时 fail-safe 返回提示，绝不挂起。

## Key Entry Points

- `__init__.py` — 全量再导出
- `common/__init__.py` — `make_tools(config, tool_items, default_write_root=None)`
- `orchestration/spawn.py` — SpawnSubAgentTool 五道闸
- `memory/__init__.py` — `get_memory_tools()`（模块级单例，按角色分发装配）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 插件侧工具声明：[`../../agents/AGENTS.md`](../../agents/AGENTS.md)
- 意图/记忆/安全等运行层：[`../core/AGENTS.md`](../core/AGENTS.md)
