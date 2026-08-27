"""从用户输入提取实体。确定性正则，只提取不判定意图。

设计约束：
- pdf/note 路径要求绝对路径（工作区策略拒绝相对路径，任务文本里也约定用绝对路径）；
  相对路径不匹配是刻意行为
- 同一输入可同时包含多个实体：各实体互不排斥，全部提取
- ``figure`` 存数字字符串（"Figure 3" → "3"），交给上层重组进任务文本
"""
import re

# ----------------------------- 正则表达式定义 -----------------------------
# 注：每个正则的设计均考虑路径内空格、防止跨路径误匹配、拒绝相对路径。

# 绝对路径 PDF（/ 开头，.pdf 结尾）。分段式正则：
# - 每段由 / 分隔，段内 [^\s/]+(?:[ \t]+[^\s/]+)* 允许目录/文件名内部含空格
#   （若用 [^\s:] 之类排除空白，含空格路径就提取不出，结果缺 pdf_path 实体）
# - filename 段独立锚定 \.pdf 结尾，正则不会跨第二个路径吞并——
#   若用 [^\n]*? 匹配，会让 "根据 /a/doc.pdf 更新 /b/note.md" 的 note 匹配从 /a 起点吸收整段
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
# 仅扩展名锚定 \.md：同样允许段内空格、不跨第二个路径、(?<!\w) 守卫防相对路径误判。
# 独立成 regex 而非复用 PDF_PATH_RE——note 提取必须精确命中 .md 结尾，
# 若与 pdf 共用 (?:pdf|md) 会让 search 返回行内第一个 pdf 路径。
NOTE_PATH_RE = re.compile(
    r"(?<!\w)(/(?:[^\s/]+(?:[ \t]+[^\s/]+)*\/)*[^\s/]+(?:[ \t]+[^\s/]+)*\.md)\b",
    re.IGNORECASE,
)

#: Figure 引用（图/Figure/fig. + 数字，中英文）
FIGURE_RE = re.compile(r"(?:图|Figure|fig\.?)\s*(\d+)", re.IGNORECASE)


def extract_entities(query: str) -> dict:
    """从用户输入提取实体，返回 {"键": 值}；无实体返回空 dict。

    本函数使用确定性正则表达式从用户查询中提取各类实体，不依赖模型，仅做提取不做意图判定。
    支持提取的实体类型及对应的键：
        - "pdf_path": 绝对路径的 PDF 文件路径（以 .pdf 结尾）
        - "arxiv_id": arXiv 标识符（格式如 2401.12345 或带版本号）
        - "doi": DOI 标识符（格式如 10.xxxx/xxx）
        - "note_path": 绝对路径的 Markdown 笔记文件路径（以 .md 结尾）
        - "figure": 图号（数字字符串，如 "3"）

    Args:
        query: 用户输入的文本字符串。

    Returns:
        字典，键为上述实体类型名，值为提取出的字符串（DOI 会去掉尾部标点）。
        若某类实体未匹配，则字典中不包含该键。
    """
    entities: dict = {}

    # 提取 PDF 路径
    if m := PDF_PATH_RE.search(query):
        entities["pdf_path"] = m.group(1)
    # 提取 arXiv ID
    if m := ARXIV_RE.search(query):
        entities["arxiv_id"] = m.group(1)
    # 提取 DOI，并去除尾部标点（逗号、分号、句点）
    if m := DOI_RE.search(query):
        entities["doi"] = m.group(1).rstrip(".,;")
    # 提取 Markdown 笔记路径
    if m := NOTE_PATH_RE.search(query):
        entities["note_path"] = m.group(1)
    # 提取图号（只取数字）
    if m := FIGURE_RE.search(query):
        entities["figure"] = m.group(1)

    return entities
