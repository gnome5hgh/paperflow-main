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
- **修法**：Docker Desktop 调 VM 内存 ≥4GB 后 `docker compose up -d` 重建；数据卷在 `data/infra/milvus/` 不用动。
- **验证**：`docker compose ps` 中 milvus 状态 Up 且 9091 健康检查通过，不再重启。

### `index_all` 卡住 / GROBID 相关报错（GROBID 60s 超时）
- **根因**：GROBID 解析单 PDF 超时（60s），批量索引被阻断；GROBID 不可用时回退 PyMuPDF（质量差，可能是后续检索不准的根）。
- **修法**：确认 GROBID 健康（`docker compose ps`、`curl localhost:8070`）；首次使用需按 docker-compose.yml 首部注释初始化 grobid-home 到 `data/infra/grobid/`。超时 PDF 单独重试，不阻塞整批。
- **验证**：`curl -s localhost:8070/api/isalive` 返回 alive；index_all 跑完无超时中断。

### 启动报「LLM API key 未配置」
- **根因**：`config.yaml` 的 `llm.api_key` 未配且无环境变量 `PAPERFLOW_LLM_API_KEY`；`config.yaml` gitignored，新环境常缺。
- **修法**：`cp config.example.yaml config.yaml` 后填 key；RAG 的 key 独立配 `rag.embedding.api_key` 或 `PAPERFLOW_RAG_EMBEDDING_API_KEY`。
- **验证**：启动不再报 key 错误，进入正常对话。

### 检索只有 BM25 结果 / 稠密路静默消失
- **根因**：`rag.embedding.api_key` 未配——设计为软降级（路由退纯 BM25、检索跳稠密路），只警告不报错。
- **修法**：配置 `rag.embedding.api_key`（或继承检查 `rag.rerank` 段）；重启会话生效。
- **验证**：启动日志无「跳过稠密」类警告；检索结果含向量召回来源。

### BM25 检索结果缺失或与重启前不一致（重启后漂移）
- **根因**：旧版本重启后 BM25 索引未恢复；已在 `paperflow/rag/services/retriever.py`（BM25 进程级裸 rebuild，自向量库重建）修复。命中此症状说明代码是旧版本。
- **修法**：更新到含裸 rebuild 的版本；临时绕过可触发一次任意查询（首查即重建）。
- **验证**：重启后首次查询结果与重启前一致；`retriever.py` 中存在 rebuild 恢复逻辑。

### 想跳过启动时的服务预检（PAPERFLOW_SKIP_BOOTSTRAP）
- **根因**：启动自动探测 Milvus/GROBID，服务未起时会拉起 docker（可能很慢或失败刷屏）。
- **修法**：`PAPERFLOW_SKIP_BOOTSTRAP=1` 跳过预检；服务失败本就只警告不阻塞（软依赖降级）。
- **验证**：设变量后启动秒进 REPL，无服务探测输出。
