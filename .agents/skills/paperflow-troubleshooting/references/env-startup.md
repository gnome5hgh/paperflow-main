# env-startup — 服务栈 / conda / Python / 配置

按报错关键词 Ctrl-F 定位。条目按发现顺序追加，不重排。

### 「paperflow 起来就退出了」且无报错（REPL EOF）
- **根因**：REPL 经 `conda run` 启动，stdin 未转发 → `input()` 立即 EOF。
- **修法**：`conda activate paperflow` 后直接运行 `paperflow`（等价 `python -m paperflow`），不经 `conda run`。
- **验证**：REPL 提示符出现且可交互输入。

### `SyntaxError` / `compile()` 报错但代码看起来没问题（Python 3.13 代理字符）
- **根因**：用系统 `python3`（3.13）跑了项目代码；3.13 对含代理字符的 docstring 的 `compile()` 更严格，3.11（项目 env）可通过。
- **修法**：改用 `conda run -n paperflow python …`；脚本内的 shebang/调用链一并检查。
- **验证**：同一命令在 paperflow env 下通过；`conda run -n paperflow python -V` 输出 3.11.x。

### Milvus 容器反复重启 / crash loop（Standalone 崩溃循环）
- **根因**：Docker VM 内存不足（曾默认 1.9G，Milvus Standalone 崩溃循环）。
- **修法**：Docker Desktop 调 VM 内存 ≥4GB 后 `docker compose up -d` 重建；数据卷在 `.paperflow/infra/milvus/` 不用动。
- **验证**：`docker compose ps` 中 milvus 状态 Up 且 9091 健康检查通过，不再重启。

### `index_all` 被某一篇 PDF 中断
- **根因**：解析是进程内 PyMuPDF（无外部服务，也没有超时这回事），但索引器**没有单篇容错**——`index_all` 逐篇调 `index_document`，其中 `extract_pdf` 对损坏/加密/非 PDF 文件会抛异常并一路穿出去，整批就此停住。
- **修法**：`index_paths(paths=[...])` 逐条回报 indexed/skipped/empty/failed，**不因单篇失败中断**——批量入库用它而不是 `index_all`；排查是哪一篇时看它的逐条回报。
- **验证**：`index_paths` 跑完并列出那一篇的 failed 原因；`index_status` 显示其余篇目已入库。

### 某篇 PDF 读不出来 / 取不到标题
- **根因**：解析全在进程内（PyMuPDF），失败只有两类：①文件本身损坏、加密或根本不是 PDF（抛异常，工具层转成错误文本）；②文件正常但**标题判据不过**——元数据标题被判为垃圾、或首页版面候选全是页眉/水印/作者行。第二类**不报错，只是标题为空**（宁空勿错的设计），表现为该 PDF 不进语料索引、笔记头部拿不到引用标题。
- **修法**：①损坏/加密的手工确认后换文件；②标题为空的用 `extract_title` 显式取一次看返回值，仍为空就**让用户提供标题**（不要拿文件名顶上），要入库则走 `add_external` 手动提供字段。
- **验证**：`read_pdf` 秒级返回正文或明确错误；`extract_title` 给出标题或明确的「未能取到可靠标题」提示。

### 一个子 agent 承包多篇 PDF 阅读，撞穿子 agent 超时帽（早先版本误记为「并发拖垮依赖栈」）
- **根因**：**任务形状**。一次「读一下某目录下所有论文」实测：supervisor 把整批交给一个 qa-agent，它对该目录 3 篇 PDF 逐个 `read_pdf`。当时的读路径是 `ReadPdfTool → RAGService.parse_pdf_cached`，而那把缓存**解析全程持 `RAGService` 全局锁**，同一批并发发起的多个 `read_pdf` 到了这一层会被**串行化**——三篇解析分别约 60.1 / 61.0 / 13.7 秒，墙钟合计约 135 秒，再加一轮 LLM 与工具开销就超过 qa-agent 的 180 秒预算。
- **别误判成「并发解析压垮了依赖栈」**：那把锁使这些解析并没有真正并发，且 Milvus 同期重启与「读 PDF 的并发」之间没有因果证据（本条早先版本这么写过，已纠正）。
- **修法**：任务侧「同类 N 件 → N 路 spawn，一个对象一个子 agent」——每路各有独立预算，即便底层串行也各自等得起。读路径现已与索引栈解耦，不再受这把锁影响。
- **验证**：单个子 agent 的 `read_pdf` 不再连续跑多个不同 path；单篇精读子任务不再撞 180 秒帽。

