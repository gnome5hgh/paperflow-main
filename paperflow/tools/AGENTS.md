# paperflow/tools/

## Scope

- 本文件覆盖 `tools/` 原子工具层；一工具一文件，按域分包。

## Directory Structure

```text
tools/
├─ file/           # 读/写/编辑/glob/grep/read_pdf + atomic.py 原子写盘（文本/bytes 两入口）
│                  #   pdf_extract.py：read_pdf 的本地 PDF 抽取（PyMuPDF 直读，不经 RAG 栈）
├─ search/         # fetch_pdf(SSRF 校验+写盘后索引热更新) + _common.py 标题规范化/运行期状态再导出
├─ review/         # submit_review / submit_download_review（审查裁决工具）+ format_check
│                  #   format_check：笔记标题树对模板；模板是 review-note skill 的资源，经 SkillRegistry 解析（needs_skill_registry）
├─ rank/           # lookup_venue_rank（期刊/会议等级）
├─ citations/      # 6 引用工具（lookup/add/format/list/sync/remove；全部只装 citation-agent）
├─ rag/            # rag_retrieve + index_paths + reindex_all + index_status（读写与体检同域，只装 rag-agent）
├─ vision/         # analyze_figures（needs_parent=True，视觉调用归属父轮次审计）
├─ memory/         # 11 个记忆工具：get_memory_tools() 惰性单例 + set/get_memory_context（全装 memory-agent）
├─ orchestration/  # spawn_sub_agent + ask_user_question + confirm 原语；constants/ 放 SubAgentStatus
├─ skills/         # load_skill（渐进披露 L2/L3）
└─ common/         # make_tools 装配工厂 + _http.py 共享 HTTP 基础设施
```

## Core Rules

- **`__init__.py` 统一再导出**：消费方从 `paperflow.tools` 导入，符号名稳定、内部路径随便拆。
- **装配经 `make_tools`**：按 `root_hints` 生成目录提示、`default_write_root` 盖章到写工具、注入 `_config`；agent 拿到什么工具由 `cli.py` 装配层决定，不在工具内自作主张。
- **安全元数据必填**：每个 Tool 声明 `risk_level`/`side_effects`/`requires_confirm` 等，注册表加载时校验合法值；覆盖/删除类写操作 `requires_confirm=True` 逐次确认（可重入的批量追加不强求，如 add_citation/sync_citations）。
- **spawn 是唯一子 agent 通道**：未知类型 → spawn 白名单 → 同批同指纹去重（只拦同一批内的机械重复）→ 审稿预算 → 每轮派发总量，**五道闸**按此顺序都在 `orchestration/spawn.py` 的 `_admit`；去重注册表、失败计数、预算计数、产物账本、在途写占用由 `paperflow/core/agent/state.py` 的 session/run 容器持有，轮数预算与每轮上限下沉代码不靠 LLM 自律（意图只作信号，顺序与并行归 supervisor）。同路径写互斥不在闸里——写工具按真实写目标在 `RunState.writing_paths` 登记写占用、跨实例当场拒绝，判定在 runtime 执行层。
- **失败降级为文本**：工具异常转错误 ToolResult 回传 LLM 自行决策；ask_user 回调缺失时 fail-safe 返回提示，绝不挂起。

## Key Entry Points

- `__init__.py` — 全量再导出
- `common/__init__.py` — `make_tools(config, tool_items, default_write_root=None)`
- `orchestration/spawn.py` — SpawnSubAgentTool（`_admit` 五道闸 + 去重/预算/摘要）
- `memory/__init__.py` — `get_memory_tools()`（模块级单例，11 件全装给 `memory-agent`）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 插件侧工具声明：[`../../agents/AGENTS.md`](../../agents/AGENTS.md)
- 意图/记忆/安全等运行层：[`../core/AGENTS.md`](../core/AGENTS.md)
