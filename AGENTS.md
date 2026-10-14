# AGENTS.md

本文件为 AI 编码代理（ZCode / Claude Code / Codex 等）提供 `paperFlow` 仓库的进入方式、阅读路径与必须遵守的规则。

**本文件只写规则，不写现状快照**：字段表、文件清单、枚举取值、当前数量都以源码为准，抄一份到这里必然过期，而读到的旧快照比读不到更糟。判据是「删掉它，agent 会不会做错事」——不会、但得去源码翻的，就删掉。唯一保留的非规则是**导航**（目录地图与「去哪里找」的指针）。

## Scope

- 本文件是 `paperFlow` 根工作区的总 manifest，负责建立仓库边界、阅读路径、治理规则与公共约束。
- 作用范围覆盖整个仓库；进入更深层目录后，服从该目录更近的 `AGENTS.md`。
- **子 manifest 不会自动注入**：客户端只注入从当前目录向上找到的**第一份** manifest，不逐层合并。所以**改某个模块之前，按 Routing 主动读它那份**——那里面是必须遵守的约定，不是可读可不读的背景。
- 事实优先级：源码与测试 > 本 manifest 及各级子 manifest > `docs/` 设计文档。文档与代码冲突时以代码为准。

## 模块定位

- 名称：`paperFlow`
- 路径：`.`
- 角色：`repo-root-workspace-manifest`
- 当前定位：LLM 驱动的学术研究流程助手。单根 agent（supervisor）接收每一轮用户输入 → 可选的意图预处理 → ReAct 循环 → 拆解子任务派发子 agent → 聚合各子 agent 的结构化摘要后汇总回答。
- 绑定环境：Python 3.11 / conda env `paperflow`。

## 目录结构

```text
paperFlow-main/
├─ paperflow/          # 主包：cli/(装配) config/(参数) core/(运行层，内含 rag/ vision/)
│                      #   citations/ tools/ terminal/
├─ agents/             # Agent 插件：AGENT.md（契约）+ tools.py
├─ .paperflow/         # 运行时数据根 + 静态资产（skills/ intent/）
├─ .agents/skills/     # 给编码代理按需加载的 skill（与本仓运行时无关）
├─ docs/               # 设计文档：adr/ learning/ research/ superpowers/
├─ tests/              # 测试套件
├─ scripts/            # 实验与标定脚本
├─ config.yaml         # 本地配置（gitignored），模板见 config.example.yaml
├─ docker-compose.yml  # 依赖服务栈（Milvus Standalone + MinIO）
└─ AGENTS.md
```

### 当前目录分层含义

- `paperflow/` 是主包；`cli/` 是装配根，`config/` 是参数唯一声明点，`core/` 是运行层（`rag/` 与 `vision/` 已住进 `core/` 内）。
- `agents/` 是 Agent 插件目录，每个子目录一个角色；它的 `description` 会被自动收进运行时的可派发清单。
- `.paperflow/` 同时是运行时数据根与静态资产根：`skills/` 与 `intent/` 入库，其余都是运行期产物。
- `.agents/skills/` 与 `.paperflow/skills/` 是**两条不相干的线**：前者给编码代理用，后者是 paperFlow 运行时的能力包。
- `docs/`、`scripts/`、`tests/` 不入库是既定策略；运行期目录与个人路径不得入库。

## 技术结构

- 语言与运行时：Python ≥ 3.11；依赖声明在 `pyproject.toml`，开发依赖经 `pip install -e ".[dev]"`。
- 依赖服务栈：Milvus Standalone（向量库，gRPC 19530 / 健康检查 9091）+ MinIO（图表原图的对象存储），由仓库根 `docker-compose.yml` 一次拉起，数据落 `.paperflow/infra/`。
- 外部模型端点：对话 LLM、视觉模型、RAG 嵌入与精排都是 OpenAI 兼容的 HTTP 接口，端点、模型名与 key 经配置提供，不硬编码。
- 可调参数与默认值的唯一声明点是 `paperflow/config/schemas/sections.py`；`config.yaml` 是纯覆盖文件，只写要改的值。

## 环境与依赖

- 两类 API key 都必须由用户提供：LLM（`llm.api_key` / `PAPERFLOW_LLM_API_KEY`）、RAG 嵌入与精排（`rag.embedding.api_key` / `PAPERFLOW_RAG_EMBEDDING_API_KEY`）。LLM key 留空即启动 fail-fast；RAG key 留空不崩，但检索退纯 BM25、索引明确报错、图表视觉描述不可用。
- `corpus.pdf_dir` / `corpus.note_dir` 这类**个人路径没有默认值、必须显式配**。用户给不出就如实说，不要猜、不要填占位值、不要静默跳过。
- 依赖服务都是**软依赖**：不可达只警告降级、不阻塞启动。单测用 Milvus Lite 内嵌、不需要这套服务；反之 `scripts/rag/rag_eval/` 的评测要连 Standalone 与 MinIO。

