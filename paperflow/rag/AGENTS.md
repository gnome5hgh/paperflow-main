# paperflow/rag/

## Scope

- 本文件覆盖 `rag/` 检索增强栈；端到端链路细节与参数表见根 manifest 的 RAG 节。

## Directory Structure

```text
rag/
├─ services/       # 门面与编排：rag_service.py(RAGService 单例门面) + indexer.py(增量索引) + retriever.py(混合检索) + query_rewriter.py
├─ parsers/        # grobid_client.py(TEI XML 解析，不可达回退) + chunker.py(学术分块)
├─ encoders/       # bm25.py(jieba BM25，向量库文本的投影)
└─ storage/        # vector_store.py(Milvus：Standalone 走 gRPC，本地文件路径走 Lite)
```

## Core Rules

- **RAGService 是唯一门面**：indexer 与 retriever 是同一实例的两个视图，共享一把锁——增量写入对查询立即可见。外部只经 `get_rag_service()` 惰性单例访问，不直连内部组件。
- **配方哈希守恒**：`index_state.json` 带配方哈希（切块参数/嵌入模型/逻辑版本），指纹不符自动放弃旧状态全量重扫重嵌——改切块或嵌入参数无需手工清库。
- **文档级删旧建新**：重索引用 `doc_chunk_ids` 定点取旧块 id，不做全表扫描。
- **降级语义**：GROBID 不可达回退字体启发式分节（降级文档不纳入配方哈希）；embedding api_key 缺失时检索跳稠密路、索引明确报错。
- **评测产物不进本包**：实验脚本与黄金集在 `scripts/rag/`（gitignored），遵循 `goldens/`（题集）+ `results/`（存档）分目录。

## Key Entry Points

- `services/rag_service.py` — `get_rag_service(config=None)` 双重检查加锁单例
- `services/indexer.py` — 增量扫描 + 配方哈希 + 一致性恢复
- `services/retriever.py` — 混合检索（指令前缀编码 → BM25+向量 → RRF → 云端重排）
- `parsers/chunker.py` — 按节切 + 句界滑窗 + 残渣丢弃判据

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 消费方工具：[`../tools/AGENTS.md`](../tools/AGENTS.md)（rag_retrieve）
