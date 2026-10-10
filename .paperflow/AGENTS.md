# .paperflow/

## Scope

- 本文件覆盖 `.paperflow/` 用户级数据目录。它同时是**两样东西**：Skill 插件的根目录（`skills/`，内置与用户安装同层、单级结构），以及**运行时数据根**（`config.runtime.workspace` 的默认值，记忆/审计/会话历史/Milvus 卷等都落在这里）。
- 入库的只有静态资产：本 manifest、`skills/`、`intent/`；其余子目录是运行期产物，gitignore 已盖（反选规则见根 `.gitignore`）。

## Directory Structure

```text
.paperflow/
├─ AGENTS.md        # 本 manifest（入库）
├─ skills/          # Skill 插件唯一根目录（入库）
│  └─ <skill-name>/ # 一个 skill 一个目录，目录名 = frontmatter 的 name
│     ├─ SKILL.md   # agentskills.io 格式：frontmatter(name/description/…) + 正文
│     ├─ tools.py   # 可选：module-level TOOLS，经 merge_tools 并入子 agent 工具表
│     └─ references/# 可选：L3 资源（load_skill(name=..., resource=...) 读取，路径围栏限本目录内）
├─ intent/          # 意图知识库（taxonomy.yaml + rules.yaml，随仓库发布，入库）
├─ memory/          # 记忆库（memory.db + MemFS markdown 投影，Git 变体在此另起一个 git 仓库）
├─ security/audit/  # 审计 JSONL（按日落盘）
├─ session/         # REPL 输入历史（repl_history.txt）
├─ rag/             # 索引状态文件（index_state.json，带配方哈希）
├─ citations/       # references.bib 默认落点（引用库真相源）
└─ infra/milvus/    # Milvus Standalone 数据卷（etcd / minio / milvus 三份）
```

## Core Rules

- **agentskills.io 规范强制**：frontmatter 必填 `name` 且必须与目录名一致，违反注册即报错（fail-closed）。
- **三级渐进披露**：L1 清单（name+description）注入 head 零成本 → L2 `load_skill` 按需加载正文 → L3 资源读取限 skill 目录内。
- **对所有 agent 可见**：L1 清单不按 agent 收窄——每份 skill 对包括 supervisor 在内的所有 agent 可见（含代码的 skill 其工具仍不并入 supervisor）。领域边界由 `description` 的触发语境承担（「谁该在什么时候加载它」写在描述里），不用私有字段做准入。
- **含代码的 skill 强制人工过目**：安装时 `-y` 被拒，须显式 `--allow-code`；skill 捆绑的 tools.py 并入子 agent 工具表，但 supervisor 代码级恒不并入（权限最小化红线）。
- 版本对齐经集中 lock 文件（`skills.lock.json`）：pin sha、update/enable-disable 走 `/skill` 斜杠命令，不手改。
- **`security/` 与 `infra/` 受保护**：WorkspacePolicy 按 `workspace/security`、`workspace/infra` 前缀硬拦读写（审计防篡改、服务卷不许 agent 碰）——工作区根换成 `.paperflow/` 后这两条保护自动跟着生效，无需另配。
- **意图知识库随仓库发布**：`intent/` 由 `paperflow/core/intent/rules/taxonomy.py` 装载并做 fail-closed 校验，路径锚仓库安装根（不随 `PAPERFLOW_RUNTIME_WORKSPACE` 重定向）。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 注册与安装逻辑：[`../paperflow/core/AGENTS.md`](../paperflow/core/AGENTS.md)（skills/registry.py、install.py）
- 意图知识库装载：[`../paperflow/core/intent/`](../paperflow/core/intent/)（rules/taxonomy.py）
