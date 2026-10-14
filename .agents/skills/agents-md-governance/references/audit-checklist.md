# 体检清单

项目跑久了之后按这个顺序过一遍。前两步是机械的（脚本直接跑），后三步要人判断。**每次改动之后再跑一遍第 0 步**——它是防「一次只改一半」的兜底。

## 0. 机械校验（先跑这两个脚本）

**骨架一致性**：每份 manifest 的节名与顺序是否与模板一致。根文件与单元文件两套骨架。

```python
# python3 - <<'EOF'   （在仓库根执行）
import pathlib
ROOT = ["Scope", "模块定位", "目录结构", "技术结构", "环境与依赖", "仓库职责",
        "核心协作逻辑", "硬红线", "关键入口", "渐进式阅读路径", "调查规则",
        "常用命令", "统一编码规范", "文档维护规则", "Routing", "References"]
SUB = ["Scope", "Module Positioning", "Directory Structure", "Responsibilities",
       "Core Logic", "Key Entry Points", "Usage And Change Notes",
       "Dependencies And Collaboration", "Investigation Rule", "Core Commands",
       "Verification", "Routing", "References"]
root = pathlib.Path("AGENTS.md")
files = [f for f in pathlib.Path(".").rglob("AGENTS.md")
         if not any(s in str(f) for s in (".git/", "node_modules", ".venv", "worktrees"))]
bad = []
for f in files:
    hs = [l[3:].strip() for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("## ")]
    if hs != (ROOT if f == root else SUB):
        bad.append((str(f), [s for s in (ROOT if f == root else SUB) if s not in hs]))
print(f"{len(files)} 份，不一致 {len(bad)}")
for f, missing in bad:
    print("  ", f, "缺/错序:", missing)
EOF
```

> 若项目用自己的语言写节名，把两份列表换成你的版本，**别在两处维护两份不同的约定**。

**链接可解析**：manifest 里的相对链接是否都指向存在的文件。

```python
# python3 - <<'EOF'
import pathlib, re
bad, total = [], 0
for f in pathlib.Path(".").rglob("AGENTS.md"):
    if any(s in str(f) for s in (".git/", "node_modules", ".venv", "worktrees")):
        continue
    for m in re.finditer(r"\[`?([^\]]+)`?\]\(([^)]+)\)", f.read_text(encoding="utf-8")):
        t = m.group(2)
        if t.startswith(("http", "#", "mailto")):
            continue
        total += 1
        if not (f.parent / t.split("#")[0]).resolve().exists():
            bad.append(f"{f}: {t}")
print(f"{total} 条链接，失效 {len(bad)}")
for b in bad:
    print("  MISS", b)
EOF
```

## 1. 量体积

```bash
find . -name AGENTS.md -not -path "*/node_modules/*" | xargs wc -lc | sort -n
```

- 单份超过客户端上限的 **70%** 就该处理（默认上限 100 KB，见 `case-notes.md`）。
- 找出最大的那几节——膨胀几乎总是集中在某一个「模块百科 / 配置字段表」节里。
- 注意**截断是从尾部丢的**：无论总长如何，不可丢失的红线都该在文件前 1/3。

## 2. 扫快照（逐节问判据）

对着每一节问：「删掉它，agent 会不会做错事？」不会、但得去源码翻的，就是快照。按以下特征搜，命中即逐条判定：

```bash
grep -rn "当前有\|共有 [0-9]\|支持 [0-9] 种\|目前\|现有 [0-9]" --include=AGENTS.md .
grep -rn "^|" --include=AGENTS.md .        # 表格：多半是字段表/默认值表 → 真相源在代码
grep -rn "\.py(\|\.ts(\|\.java(" --include=AGENTS.md .   # 逐文件清单
```

重灾区（一律删，改成指针）：配置字段表与默认值、目录逐文件职责、枚举取值清单、角色/工具/端点清单、当前数量与统计数字。

**判别不是「删光表格」**：表格里如果有「改 X 要动 Y」这种才留——那是规则，只是恰好排成了表。

## 3. 查复述

同一件事写了几遍？三处常见复述：

- **父子之间**：父文件写了机制，子文件又写一遍（或反过来）。留最靠近使用点的那处，另一处改指针。
- **manifest 与设计文档之间**：设计文档里已有「为什么」，manifest 里再写一段设计叙事。
- **manifest 与 README 之间**：README 面向人，容易先写一步，然后 manifest 抄它、或反过来。

```bash
# 找一个可疑关键词出现的位置，人工比对是不是同一件事
grep -rn "<关键词>" --include=AGENTS.md --include=README.md .
```

这一刀通常比「搬去别处」更省事、风险更低，**先做它再做搬移**。

## 4. 对漂移

manifest 里的每一次点名都是一个承诺。逐类验证：

- **链接** → 第 0 步的脚本。
- **被点名的文件/目录/符号** → 逐个 `ls` / `grep` 一下还真不真。改名与搬迁是漂移的主要来源。
- **命令** → 抽查几条真的跑得起来（尤其是 `Verification` 那节，编造的命令比留空更有害）。
- **清单类** → 编号清单（ADR 编号、模块编号）最容易停在旧版本：新增了 0011、0012，而某处的清单还停在 0010。
- **「修正史」类文字** → 规则若是「正文恒为最新」，正文里就不该有「更新（日期）」这种痕迹；发现一处就折回正文，历史挪进修正记录。

判断标准：**改名、搬迁、退役留下的死引用要就地改正。改正不是历史，是错。**

## 5. 瘦身后的回归（最容易漏）

删掉/搬走内容之后，**把每一条删掉的规则过一遍「它有没有别的去处」**：

- 该记进设计文档的（为什么这么设计）→ 挪进去
- 该进更近的子 manifest 的 → 挪下去
- 该进 skill（按需加载的操作手册）的 → 挪进去
- 真没有去处的 → **留在原地**，别为了瘦身删掉真规则

这一步不做，瘦身就是信息丢失；做了，瘦身只是搬家。判定手法：把删除前后的内容都导出，逐条对照（`git diff` 一个文件、或把旧版存到临时文件），**逐条说得出「它现在在哪」**才算过。

## 体检报告（产出）

给用户一个短报告，不要贴全部细节：

```markdown
## AGENTS.md 体检

| 指标 | 结果 |
|---|---|
| manifest 份数 | N 份（新增 X / 删除 Y） |
| 最大一份 | <路径> N KB / 占上限 N% |
| 骨架一致性 | N/N |
| 链接失效 | N |
| 规则去向核对 | N 条删除项，全部有去处（或列出无处可去的 M 条） |

主要动作：<删了什么快照 / 合并了哪几处复述 / 改正了哪些漂移>
遗留：<无处安放的规则、需要用户裁决的边界>
```
