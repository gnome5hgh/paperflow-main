"""references.bib 的读写原语——引用库真相源的唯一出入口。

本包只做再导出；消费方（services/ 编排、引用工具）一律从
`paperflow.citations.storage` 取，不直接引内部模块路径。
"""

from .bib import (append_entry, ensure_file, entry_text, find_all_by_title,
                  find_by_title, parse_entries, remove_entries)

__all__ = ["ensure_file", "parse_entries", "find_by_title", "find_all_by_title",
           "entry_text", "append_entry", "remove_entries"]
