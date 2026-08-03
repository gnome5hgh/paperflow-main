"""Stage 0 实体提取（ADR §管线）。确定性正则，只提取不判定意图。

设计约束：
- pdf/note 路径要求绝对路径（WorkspacePolicy 拒绝相对路径，LLM 任务文本里也是绝对路径）；
  相对路径不匹配是刻意行为（对齐既有 pipeline stub 断言）
- 同一输入多实体共存：各实体互不排斥，全部提取
- ``figure`` 存数字字符串（"Figure 3" → "3"），交给 Supervisor 重组进任务文本
"""
import re

#: 绝对路径 PDF（/ 开头，非空格非冒号，.pdf 结尾）
#: (?<!\w) 负向断言：要求前导 / 不被单词字符紧邻，否则相对路径
#: "paper/pdf/x.pdf" 中的内部 "/pdf" 也会被误判为绝对路径（与既有 stub 断言冲突）。
#: 说明：相对路径不匹配是设计意图（WorkspacePolicy 只认绝对路径）。
PDF_PATH_RE = re.compile(r"(?<!\w)(/[^\s:]*?\.pdf)\b", re.IGNORECASE)
#: arXiv ID（如 2401.12345 / 2401.12345v2）
ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b")
#: DOI（10.xxxx/xxx，去掉尾部标点）
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;]+)\b")
#: 绝对路径 Markdown 笔记（同 PDF_PATH_RE，绝对路径约束同样适用）
NOTE_PATH_RE = re.compile(r"(?<!\w)(/[^\s:]*?\.md)\b", re.IGNORECASE)
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
