# .paperflow/

## Scope

- 本文件覆盖 `.paperflow/` 用户级数据目录；`skills/` 是 Skill 插件的唯一根目录（内置与用户安装同层，单级结构）。

## Directory Structure

```text
.paperflow/
└─ skills/
   └─ <skill-name>/   # 一个 skill 一个目录，目录名 = frontmatter 的 name
      ├─ SKILL.md     # agentskills.io 格式：frontmatter(name/description/…) + 正文
      ├─ tools.py     # 可选：module-level TOOLS，经 merge_tools 并入子 agent 工具表
      └─ references/  # 可选：L3 资源（load_skill(resource=...) 读取，路径围栏限本目录内）
```

## Core Rules

- **agentskills.io 规范强制**：frontmatter 必填 `name` 且必须与目录名一致，违反注册即报错（fail-closed）。
- **三级渐进披露**：L1 清单（name+description）注入 head 零成本 → L2 `load_skill` 按需加载正文 → L3 资源读取限 skill 目录内。
- **对所有 agent 可见**：L1 清单不按 agent 收窄——每份 skill 对包括 supervisor 在内的所有 agent 可见（含代码的 skill 其工具仍不并入 supervisor）。领域边界由 `description` 的触发语境承担（「谁该在什么时候加载它」写在描述里），不用私有字段做准入。
- **含代码的 skill 强制人工过目**：安装时 `-y` 被拒，须显式 `--allow-code`；skill 捆绑的 tools.py 并入子 agent 工具表，但 supervisor 代码级恒不并入（权限最小化红线）。
- 版本对齐经集中 lock 文件（`skills.lock.json`）：pin sha、update/enable-disable 走 `/skill` 斜杠命令，不手改。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 注册与安装逻辑：[`../paperflow/core/AGENTS.md`](../paperflow/core/AGENTS.md)（skills/registry.py、install.py）
