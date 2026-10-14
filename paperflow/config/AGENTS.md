# paperflow/config

## Scope

- 覆盖 `paperflow/config/`：全局可调参数的声明与加载。
- 本目录只管**全局可调参数**；各子包组件自己的行为参数跟组件走（见下）。

## Module Positioning

- 角色：`config-package-manifest-dir`
- 职责：声明全部可调参数及其默认值，并按固定优先级把 `config.yaml` 与环境变量合并进来。

## Directory Structure

- `__init__.py` — 只做再导出（对外公共面只有 `paperflow.config` 一个）
- `schemas/sections.py` — 与 `config.yaml` 同构的 dataclass 树，**所有可调参数默认值的唯一声明点**
- `services/loader.py` — 三级加载（默认值 < YAML < env）、递归合并、env 按路径派生、路径绝对化

## Responsibilities

- 提供唯一一份参数契约，供所有消费模块读取。
- 保证「配置文件只写要改的」——未列出的字段回落代码默认值。
- 把相对路径在加载出口统一绝对化。

## Core Logic

- **`schemas/sections.py` 是默认值的唯一声明点**：新增可调参数先在这里加字段，消费模块直接读，**不要在模块内另设常量副本**。
- **加载优先级固定为 默认值 < `config.yaml` < 环境变量**。
- **只认标量覆写**：dict / list 只能整段写在 YAML 里，env 与 YAML 都不吃非标量对象。
- **环境变量名不另设映射表**，按配置路径派生：`PAPERFLOW_` + 路径大写、`_` 连接（`rag.storage.uri` → `PAPERFLOW_RAG_STORAGE_URI`）。自由 dict 型配置（各 agent 超时表、MCP server 声明）**没有 env 形态**。
- **路径字段在加载出口绝对化**：`workspace` 一类路径在 `from_env()` 末尾解析成绝对路径，其他模块不要再自行拼相对路径。

## Key Entry Points

- `schemas/sections.py` — 参数树与默认值
- `services/loader.py` 的 `PaperFlowConfig` — 加载入口（含 `from_env`）
- `services/loader.py` 的合并与 env 派生实现 — 改加载语义的落点

## Usage And Change Notes

- **两类 `config/` 的判据是「用户会不会在 `config.yaml` 里改」**：会 → 顶层 `paperflow/config/`；不会、只有代码调用点决定 → 跟组件走（各子包自己的 `config/`，调用点各给一套、不进 YAML）。
- **新增或改名参数要同步三处**：`schemas/sections.py` 的字段、`config.example.yaml` 的示例、以及根 manifest 里对应的说明（配置段的取舍、环境变量清单）。
- 新增映射型配置时别指望它能经 env 派生；要它可配就得留在 YAML。
- 不要为兼容旧键名留别名分支——这类分支没有消费方、只会让「唯一声明点」失效。

## Dependencies And Collaboration

- 被 `cli/`（装配）、`core/` 各子包、`tools/`、`terminal/` 读取；本包不依赖它们中的任何一个。
- 与 `config.yaml`（本地、不入库）和 `config.example.yaml`（入库模板）是一对：字段定义在代码，取值在本地。

## Investigation Rule

1. 先分清是「参数没生效」还是「默认值不对」：前者看加载优先级与 env 派生，后者看 `schemas/sections.py`。
2. 参数问题查 `schemas/sections.py`（唯一声明点）→ `services/loader.py`（合并与派生）→ 仓库根 `config.example.yaml`（示例）。
3. 排查「配置写了没反应」先确认该字段是标量（非标量的 YAML 覆写与 env 派生都不生效）。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/core -k "config" -q
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/core -k "config" -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 装配方：[`../cli/AGENTS.md`](../cli/AGENTS.md)
- 运行层的组件参数：[`../core/AGENTS.md`](../core/AGENTS.md)

## References

- [`schemas/sections.py`](schemas/sections.py)
- [`services/loader.py`](services/loader.py)
- [`../../config.example.yaml`](../../config.example.yaml)
