"""Stage 0 实体提取（ADR §管线）。确定性正则，只提取不判定意图。

设计约束：
- pdf/note 路径要求绝对路径（WorkspacePolicy 拒绝相对路径，LLM 任务文本里也是绝对路径）；
  相对路径不匹配是刻意行为（对齐既有 pipeline stub 断言）
- 同一输入多实体共存：各实体互不排斥，全部提取
- ``figure`` 存数字字符串（"Figure 3" → "3"），交给 Supervisor 重组进任务文本
"""
import re

# 绝对路径 PDF（/ 开头，.pdf 结尾）。分段式正则：
# - 每段由 / 分隔，段内 [^\s/]+(?:[ \t]+[^\s/]+)* 允许目录/文件名内部含空格（RC2a，
#   修复前 [^\s:] 排除空白 → 含空格路径提取不出 → INTENT 块无 pdf_path 实体）
# - filename 段独立锚定 \.pdf 结尾，正则不会跨第二个路径吞并——修复 RC2c 共存回归：
#   初稿 [^\n]*? 使 "根据 /a/doc.pdf 更新 /b/note.md" 的 note 匹配从 /a 起点吸收整段
# - (?<!\w) 前导守卫：相对路径 "paper/pdf/x.pdf" 的内部 /pdf 前是单词字符，仍不误判
PDF_PATH_RE = re.compile(
    r"(?<!\w)(/(?:[^\s/]+(?:[ \t]+[^\s/]+)*\/)*[^\s/]+(?:[ \t]+[^\s/]+)*\.pdf)\b",
    re.IGNORECASE,
)
#: arXiv ID（如 2401.12345 / 2401.12345v2）
ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b")
#: DOI（10.xxxx/xxx，去掉尾部标点）
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;]+)\b")
# 绝对路径 Markdown 笔记（/ 开头，.md 结尾）。分段式正则，结构与 PDF_PATH_RE 同款，
# 仅扩展名锚定 \.md：同样允许段内空格（RC2a）、不跨第二个路径（RC2c 共存回归）、
# (?<!\w) 守卫防相对路径误判。独立成 regex 而非复用 PDF_PATH_RE——note 提取必须
# 精确命中 .md 结尾，若与 pdf 共用 (?:pdf|md) 会让 search 返回行内第一个 pdf 路径。
NOTE_PATH_RE = re.compile(
    r"(?<!\w)(/(?:[^\s/]+(?:[ \t]+[^\s/]+)*\/)*[^\s/]+(?:[ \t]+[^\s/]+)*\.md)\b",
    re.IGNORECASE,
)
#: Figure 引用（图/Figure/fig. + 数字，中英文）
FIGURE_RE = re.compile(r"(?:图|Figure|fig\.?)\s*(\d+)", re.IGNORECASE)


def extract_entities(query: str) -> dict:
    """从用户输入提取实体，返回 {"键": 值}；无实体返回空 dict。"""
    entities: dict = {}
    if m := PDF_PATH_RE.search(query):
        entities["pdf_path"] = m.group(1)
    if m := ARXIV_RE.search(query):
        entities["arxiv_id"] = m.group(1)
    if m := DOI_RE.search(query):
        entities["doi"] = m.group(1).rstrip(".,;")
    if m := NOTE_PATH_RE.search(query):
        entities["note_path"] = m.group(1)
    if m := FIGURE_RE.search(query):
        entities["figure"] = m.group(1)
    return entities
