# paperflow/rag/

## Scope

- 本文件覆盖 `rag/` 检索增强栈；端到端链路细节与参数表见根 manifest 的 RAG 节。

## Directory Structure

```text
rag/
├─ domain/         # 领域模型（各层公共词汇）：entity/(Chunk 检索块 + Section 章节 + 块文本派生函数)
│                  #   + dto/(PdfText/Block 解析产物、IndexOutcome/IndexRunOutcome/IndexStatus 索引结果、RewriteResult 改写结果)
├─ constants/      # 跨文件共享常量：CHUNK_ID_LEN(块 id 前缀长度) + CHUNK_TYPE_(TEXT|TABLE|FIGURE)
├─ services/       # 门面与编排：rag_service.py(RAGService 单例门面) + indexer.py(增量索引) + retriever.py(混合检索) + query_rewriter.py
├─ parsers/        # pdf_extract.py(PDF → markdown + 版面坐标 + 标题) + chunker.py(分块算法本体；Chunk/Section 已移入 domain/)
│                  #   + table.py(表格区域文字按版面几何重建成 markdown，判据保守、拼不出可信行列就退回纯文字)
├─ encoders/       # bm25.py(jieba BM25，向量库文本的投影) + embedder.py(云端稠密编码) + reranker.py(云端交叉精排)
└─ storage/        # vector_store.py(Milvus：Standalone 走 gRPC，本地文件路径走 Lite)
                   #   + image_store.py(图表原图的对象存储：接口 + MinIO 实现 + 空实现，全软依赖)
```

## Core Rules

- **领域模型收 `domain/`**：跨层共享的数据模型（实体与层间载体）是各层的公共词汇，只依赖 `constants/` 的取值、不依赖服务实现——消费方一律 `from paperflow.rag.domain import ...`，模型不散落在产出它的实现文件里。分 `entity/`（业务里的「东西」）与 `dto/`（层间搬运的数据）两个子包，一模型一文件。
- **RAGService 是唯一门面**：indexer 与 retriever 是同一实例的两个视图，共享一把锁——增量写入对查询立即可见。外部只经 `get_rag_service()` 惰性单例访问，不直连内部组件。
- **解析来源单一**：PDF 与笔记都走「抽取出文本 → 按 `#` 行分节」，PDF 的文本与章节由 `parsers/pdf_extract.py` 本地版面还原，不存在解析器降级。
- **跨文件常量收 `constants/`**：`CHUNK_ID_LEN`（chunker 生成块 id、indexer 生成媒体块 id，同一规则）与 `CHUNK_TYPE_*`（chunker 打默认值、indexer 打图/表类型、vector_store 反序列化补默认值，同一套词）都是跨越消费方的结构契约，改一边就会让两边对不上——故有唯一声明点。正则、提示词、退避基数、容量阈值这类只服务单一消费方的结构常量留在消费处就地声明。
- **配方哈希守恒**：`index_state.json` 带配方哈希（切块参数/嵌入模型/逻辑版本），指纹不符自动放弃旧状态全量重扫重嵌——改切块或嵌入参数无需手工清库。
- **文档级删旧建新**：重索引用 `doc_chunk_ids` 定点取旧块 id，不做全表扫描；存储键与块 id 都是绝对路径。
- **集合结构变更自愈**：`VectorStore` 启动时比对所需字段，缺字段即删集合重建并提示需全量重建（老集合上写新字段会直接报错）。
- **媒体块的文本契约**：表块正文是重建出的 markdown 表格，**图块正文恒为空**（内容在图注里，产块判据也是「有图注才产」）——契约写在 `Chunk.text` 的注释里，改它等于改检索质量。
- **图表原图是软依赖**：存进依赖栈里 MinIO 的自有 bucket，块上只留对象键；存储不可达只告警、不拖累文本索引，且失败进冷却（避免按图重复等连接超时）。开关 `rag.store_images` 关掉即退回「只有文字」。
- **embedding api_key 缺失时检索跳稠密路、索引明确报错。**
- **评测产物不进本包**：实验脚本与黄金集在 `scripts/rag/`（gitignored），遵循 `goldens/`（题集）+ `results/`（存档）分目录。

## Key Entry Points

- `services/rag_service.py` — `get_rag_service(config=None)` 双重检查加锁单例
- `services/indexer.py` — 增量扫描 + 配方哈希 + 一致性恢复
- `services/retriever.py` — 混合检索（指令前缀编码 → BM25+向量 → RRF → 云端重排）
- `parsers/chunker.py` — 按节切 + 句界滑窗 + 残渣丢弃判据
- `parsers/pdf_extract.py` — PDF → markdown 渲染块（带页码与包围盒）

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 消费方工具：[`../tools/AGENTS.md`](../tools/AGENTS.md)（rag_retrieve）