## 仓库职责

- 承载一个可运行的多 agent 学术研究流程助手：意图预处理、ReAct 编排、领域子 agent、RAG 检索、引用管理、记忆系统、终端交互。
- 维护 agent 插件契约、工具的安全元数据与安全中间件链。
- 维护设计文档体系：按模块划分的 ADR（正文恒为最新设计）、学习材料、实施期 spec/plan。
- 承载编码代理的协作资产：各级 manifest、`.agents/skills/`、`.paperflow/skills/`。

## 核心协作逻辑

- **没有确定性 pipeline**：路由、工具选择、任务拆解全由 LLM 的 ReAct 循环驱动；工具只是喂给模型的 JSON Schema 定义。
- **编排归 supervisor**：它按各子 agent 的 `description` 选型，顺序与并行由它自主决定。契约里写的「一个对象一路」是提示层的编排期望，不是闸门。
- **意图只作信号**：意图识别是可选预处理层，产出一条类别信号注入根 agent 的 head，不参与任何决策（约束见下方硬红线）。
- **领域产物各自归口**：记忆、语料索引、引用库各由一位领域 agent 独占读写，其余角色需要时派发它。
- 会话跨轮靠 SQL 落盘 + `session_id` 一致；`docs/`、`scripts/`、`tests/` 不入库，**文档漂移不会被 git 顶出来**，改完要主动核对。

## 硬红线

以下是不分模块、任何地方都必须成立的不变式。违反它们通常不报错，只会让功能静默失效或让设计倒退。

- **错误一律降级为文本**：工具与中间件的错误转成 `ToolResult` 文本回传模型，由模型决定重试或调整。唯一向上抛的异常是 `MaxTurnsExceeded`。
- **截断必须续写，绝不交付残缺内容**：模型因长度被截断时，把半截内容与续写提示放回 in-context 继续循环，不能把那半截当最终回答返回。
- **安全只有策略轴**：进程内决定「要不要用户点头」，性质是贴张纸。**隔离轴（由 OS 或容器强制）本项目不做**——要加能执行 shell 的工具，必须先有进程外隔离。
- **只有一个确认决策点**：策略引擎合并全部升级因后统一弹一次确认；其余中间件只登记升级原因。硬拒只来自显式规则，不来自档位。
- **所有拦截走同一条降级通道**：拒绝、确认失败、安全阻断都表现为带 `decision` 的 `ToolResult`，绝不绕过 ReAct 循环。
- **写工具必须声明安全元数据**（`risk_level` / `side_effects` / `provenance` 等），注册表加载时校验合法值。
- **路径检查与写盘共用同一份解析结果**：检查通过的路径参数就地归一化为解析后的绝对路径（防 TOCTOU）。
- **SQL 是记忆真相源，markdown 是投影**：压缩与窗口驱逐**永不删 SQL 行**；读—改—写类块写入必须经一次持锁事务，不能「读旧值→算新值→写回」分三步做。
- **领域产物的读写各自归口**：记忆归 `memory-agent`、语料索引归 `rag-agent`、引用库归 `citation-agent`；这三类工具**其余 agent 一件不装**。supervisor 不持有任何执行类工具。
- **`allowed_spawns` 与风险档位由框架强制**，不靠提示词自律；批量同类对象**一个对象一路**派发。
- **意图只作信号**：不决定派发顺序、不作派发门禁、不持跨轮状态；spawn 对意图零引用，子 agent 不做意图识别。
- **同一会话的 `session_id` 三处必须一致**（CLI / AgentManager / `Agent.session_id`），错位会让各自读到空数据。
- **流式零开销**：`stream_callback` / `telemetry_callback` 为 `None` 时，全链路保持非流式行为。
- **常量与配置的落点**：一个枚举一个文件（`constants/enums/<name>.py`，`__init__` 只再导出）；常量**有第二个消费方才**进 `constants/`；可调参数进顶层 `paperflow/config/`，组件自己的行为参数跟组件走。
- **标题宁空勿错**：论文标题来自解析器出口，取不到就如实为空；**绝不回退到 PDF 文件名**。
- **已退役的实现不要复活**：意图的旧混合路由与澄清链路、路径白名单、沙箱轴都已整体移出主干。旧文档里的描述不作论据。

## 关键入口

- 仓库规则与阅读路径：`AGENTS.md`
- 装配根：`paperflow/cli/services/assembly.py`；启动预检：`paperflow/cli/services/bootstrap.py`
- 参数声明点：`paperflow/config/schemas/sections.py`
- 配置模板与字段示例：`config.example.yaml`
- Agent 插件契约：`agents/AGENTS.md` 与各 `agents/<name>/AGENT.md`
- 设计文档索引：`docs/adr/`（设计为什么这么做）与 `docs/learning/`（实现怎么运作）

