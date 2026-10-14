---
name: paperflow-troubleshooting
description: 排查 paperFlow 项目问题：启动失败/报错、Milvus 服务异常、检索不到或不准、意图不生效或判错类别、测试失败。触发：用户说「排查」「报错了」「起不来」「为什么检索不到」「意图识别不对」「测试挂了」，或准备对本项目异常下结论之前。
---

# PaperFlow Troubleshooting

你是正在排查 paperFlow 问题的编码 agent。本 skill 提供两类东西：已知故障的症状速查表（references/），和本项目环境的铁律。先查表，查不到再走通用流程。

## 流程

1. 按下面的路由读对应 reference，用报错原文的关键词在文件里 Ctrl-F 定位条目。
2. 命中条目 → 按条目的「修法」执行，用「验证」命令确认恢复。
3. reference 文件缺失或未命中任何条目 → 按通用 `diagnosing-bugs` skill 的反馈闭环纪律排查。**排查收敛出新根因后，必须回写**：在对应 reference（不存在则新建）按下方「条目格式」追加一条，再收尾。这是维护协议，不可跳过。

## 路由

- 起不来 / 进程报错 / Docker 服务异常 / 导入错误 → `references/env-startup.md`
- 能跑但检索不到 / 检索不准 / 引用溯源可疑 / 索引行为异常 → `references/retrieval-quality.md`
- 意图不生效（head 里没有 INTENT 块）/ 意图判错类别 / 启用意图后启动即退出；旧称「路由行为异常」「拆分结果怪」「澄清不触发」（这三个概念已随混合路由退役）→ `references/intent-quality.md`
- 都不匹配 → 通用 `diagnosing-bugs` 流程

## 环境铁律（每次排查先过一遍）

1. 测试/脚本/安装一律 `conda run -n paperflow …`，绝不裸 `python`/`pip`：系统 python3 是 3.13，会被含代理字符的 docstring 炸 `compile()`；项目 env 是 3.11。
2. 交互式 REPL 例外：`conda run` 不转发 stdin，REPL 启动即 EOF。先 `conda activate paperflow`，再直接 `paperflow`。
3. 单测走 Milvus Lite（内嵌本地文件，`tests/conftest.py` 把 uri 指到临时 `.db`），不需要服务栈。**评测相反**：`scripts/rag/rag_eval/` 连 Standalone、媒体块原图还要 MinIO，跑之前先 `docker compose up -d`（Lite 没有对象存储，起不来）。
4. 全量套件当前全绿，整跑即可。历史上曾有「tests/rag 与其他套件合跑原生 Abort、须分套件跑」的环境问题，2026-10-14 实测已不复现（`1612 passed`，约 95s）；看到红先按真实失败查，别假定它是已知噪音。
5. `config.yaml` 被 gitignore。缺失或字段异常时先对照仓库根 `config.example.yaml`，别假设代码坏了。

## 条目格式（回写时遵守）

### <症状一句话，含报错关键词原文>
- **根因**：<一句话>
- **修法**：<具体命令/改动>
- **验证**：<跑什么命令、看什么输出算过>

条目按发现顺序追加，不重排旧条目（保持关键词位置稳定）。修法过时的条目标注 `（已过时 <日期>）`，不删除。同一症状存在多个根因时，拆成多条并列条目，不合并。**名称、路径、函数名写错的就地改正**——那不是历史，是错（如已改名的角色、已搬迁的模块），改正的判据是源码里还有没有这个符号。整个组件退役的条目（如已删的意图阈值体系）在标题上标注已过时并指向现状那一版，正文留着供关键词命中。

**这份表的价值全在「与代码一致」**：任何时候按它排查，一旦发现条目对不上现状，当场改正再收尾，别把「以后再修」留在表里。
