# paperflow/tools/review/_validate.py
"""review 子包共享校验 helper（submit_review / submit_download_review 同构段收敛）。

两工具的 execute 都做「枚举成员校验 → 非法输入返回可行动报错文本」：枚举错说明
reviewer 的 LLM 理解偏差，报错给 LLM 修正后重试，绝不静默吞、不让非法值通过
（校验哲学对齐 edit_file 的 miss/multi 报错）。此处收敛纯校验逻辑——格式化与
「verdict↔issues/items 一致性」检查仍留在各 Tool 类（字段结构不同）。
"""

#: verdict 合法值（两工具共用：note 审查 verdict 与下载审查 verdict 都是 pass/fail）
VERDICTS = ("pass", "fail")


def enum_check(value, allowed: tuple, label: str) -> str | None:
    """枚举成员校验：value ∈ allowed 返回 None；否则返回可行动报错文本。

    报错文案带当前值（即使 None 也展示）与合法值列表，让 reviewer 的 LLM 看到
    「哪个字段、当前是什么、合法范围是什么」后自行修正。label 是字段名。
    """
    if value not in allowed:
        return f"{label} 非法: {value}，应为 {', '.join(allowed)}"
    return None