## 渐进式阅读路径

- 仓库边界与公共规则：先读本文件。
- 改主包某模块：`paperflow/AGENTS.md` → 对应子包 `AGENTS.md` → 源码与 `tests/`。
- 改某个 agent 的契约：`agents/AGENTS.md` → `agents/<name>/AGENT.md` → `tools.py`。
- 改运行时能力包（skill）：`.paperflow/AGENTS.md` → `paperflow/core/skills/AGENTS.md`。
- 追设计背景：`docs/adr/`（按模块一份，正文恒为最新）→ 需要历史时 `docs/adr/修正记录.md`。
- 想知道某块实现**怎么运作**（而不是为什么这么设计）：`docs/learning/`（按主题一份，正文同样恒为最新）。
- 排查异常：先过 `.agents/skills/paperflow-troubleshooting/SKILL.md` 的已知根因表。

## 调查规则

1. 先读本文件建立边界，再按 Routing 读目标模块那份子 manifest，最后才读源码。
2. 设计背景查 `docs/adr/`；历史决策查 `docs/adr/修正记录.md`。
3. manifest 不够用时深入源码、`pyproject.toml` 与调用链核实。**代码事实优先于任何文档概括**。
4. 实验与标定事实查 `scripts/<模块>/` 下各实验目录的 `results/` 报告（`scripts/` 不入库，删掉不可恢复）；评测题集在 `goldens/`、产物在 `results/`。
5. 运行期目录（`.paperflow/` 下非入库子目录、`.venv/`、`__pycache__/`）与 gitignored 产物不作事实源，除非明确要求排查运行时现象。

## 常用命令

```bash
# 安装开发依赖（conda env 内）
conda run -n paperflow pip install -e ".[dev]"

# 全量测试
conda run -n paperflow python -m pytest tests/ -v

# 单个测试
conda run -n paperflow python -m pytest tests/agent/test_agent.py::TestExecTool -v

# 依赖服务栈（Milvus Standalone + MinIO）；paperflow 启动时会自动拉起，此命令用于手动管理
docker compose up -d

# 交互式 REPL（⚠️ 不能经 conda run：它不转发 stdin，REPL 会立即 EOF 退出）
conda activate paperflow && paperflow     # 等价：python -m paperflow（同一 env 内）
```

- 启动时自动探测依赖服务，未起则自动 `docker compose up -d` 并等待健康；失败只警告不阻塞。跳过预检：`PAPERFLOW_SKIP_BOOTSTRAP=1`。
- 非交互命令（测试 / 脚本 / 安装）**一律经 `conda run -n paperflow`**，不要用裸 `python` 或 `pip`。唯一例外就是上面的交互式 REPL。

## 统一编码规范

- 注释面向所有人——包括新人和非维护者。用中文写，不出现内部项目代号（任务/决策/评审编号、日期、spec/ADR 章节引用），也不写过程流水账；需要背景时用一句白话说明。
- 每个函数/方法的 docstring 至少说明其**作用**；关键算法思路、非显而易见的边界条件（安全/并发/失败处理）是重点。非显而易见的逻辑保留 WHY，但用通俗语言，不写成变更日志。
- **docstring 按 Google 风格写**（三个双引号；首行一句话摘要并以句号收尾，与正文之间空一行）：有参数写 `Args:` 并**逐个**列出，有返回值写 `Returns:`（只返回 `None` 可省），会抛异常写 `Raises:`，生成器用 `Yields:`。函数名与签名本身已说清、且 docstring 只有一句话时，这些段落可以**整段**省略——要么不写、要么列全，不挑「显而易见之外的」写。仓库无 linter 配置，此条靠人工遵守。
- 按逻辑块组织注释：同一逻辑块的多行代码合并成一条块注释，放在块首；避免每行一注。每条注释紧跟其描述的代码。

```python
# BAD:  "Loop over items and add to result"        ← 复述代码
# GOOD: "遍历所有 items 并去重,因为多源搜索结果可能包含同一篇论文的不同版本"
```

- 修改代码时同步更新注释——过期注释比没有注释更糟。
- **复用优先**：新增前先检索既有实现，禁止平行造轮子；扩展既有能力优先于新增本地实现。

## 文档维护规则

