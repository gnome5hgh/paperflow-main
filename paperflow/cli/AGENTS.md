# paperflow/cli

## Scope

- 覆盖 `paperflow/cli/`：进程入口的两件事——启动预检与对象图装配。
- 终端交互逻辑不在本包（在 `paperflow/terminal/`）。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：把配置、安全中间件链、记忆服务、意图层、Agent 与 REPL 组装成一个可运行进程，并在装配前把依赖服务拉起来。

## Directory Structure

- `__init__.py` — 只做再导出（`main`）+ 一处环境守卫（包根只留入口与 manifest）
- `services/` — 启动期两件事：`bootstrap.py`（依赖服务预检：探测端口 → 必要时 `docker compose up -d` → 等健康）
  + `assembly.py`（对象图装配组合根 + 命令行参数解析）

## Responsibilities

- 启动前探测依赖服务并在缺失时自动拉起，失败只降级不阻塞。
- 构造对象图：配置 → 安全中间件链 → 记忆服务 → 意图层 → Agent → REPL。
- 解析命令行参数（`--resume`、`--skip-bootstrap`）。

## Core Logic

- **装配顺序即依赖方向**：顺序写在装配根函数（`services/assembly.py` 的 `main()`）的 docstring 里，改顺序等于改语义。
- **预检是软依赖**：任何失败只产警告、不阻塞启动；`PAPERFLOW_SKIP_BOOTSTRAP=1` 或非 TTY 直接跳过。**别把「预检失败」写成启动失败**。
- **包 `__init__` 里有一处必须在任何 grpc 导入之前执行的环境守卫**（`GRPC_VERBOSITY`）：gRPC C 核心在初始化时读它，晚一步就会在 MCP 子进程 fork 时刷出成片的 poll-list 噪音。用 `setdefault`，不覆盖用户显式配置。

## Key Entry Points

- `__init__.py` — 再导出的 `main`（进程入口）
- `services/assembly.py` 的 `main()` — 装配组合根与参数解析
- `services/bootstrap.py` — 预检实现

## Usage And Change Notes

- **装配模块叫 `services/assembly.py`**，不是 `main.py`：后者会与再导出的 `main` 函数同名，挡住 pytest 对 monkeypatch 目标的逐段解析。
- **改本目录（或 `config/`）的内部模块名，要同步 `tests/` 里的 monkeypatch 字符串目标**——主包里最容易踩的一处，判定细节见 [`../AGENTS.md`](../AGENTS.md) 的调查规则。
- 新增中间件、服务或工具组时，改动落在装配顺序里——安全侧的装配顺序还要同步 ADR 0002 的描述。
- 预检的超时、轮询间隔与 compose 命令都是本包的实现细节，改它们要同时考虑首次启动拉镜像的耗时。

## Dependencies And Collaboration

- 依赖 `config/`（参数）、`core/`（运行层与服务）、`citations/`、`tools/`（工具装配）、`terminal/`（REPL）。
- 被 `paperflow/__main__.py` 调用（`python -m paperflow` 的结构性入口）。
- 是被依赖方的**调用者**，不被任何下层反向依赖——这是「core 不 import 上层」那条规则的来源一侧。

## Investigation Rule

1. 启动或装配异常先看 `services/bootstrap.py`（预检）与 `services/assembly.py`（对象图），再往 `core/` 里找。
2. 参数问题看 `services/assembly.py` 的解析段与 `config/`。
3. 排查「某个能力没生效」先确认它在装配顺序里有没有被接上（例如回调、注册表、工具组）。
4. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
conda run -n paperflow python -m pytest tests/cli -q
conda activate paperflow && paperflow   # 交互式启动（不能经 conda run）
```

## Verification

```bash
conda run -n paperflow python -m pytest tests/cli -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 参数声明：[`../config/AGENTS.md`](../config/AGENTS.md)
- 运行层：[`../core/AGENTS.md`](../core/AGENTS.md)、终端：[`../terminal/AGENTS.md`](../terminal/AGENTS.md)

## References

- [`services/assembly.py`](services/assembly.py)
- [`services/bootstrap.py`](services/bootstrap.py)
- [`__init__.py`](__init__.py)
- [`../config/AGENTS.md`](../config/AGENTS.md)
