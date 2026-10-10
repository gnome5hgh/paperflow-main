# paperflow/core/skills/

## Scope

- 本文件覆盖 Skill 插件体系：`.paperflow/skills/<name>/` 的发现、校验、准入安装与装配期工具并入。
- 本层只放子包（外加本 manifest 与 `__init__.py`），不放裸模块。

## Directory Structure

```text
skills/
├─ domain/dto/     # skill_config.py：SkillConfig（插件配置实体：名字/描述/正文/metadata/工具/路径）
└─ services/       # registry.py(发现+frontmatter 校验+可见性+渐进披露) + install.py(准入安装/更新/停用与 lock 治理)
                   #   + assembly.py(工具并入 merge_tools + 命名空间唯一性)
```

## Core Rules

- **agentskills.io 规范强制**：frontmatter 必填 `name` 且必须与目录名一致，违反注册即报错（fail-closed）。
- **三级渐进披露**：L1 清单（name+description）注入 head 零成本 → L2 `load_skill` 按需加载正文 → L3 资源读取限 skill 目录内。
- **对所有 agent 可见**：清单不按 agent 收窄（含 supervisor）；含代码的 skill 其 `tools.py` 仍**不并入 supervisor**（权限最小化红线）。
- **含代码的安装强制人工过目**：安装时 `-y` 被拒，须显式 `--allow-code`。
- **版本对齐经集中 lock**：`skills.lock.json` 记来源/版本/内容 hash/enabled，update 与 enable-disable 走 `/skill` 斜杠命令，不手改。
- **不要 `constants/`**：本包候选项（lock 文件名/版本）只服务 `install.py` 单一消费方，就地声明即可。

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 设计依据：ADR 0011（Skill 系统）
- Skill 根目录与资产入库规则：[`../../../.paperflow/AGENTS.md`](../../../.paperflow/AGENTS.md)
