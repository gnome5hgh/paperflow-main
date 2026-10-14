# 单元 manifest 模板

每个「有自成体系的约定」的目录一份。13 节顺序固定；填不出内容的节保留标题写「无」，**不要删节**（骨架统一才能机械校验）。

`角色` 从下表取，别自创同义词：

```text
repo-root-workspace-manifest            工作区总 manifest（多仓 / 多包根）
repo-root-manifest                      单仓根 manifest
aggregator-manifest-dir                 只聚合子包、自身不放代码
package-root-manifest                   包根（有代码，但它是这一层的入口）
capability-or-domain-leaf-manifest-dir  能力 / 领域叶子目录（最常用）
shared-package-manifest-dir             被多处复用的共享包
app-manifest-dir / runnable-app-leaf-manifest-dir   可运行应用
bom-or-contract-leaf-manifest-dir       只有契约或清单、没有运行时代码
config-package-manifest-dir             配置模块
asset-root-manifest-dir                 资产根（随仓库分发的模板 / 知识库）
plugin-root-manifest                    插件目录
```

## 模板

```markdown
# <目录路径，如 src/core/rag>

## Scope

- 覆盖 `<目录路径>`：<它是什么>。
- <可选：不重复上级的哪部分——「根 manifest 的硬红线在本层同样成立，这里不重复」>
- <可选：与相邻单元的边界——「工具侧封装见 X，本文件只写实现侧」>

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：<一句话：它负责产出什么 / 承担什么>。

## Directory Structure

- `<子目录或关键文件>` — <一行用途>
- `<子目录>` — <一行用途>

## Responsibilities

- <它负责什么（边界内的事，3–6 条）>
- <它不负责什么（边界外的事，写出来比写职责更能防止越界）>

## Core Logic

- **<机制名>**：<怎么运作 + 为什么这么设计，一句话一条>。
- **<不变式>**：<什么必须永远成立；违反了会静默失效的那种>。
- <数据流：输入→处理→输出，只在链路不直观时写>

## Key Entry Points

- `<文件>` — <从哪个函数/类入手>
- `<文件>` — <入口>

## Usage And Change Notes

- **<改动 X> 要同时改 <Y、Z>**：<漏改的后果>。
- <新增 <东西> 时的固定步骤（几步就写几步）>。
- <易错的边界：某个参数、某个顺序、某个必须成对出现的调用>。

## Dependencies And Collaboration

- 依赖：<依赖谁，以及依赖的性质（编译期/运行期/只能经门面）>。
- 被依赖：<谁依赖它，以及它们期望什么>。
- 禁止方向：<明确不许反向依赖谁——这条最值钱>。

## Investigation Rule

1. <出问题先看哪个文件/哪一类问题落在哪>。
2. <排查顺序：从入口到实现，或从错误到源头>。
3. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
<这个模块的构建/测试/运行命令；没有就写「无（随根 manifest 的全量测试）」>
```

## Verification

```bash
<证明这次改动正确的命令（通常是模块级测试子集）>
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 同级 / 相关：[`<兄弟目录>/AGENTS.md`](<兄弟目录>/AGENTS.md)
- 设计依据：<设计文档链接>

## References

- [`<关键文件>`](<关键文件>)
- [`<关键文件>`](<关键文件>)
```

## 填每一节的判据

| 节 | 装什么 | 反例（别这么写） |
|---|---|---|
| Scope | 覆盖范围与边界声明 | 罗列本目录下所有文件名 |
| Module Positioning | 一句话职责 + 固定角色词 | 一段架构史 |
| Directory Structure | 子目录/关键文件 + 一行用途 | 每个文件写它有哪些函数 |
| Responsibilities | 边界内的职责 + 边界外的排除 | 复述架构图 |
| Core Logic | 机制与**不变式**（违反即 bug 的那些） | 教程式讲解 |
| Key Entry Points | 3–6 个入手点 | 完整 API 列表 |
| Usage And Change Notes | **改动注意**：改 A 要动 B、新增字段同步哪几处 | 空着；或写成「注意保持代码质量」 |
| Dependencies And Collaboration | 依赖方向 + 禁止方向 | 只写「被很多人使用」 |
| Investigation Rule | 可执行的阅读顺序 | 「请仔细阅读源码」 |
| Core Commands / Verification | 真实可跑的命令 | 编一条没跑过的命令 |
| Routing / References | 链接，且要能解析 | 写相对路径但不校验 |

## 一个填满的例子（虚构，看密度）

```markdown
# src/core/parser

## Scope

- 覆盖 `src/core/parser/`：把原始文档转成结构化块。
- 根 manifest 的硬红线在本层同样成立，这里不重复。

## Module Positioning

- 角色：`capability-or-domain-leaf-manifest-dir`
- 职责：把任意来源的文档解析成统一的块序列，并保留可用于定位的坐标信息。

## Directory Structure

- `formats/` — 各来源格式的解析器，一个格式一个文件
- `chunker.py` — 分块策略与边界处理
- `service.py` — 对外门面（单例）

## Responsibilities

- 解析来源文档，输出带位置信息的块。
- 识别并丢弃样板段落（页眉、参考文献、致谢）。
- 不负责：存储、检索、以及「哪篇文档该进库」的判断（那些归上层）。

## Core Logic

- **解析器不含降级分支**：解析失败即上报，不静默换一条更差的路径——两套解析结果混在一起没法排查。
- **块 id 由内容与位置派生**：同一份文档重复解析必须得到同一批 id，否则增量索引会重复写入。
- **块里只存正文**：章节前缀在使用点现拼，避免同一段内容出现两个版本。

## Key Entry Points

- `service.py` — 门面与单例
- `chunker.py` — 分块算法
- `formats/` — 具体格式解析器

## Usage And Change Notes

- **改分块参数要同时改缓存指纹**：否则新旧块会在同一个库里混合，且不会报错。
- **新增格式要在 `formats/__init__` 的注册表里登记**，否则它永远不会被调用。
- 给块加字段时，存储侧的读取要同步补默认值，否则旧数据读出为空。

## Dependencies And Collaboration

- 依赖：`core/config`（参数）、`core/common`（共享叶子）。
- 被依赖：`core/index`、`core/retrieve`。
- 禁止方向：本层不得 import 存储与检索——那会让解析耦合到运行环境。

## Investigation Rule

1. 「解析结果不对」看 `formats/` 对应格式；「块切得不合理」看 `chunker.py`。
2. 先确认输入文本本身是否正确（解析问题常常是上游取文本的问题）。
3. 文档与代码冲突时以代码事实为准。

## Core Commands

```bash
pytest tests/parser -q
```

## Verification

```bash
pytest tests/parser -q
```

## Routing

- 上级：[`../AGENTS.md`](../AGENTS.md)
- 消费方：[`../index/AGENTS.md`](../index/AGENTS.md)

## References

- [`service.py`](service.py)
- [`chunker.py`](chunker.py)
```
