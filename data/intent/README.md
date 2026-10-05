# data/intent —— 意图模块的生产资产（随仓库发布）

本目录只放**运行时需要**的意图知识资产（其余 `data/*` 均 gitignored，本目录是反选入库的例外）：

| 文件 | 说明 |
|---|---|
| `routes.yaml` | 路由知识库：12 条路由 × 例句 + 每条路由的 `score_threshold`（阈值由标定实验写回，2026-10-05 起非零） |
| `routes_vectors.npz` | 路由语料的稠密向量缓存（启动零网络；按模型名分键，当前为 Qwen3-Embedding-8B，4096 维） |

**题集（评测集）不在这里**——2026-10-05 起按实验归属移出：

| 题集 | 位置 | 谁用 |
|---|---|---|
| 意图标定的原始/审计终版题集 | `scripts/intent/calibration/goldens/{source,audited}/` | `scripts/intent/calibration/` |
| 将来的可用性指标题集 | `scripts/intent/eval/goldens/` | `scripts/intent/eval/`（计划中） |

理由：题集是实验资产（随实验版本演进、可被替换/重建），`routes.yaml` 是生产知识库
（随代码发布、被 `paperflow/core/intent/routing/route_loader.load_routes` 直接加载）。
`load_eval` 因此不再设默认路径——调用方显式传入题集路径。

改动 `routes.yaml` 的两种方式：
- 标定阈值：`scripts/intent/calibration/apply_calibration.py`（行级替换，保留注释）；
- 增删例句/新建路由：手改本文件，然后**必须重跑标定**（分数、阈值、判据三层都依赖语料，
  `results/cache/` 的矩阵缓存按语料指纹失效重算；路由向量缓存按语料指纹失效重算）。