### 启动报「LLM API key 未配置」
- **根因**：`config.yaml` 的 `llm.api_key` 未配且无环境变量 `PAPERFLOW_LLM_API_KEY`；`config.yaml` gitignored，新环境常缺。
- **修法**：`cp config.example.yaml config.yaml` 后填 key；RAG 的 key 独立配 `rag.embedding.api_key` 或 `PAPERFLOW_RAG_EMBEDDING_API_KEY`。
- **验证**：启动不再报 key 错误，进入正常对话。

### 检索只有 BM25 结果 / 稠密路静默消失
- **根因**：`rag.embedding.api_key` 未配——设计为软降级（路由退纯 BM25、检索跳稠密路），只警告不报错。
- **修法**：配置 `rag.embedding.api_key`（或继承检查 `rag.rerank` 段）；重启会话生效。
- **验证**：查询时日志无「RAG 查询编码失败，本次退化为纯 BM25 检索」告警；检索结果含向量召回来源。

### BM25 检索结果缺失或与重启前不一致（重启后漂移）
- **根因**：旧版本重启后 BM25 索引未恢复；已在 `paperflow/rag/services/retriever.py`（BM25 进程级裸 rebuild，自向量库重建）修复。命中此症状说明代码是旧版本。
- **修法**：更新到含裸 rebuild 的版本；临时绕过可触发一次任意查询（首查即重建）。
- **验证**：重启后首次查询结果与重启前一致；`retriever.py` 中存在 rebuild 恢复逻辑。

### 想跳过启动时的服务预检（PAPERFLOW_SKIP_BOOTSTRAP）
- **根因**：启动自动探测 Milvus，服务未起时会拉起 docker（可能很慢或失败刷屏）。
- **修法**：`PAPERFLOW_SKIP_BOOTSTRAP=1` 跳过预检；服务失败本就只警告不阻塞（软依赖降级）。
- **验证**：设变量后启动秒进 REPL，无服务探测输出。

### 改了 `config.yaml` 里的一个字段，程序行为完全没变（无报错、静默不生效）
- **根因**：2026-10-05 配置模块化（spec 2026-10-05-constants-and-config-reorg）后 `config.yaml` 是纯覆盖文件，加载器 `_merge`（`paperflow/config.py`）对 dataclass 树里不存在的键**静默忽略、不报错**；键名一次性迁移且无兼容层——按旧平铺键名（如 `milvus_uri`，`docs/SERVER.md` 仍这么写）或拼错、放错层级的字段改动等于没改。另注意：优先级 `PAPERFLOW_*` 环境变量 > YAML，且 config 仅启动时 `from_env()` 加载一次，改完必须重启进程。
- **修法**：键名/层级对照 `config.example.yaml` 与 `paperflow/config.py` 的 dataclass 字段路径改写（旧→新键映射见 spec §4）；重启进程；确认 shell/`.env` 无同名 `PAPERFLOW_*` 变量顶掉 YAML 值。
- **验证**：`conda run -n paperflow python -c "from paperflow.config import PaperFlowConfig; print(PaperFlowConfig.from_env().<改动字段的路径，如 rag.storage.uri>)"` 输出与 config.yaml 改后的值一致；不一致时按 `tests/core/test_config.py` 的 `_leaf_unknowns` 口径扫 config.yaml 未生效键（未知键列表应只为命中的旧键）。

