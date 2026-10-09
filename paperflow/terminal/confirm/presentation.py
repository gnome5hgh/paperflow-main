"""确认框的呈现：一行提示 + 写/编辑的 diff 预览。

`_confirm_prompt` 画确认框的标题行（图标动词复用活动行的 activity_label，目标
取路径尾段）；`_confirm_diff_preview` 为写/编辑工具算出「将要发生什么」的
unified diff，让用户在按 y 之前看得到实际改动。两者都只产出文本，渲染与按键
读取由 center.py 负责。
"""
from __future__ import annotations

from pathlib import Path

from ..common import compute_diff, truncate_diff
from ..render.activity import activity_label


def _confirm_prompt(cr) -> str:
    """确认框一行提示：左边条 + 图标动词 + 目标 + 键提示。

    图标动词复用活动行的 activity_label；目标取 params 里的 path 尾段
    （basename），取不到 path（如 spawn_sub_agent）就只显示工具名。
    键绑定不变：y=本次放行 / a=本会话放行 / n=拒绝。

    Args:
        cr: ConfirmRequired，待确认的工具调用

    Returns:
        确认框一行提示文本（左边条 + 图标动词 + 目标 + 键提示）。
    """
    tool_name = getattr(cr, "tool_name", "") or ""
    verb, _ = activity_label(tool_name)
    params = getattr(cr, "params", None)
    path = params.get("path") if isinstance(params, dict) else None
    target = (str(path).rstrip("/").rsplit("/", 1)[-1]
              if path else (tool_name or "确认"))
    return f"┃ {verb} {target}　y 放行 / a 本会话放行 / n 拒绝"


def _confirm_diff_preview(tool_name: str, params: dict) -> str | None:
    """
    为写/编辑工具生成确认前的 diff 预览文本。

    Args:
        tool_name: 工具名称（"write_file" 或 "edit_file"）。
        params: 工具参数字典，需包含 "path"，write_file 还需 "content"，
                edit_file 还需 "old_text" 和 "new_text"。

    Returns:
        str | None: 若预览可用则返回截断后的 unified diff 字符串；
                    若工具不是写/编辑、参数缺失、文件读取失败或编辑替换条件不满足，
                    则返回 None（表示走纯确认，无预览）。

    关键边界条件（edit_file）：
        - edit_file 工具仅在 old_text 在文件中恰好出现一次时才执行替换。
          若 count != 1，则实际不会写入，此时预览与当前内容无异，反而干扰用户，
          因此直接返回 None，只走纯确认。
        - 若文件不存在，old 为空字符串，count=0，亦返回 None。
    """
    if tool_name not in {"write_file", "edit_file"}:
        return None
    path = params.get("path") if isinstance(params, dict) else None
    if not path:
        return None
    p = Path(path)
    try:
        old = p.read_text(encoding="utf-8") if p.exists() else ""
    except (OSError, UnicodeDecodeError):
        return None
    if tool_name == "write_file":
        new = params.get("content", "")
    else:  # edit_file
        old_text, new_text = params.get("old_text"), params.get("new_text")
        if old_text is None or new_text is None:
            return None
        # 仅在替换确实会应用时预览：要求 old_text 在文件中恰好出现一次
        if old.count(old_text) != 1:
            return None
        new = old.replace(old_text, new_text)
    return truncate_diff(compute_diff(old, new, fromfile=str(p), tofile=str(p)))