- **本文件与各级子 manifest 只写规则，不写容易失效的静态计数、一次性状态或临时结论**（详见文首判据）；需要现状就读源码或 ADR / learning。**新增或整理 manifest 时加载 [`agents-md-governance`](.agents/skills/agents-md-governance/SKILL.md)**——判据、根/单元骨架模板、体检清单与装载行为实测都在那份 skill 里。
- **改代码后先自问要不要同步设计文档**：接口签名、行为语义、结构或已记录决策发生变化都算。需要同步时**先让测试通过再改文档**——先让代码行为被测试锁住，再让文档描述现状。
- **ADR（`docs/adr/`）**：
  - 按模块划分，**一份 ADR = 一个模块**（清单见该目录，文件名即模块）。新决策扩展对应模块那份；确属新模块才新建，编号顺延。
  - **正文恒为最新设计**：设计变更直接改写正文对应小节，不在正文留「修正说明 / 修正（日期）」这类历史痕迹，也不写「原方案被废弃」。
  - **修正史统一追加进 `docs/adr/修正记录.md`**（日期 / 文档 / 变更摘要 / 来源）——ADR 与 learning 共用这一份。
  - 跨模块变更**按归属拆分**写进各模块 ADR，不设跨模块 ADR；每个 ADR 要显式声明它与相邻模块的分工边界。
  - **编号不回收、空缺须补齐**：删除或合并 ADR 后重编号，并同步更新所有活文档的引用。历史 spec/plan 里的旧编号不回改。
  - 文中互相引用只写「见 ADR XXXX」。
- **learning（`docs/learning/`）与 ADR 同一套规则**——它与 ADR 只差读者：ADR 讲「为什么这么设计」，learning 讲「实现怎么运作」，**同步义务完全一样**。
  - 按主题划分，**一份文件 = 一个主题**（清单见该目录，文件名即主题）。新主题才新建，编号顺延。
  - **正文恒为最新**：行为或结构变了直接改写正文对应小节，**不追加「更新（日期）」节、不在正文留修正痕迹**；历史上已追加的「更新」节要折回正文。
  - **修正史同样追加进 `docs/adr/修正记录.md`**（同一份文件，标注是 learning 的哪一篇）。不要另起一份 learning 修正记录。
  - **编号不回收、空缺须补齐**：删除或合并后重编号，并同步更新所有活文档的引用；历史文档里的旧编号不回改。
  - 文中引用只写指针（如「见 learning 04」）。
- spec（`docs/superpowers/specs/`）与 plan（`docs/superpowers/plans/`）是**实施期文档**：实现过程中发现它与最终代码不一致，或当前改动影响上层接口，都要回修。
- **`docs/`、`scripts/`、`tests/` 都不入库**，所以文档漂移不会被 diff 顶出来，也没有 CI 替你拦：改完要主动核对受影响的文档。

## Routing

- 当前目录是 `paperFlow` 根工作区 manifest。
- 主包入口：[`paperflow/AGENTS.md`](paperflow/AGENTS.md)（其下 [`cli/`](paperflow/cli/AGENTS.md) / [`config/`](paperflow/config/AGENTS.md) / [`core/`](paperflow/core/AGENTS.md) / [`tools/`](paperflow/tools/AGENTS.md) / [`citations/`](paperflow/citations/AGENTS.md) / [`terminal/`](paperflow/terminal/AGENTS.md)；`core/` 之下的十二份见 core manifest）
- 插件目录入口：[`agents/AGENTS.md`](agents/AGENTS.md)
- 资产与数据根入口：[`.paperflow/AGENTS.md`](.paperflow/AGENTS.md)（其下 [`skills/`](.paperflow/skills/AGENTS.md) / [`intent/`](.paperflow/intent/AGENTS.md)）
- 设计文档索引：[`docs/adr/`](docs/adr/)（修正史：[`docs/adr/修正记录.md`](docs/adr/修正记录.md)）、[`docs/learning/`](docs/learning/)
- 排障技能：[`.agents/skills/paperflow-troubleshooting/SKILL.md`](.agents/skills/paperflow-troubleshooting/SKILL.md)
- manifest 治理技能（搭建 / 增删模块 / 体检）：[`.agents/skills/agents-md-governance/SKILL.md`](.agents/skills/agents-md-governance/SKILL.md)

## References

- [`README.md`](README.md)
- [`config.example.yaml`](config.example.yaml)
- [`docker-compose.yml`](docker-compose.yml)
- [`pyproject.toml`](pyproject.toml)
- [`paperflow/AGENTS.md`](paperflow/AGENTS.md)
- [`agents/AGENTS.md`](agents/AGENTS.md)
- [`.paperflow/AGENTS.md`](.paperflow/AGENTS.md)
- [`docs/adr/`](docs/adr/)（按模块的设计决策；修正史：[`docs/adr/修正记录.md`](docs/adr/修正记录.md)）
- [`docs/learning/`](docs/learning/)（按主题的实现讲解，与 ADR 同一套同步规则）
- [`.agents/skills/paperflow-troubleshooting/SKILL.md`](.agents/skills/paperflow-troubleshooting/SKILL.md)
