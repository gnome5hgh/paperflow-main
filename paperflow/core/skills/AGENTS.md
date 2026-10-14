# paperflow/core/skills

## Scope

- 覆盖 `core/skills/`：Skill 插件体系的发现、校验、准入安装与装配期的工具并入。
- Skill 根目录与资产入库规则见 [`.paperflow/AGENTS.md`](../../../.paperflow/AGENTS.md)。
- 本层只放子包，不放裸模块。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：把「给现有 agent 注入领域知识 / 流程指令 / 轻量工具」的能力包做成可安装、可校验、可停用的一等公民。

## Directory Structure

```text
skills/
├─ config/      # SkillConfig：skill 自有参数（名字、描述、正文、元数据、工具、路径）
└─ services/    # registry.py（发现 + frontmatter 校验 + 渐进披露）
                #   + install.py（准入安装 / 更新 / 停用与 lock 治理）
                #   + assembly.py（工具并入工具表 + 命名空间唯一性）
```

## Responsibilities

- 扫描 `.paperflow/skills/` 并按规范校验每份 skill。
- 提供三级渐进披露：清单注入 head → 按需加载正文 → 读目录内资源。
- 承担安装准入（含第三方代码的过目闸门）与版本对齐。
- 把 skill 捆绑的工具并入子 agent 的工具表。

## Core Logic

- **三级渐进披露**：L1 清单（名字 + 描述）注入 head、零成本 → L2 按需加载正文 → L3 资源读取**限 skill 目录内**（路径围栏）。
- **skill 对全部 agent 可见**：清单不按 agent 收窄（含 supervisor）——规范没有 per-agent 可见性字段，领域边界由描述里的触发语境承担。
- **含代码的 skill 其工具不并入 supervisor**（代码级红线，权限最小化）；其余子 agent 正常并入。
- **版本对齐经集中 lock**：来源、版本、内容 hash、启用状态都记在 lock 文件里；安装 / 更新 / 启停走斜杠命令，**不要手改 lock**。

## Key Entry Points

- `services/registry.py` — 发现与校验（名字与目录名的一致性在这里把关）
- `services/install.py` — 准入安装与 lock 治理
- `services/assembly.py` — 工具并入
- `config/` — skill 的参数模型

## Usage And Change Notes

- **agentskills.io 规范强制**：frontmatter 必填 `name` 且必须与目录名一致，违反即注册报错（fail-closed）。放宽这一条会让「按名字加载」的所有下游一起失准。
- **含代码的安装强制人工过目**：`-y` 被拒，必须显式 `--allow-code`。这是第三方代码进入本进程的唯一闸门，不要为了方便放开。
- **本包不建 `constants/`**：候选项（lock 文件名、版本）只服务单一消费方，就地声明即可。
- 新增 skill 的规范细节（frontmatter 字段、目录结构）以仓库外的 agentskills.io 规范与现有 skill 为准，不在本文件另立一份。

## Dependencies And Collaboration

- 被 `core/agent`（head 注入清单、加载正文）与 `tools/skills/`（加载工具）消费。
- 被 `core/agent` 的装配层调用做工具并入；`tools/review/format_check` 经本包的路径出口解析 skill 资源。
- 与 `.paperflow/skills/`（资产目录）是「实现 ↔ 数据」的关系；与 `.agents/skills/`（给编码代理用）无关。

## Investigation Rule

1. 先确认 skill 是被发现了还是没被发现——注册期 fail-closed，问题通常出在名字与目录名不一致或 frontmatter 缺字段。
2. 发现与校验看 `services/registry.py`；安装与 lock 看 `services/install.py`；工具没生效看 `services/assembly.py`。
3. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core -k "skill" -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core -k "skill" -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)（core manifest）
- 设计依据：ADR 0011（Skill 系统）
- Skill 根目录与入库规则：[`../../../.paperflow/AGENTS.md`](../../../.paperflow/AGENTS.md)
- 工具封装：[`../../tools/AGENTS.md`](../../tools/AGENTS.md)

## References

- [`services/registry.py`](services/registry.py)
- [`services/install.py`](services/install.py)
- [`services/assembly.py`](services/assembly.py)
- [`../../../.paperflow/AGENTS.md`](../../../.paperflow/AGENTS.md)
