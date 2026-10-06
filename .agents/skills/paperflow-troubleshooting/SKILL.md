---
name: paperflow-troubleshooting
description: 排查 paperFlow 项目问题：启动失败/报错、Milvus/GROBID 服务异常、检索不到或不准、意图识别错类、澄清不触发、测试红。触发：用户说「排查」「报错了」「起不来」「为什么检索不到」「意图识别不对」「测试挂了」，或准备对本项目异常下结论之前。
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
- 意图错类 / 路由行为异常 / 澄清不触发 / 拆分结果怪 → `references/intent-quality.md`
- 都不匹配 → 通用 `diagnosing-bugs` 流程

## 环境铁律（每次排查先过一遍）

1. 测试/脚本/安装一律 `conda run -n paperflow …`，绝不裸 `python`/`pip`：系统 python3 是 3.13，会被含代理字符的 docstring 炸 `compile()`；项目 env 是 3.11。
2. 交互式 REPL 例外：`conda run` 不转发 stdin，REPL 启动即 EOF。先 `conda activate paperflow`，再直接 `paperflow`。
3. 单测与评测走 Milvus Lite（内嵌本地文件），不连 Standalone；只有排查服务栈本身时才 `docker compose up -d`。
4. 全量套件存在两个环境所致的红：tests/rag 与其他套件合跑会原生 Abort，须单独跑。判定回归前先分套件重跑确认。
5. `config.yaml` 被 gitignore。缺失或字段异常时先对照仓库根 `config.example.yaml`，别假设代码坏了。

## 条目格式（回写时遵守）

### <症状一句话，含报错关键词原文>
- **根因**：<一句话>
- **修法**：<具体命令/改动>
- **验证**：<跑什么命令、看什么输出算过>

条目按发现顺序追加，不重排旧条目（保持关键词位置稳定）。修法过时的条目标注 `（已过时 <日期>）`，不删除。同一症状存在多个根因时，拆成多条并列条目，不合并。