### 重负载任务跑到一半 Milvus 崩溃循环（`streaming node is not alive` / `Slow etcd operation` / `session is expired`）
- **根因**：与上一条同源但触发条件不同——不是 VM 一开始就太小，而是**任务把 VM 内存吃穿**。Docker VM 约 3.9GB，其中 Milvus 栈（etcd + minio + standalone）并发检索/写入时能占 1GB+。实测一次「读一下某目录下所有论文」：Milvus 在 13 分钟内重启 9 次（此前累计 RestartCount 25），随后整个 OrbStack VM 也重启一次（表现为**所有**容器同时变成 `Up N seconds`，RestartCount 清零）。VM 内内存耗尽后 Milvus 的 datanode/mixcoord/streamingnode 拿不到 etcd 租约（日志依次出现 `etcdserver: request timed out, waiting for the applied index took too long`、`Slow etcd operation`、`streaming node is not alive`、`confirm the lease is expired`）→ standalone 进程退出，`restart: unless-stopped` 反复拉起。宿主同为内存紧张时（`sysctl vm.swapusage` 显示 swap 用量高、宿主 load average 远超核数）会加剧。
- **因果的成色**：重启时间窗与该次重负载窗重合、机制（重负载把 VM 内存吃穿）也说得通，但**未经受控实验验证**——别在别的场合把它当成已证结论照搬。
- **修法**：两条腿——① **削负载**：重活别堆在一轮里做完（多篇大 PDF 分轮、同类任务拆多路 spawn）；PDF 解析已是进程内的事，不再占 VM 资源，但**建索引**要调云端嵌入并写 Milvus，仍是重活。② **加资源**：调大 OrbStack/Docker VM 内存；宿主侧先释放内存（关掉大内存 IDE 等）比调 VM 更立竿见影。
- **验证**：重负载任务跑完后 `docker inspect milvus-standalone --format '{{.RestartCount}}'` 不再增长；`curl -s localhost:9091/healthz` 持续返回 OK；`docker ps` 各容器 uptime 连续不归零。

### Milvus 半死时单次 `rag_retrieve` 阻塞 25~90 秒（`Connection recovery failed` / `Fail connecting to server on localhost:19530` / `DEADLINE_EXCEEDED`）
- **根因**：Milvus 端口仍开着但服务已半死（或正在重启）时，检索不是立刻失败而是长时间阻塞——`paperflow/rag/storage/vector_store.py` 构造 `MilvusClient(uri=uri)` **未传 timeout**，全部 RPC 也无 timeout，于是走 pymilvus 默认值：`retry_on_rpc_failure`（`retry_times=75`、backoff 上限 3s）与 `connection_manager._recover → reconnect`（connect deadline 被规范化为 10s）。而一次 `Retriever.retrieve()` 要做**多次** Milvus RPC（首次 `all_documents()` 重建 BM25 + 每个改写 query 一次 `query` + 至多两次 `fetch_by_ids`），几个 10s 恢复等待累加即 25s/64s/90s 量级。仓库内没有超时、没有熔断（`RAGService.milvus_available()` 结果永久缓存且**无人调用**，是死代码）。
- **放大伤害**：子 agent 超时帽是预算制（qa-agent 默认 180s，见 `paperflow/config.py`），1~2 次阻塞检索就能吃穿整个预算 → 子 agent `timeout` → supervisor 重派仍超时 → 整轮十几分钟拿不到结果，最后只能转成 ask_user 请示。
- **附注**：Milvus 不可用时 `rag_retrieve` **不会**软降级成纯 BM25，而是返回固定文本「⚠️ 向量检索不可用（Milvus 异常）」（`paperflow/tools/rag/rag_retrieve.py`）；`Retriever` 里只有 embedding 与 reranker 两路有 try/except，向量路与 BM25 路的 `fetch_by_ids` 均无保护——BM25 索引虽在本地内存，其元数据仍依赖 Milvus，所以「退纯 BM25」在 Milvus 挂掉时物理上不可行。别把这条与「embedding 未配 key → 退纯 BM25」的软降级混淆。
- **修法（已实施）**：各 RPC 传 `rag.storage.timeout`（读路径 5s / 写路径 60s），并在 `rag_retrieve` 边界加检索侧熔断器（`paperflow/rag/services/breaker.py`，冷却 60s，跳闸期间直接返回降级文本）。旧版本上遇到时先升到含 `RetrievalBreaker` 的版本。
- **验证**：`docker stop milvus-standalone` 后调一次 `rag_retrieve`，应在数秒内返回降级文本，而不是阻塞数十秒；再调一次应瞬时返回（熔断短路）；Milvus 起来后过冷却窗口自动恢复，无需重启进程。
