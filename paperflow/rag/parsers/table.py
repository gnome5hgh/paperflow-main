"""表格结构重建：把表格区域内的词按版面几何拼回 markdown 表格。

从 PDF 文本层抽出来的表格只是一堆带坐标的词，行列关系只存在于坐标里。检索块需要
可读的结构（「哪一行的数值是多少」不能靠一串拍平的词回答），所以这里按 y 聚类分行、
按 x 间距切分单元格，再拼成 markdown。

**判据刻意保守，宁可降级也不造假结构**：表格被切错行列会给出看起来对、实际错的内容，
比没有结构更糟。因此只在「多数行共享同一个列数」且至少两行两列时才认可重建；否则返回
None，由调用方退回纯文字（各词按阅读顺序空格连接）——那时不声称自己有结构。

**局限（已知且接受）**：跨页表格、合并单元格、多级表头一律做不好，判据会让它们退到
纯文字路径。精度要求高的场景请取该块的原图交给视觉模型看，而不是指望这份重建。
"""
from __future__ import annotations

#: 行聚类容差系数：词的 y 中心相差不超过「中位词高的这个倍数」即视为同一行。
#: 取偏小值——把两行并成一行，比把一行拆成两行更难被后面的列数判据发现。
_ROW_TOL_RATIO = 0.6

#: 单元格切分阈值系数：相邻两词的横向间距超过「中位词高的这个倍数」即认为跨了列。
#: 词内与单元格内的空格远小于这个值，而列与列之间的空白通常大于它。
_COL_GAP_RATIO = 1.0

#: 单元格切分阈值的绝对下限（pt）：字号很小时，避免把正常词距误判成列间距。
_COL_GAP_MIN = 4.0

#: 认可重建所需的一致行占比：多数行共享同一个列数，才认为结构可靠。
_CONSISTENT_RATIO = 0.7


def _median(values: list[float]) -> float:
    """取中位数（空列表返回 0）。中位数抗离群——一个超宽表头不该抬高整表的阈值。

    Args:
        values: 数值列表。

    Returns:
        float: 中位数；输入为空时返回 0.0。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _rows(words: list[tuple[str, object]], tol: float) -> list[list[tuple[str, float, float]]]:
    """按 y 中心把词聚成行，每行内按 x 从左到右排序。

    Args:
        words: ``(文本, 包围盒)`` 列表（已过滤空文本）；包围盒需有 ``x1/x2/y1/y2``。
        tol: 行聚类容差（pt）：与上一行最后一个词的 y 中心相差不超过它即算同一行。

    Returns:
        list[list[tuple[str, float, float]]]: 每行是 ``(文本, x1, x2)`` 列表，行自上而下。
    """
    items = [(t, b.x1, b.x2, (b.y1 + b.y2) / 2) for t, b in words]
    rows: list[list[tuple]] = []
    for item in sorted(items, key=lambda it: it[3]):
        if rows and abs(item[3] - rows[-1][-1][3]) <= tol:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [[(t, x1, x2) for t, x1, x2, _cy in sorted(row, key=lambda it: it[1])]
            for row in rows]


def _cells(row: list[tuple[str, float, float]], gap: float) -> list[str]:
    """把一行里的词按横向间距切成单元格，同格内的词用空格连接。

    Args:
        row: 该行的 ``(文本, x1, x2)`` 列表（已按 x 排序）。
        gap: 跨列判定阈值（pt）。

    Returns:
        list[str]: 该行的单元格文本列表。
    """
    cells: list[str] = []
    last_right: float | None = None
    for text, x1, x2 in row:
        if last_right is not None and x1 - last_right > gap:
            cells.append(text)                     # 与上一词的间距超过阈值 → 新单元格
        elif cells:
            cells[-1] = f"{cells[-1]} {text}"      # 同格内的下一个词
        else:
            cells.append(text)                     # 本行首词
        last_right = x2
    return [c.strip() for c in cells]


def _escape(text: str) -> str:
    """转义单元格里的竖线——否则会把 markdown 表格切坏。

    Args:
        text: 原始单元格文本。

    Returns:
        str: 可安全放进 ``| … |`` 的文本（空白已折叠）。
    """
    return " ".join(text.replace("|", "\\|").split())


def table_markdown(words) -> str | None:
    """把表格区域内的词重建为 markdown 表格；判据不过返回 None（调用方退回纯文字）。

    判据（须全部满足）：至少两行、至少两列，且 ≥70% 的行共享同一个列数。认可后以第一行
    为表头；列数与多数不一致的少数行，多出的单元格并进末列、缺的补空，以保住文字不丢。

    Args:
        words: ``(文本, 包围盒)`` 序列（图区域内识别出的词）。

    Returns:
        str | None: markdown 表格文本；结构不可靠时为 None。
    """
    items = [(t, b) for t, b in words if (t or "").strip()]
    if len(items) < 4:                     # 一行两词算不上表格，省掉后面的几何计算
        return None

    med_h = _median([b.y2 - b.y1 for _t, b in items if b.y2 > b.y1])
    rows = _rows(items, _ROW_TOL_RATIO * med_h)
    if len(rows) < 2:
        return None

    gap = max(_COL_GAP_RATIO * med_h, _COL_GAP_MIN)
    grid = [_cells(row, gap) for row in rows]
    counts = [len(c) for c in grid]
    n_cols = max(set(counts), key=counts.count)           # 出现次数最多的列数
    if n_cols < 2 or counts.count(n_cols) < _CONSISTENT_RATIO * len(grid):
        return None

    def normalize(cells: list[str]) -> list[str]:
        """把一行规整到 n_cols 列：多出的并进末列（不丢文字），缺的补空。"""
        if len(cells) == n_cols:
            return cells
        if len(cells) > n_cols:
            return cells[:n_cols - 1] + [" ".join(cells[n_cols - 1:])]
        return cells + [""] * (n_cols - len(cells))

    normalized = [[_escape(c) for c in normalize(cells)] for cells in grid]
    header, body = normalized[0], normalized[1:]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * n_cols) + " |"]
    lines += ["| " + " | ".join(cells) + " |" for cells in body]
    return "\n".join(lines)
