# paperflow/terminal/errors.py
"""错误 → 用户语言翻译：API 错误原文不再是唯一呈现（真实使用测试 P3-2）。

此前 REPL 把异常原样打印（"Error code: 402 - Insufficient Balance"），用户
看到的是 API 原文而不是「余额不足，请充值」这类可行动指引。translate_error
把常见错误映射为用户语言，并保留一行 dim 原文便于报障（cli 的 key 守卫与
repl 的异常兜底共用）。
"""
from __future__ import annotations

#: (匹配子串, 用户语言提示) 顺序即优先级——先命中先得
_RULES: list[tuple[str, str]] = [
    ("insufficient balance", "模型服务余额不足——请前往服务商控制台充值后重试。"),
    ("error code: 402", "模型服务余额不足——请前往服务商控制台充值后重试。"),
    ("error code: 401", "API key 无效或已过期——请检查 .env 中的 PAPERFLOW_API_KEY。"),
    ("invalid api key", "API key 无效或已过期——请检查 .env 中的 PAPERFLOW_API_KEY。"),
    ("error code: 429", "请求过于频繁（限流）——稍等片刻后重试。"),
    ("must be followed by tool messages",
     "会话历史出现异常（已自动修复）——请重新发送上一条消息。"),
    ("context length", "对话过长超出模型上下文——请开启新会话或缩小任务范围。"),
    ("timeout", "模型服务响应超时——请稍后重试。"),
    ("connect", "网络连接异常——请检查网络或代理设置后重试。"),
]

#: 原文保留长度上限（排障够用，不刷屏）
_ORIGINAL_LIMIT = 200


def translate_error(e: BaseException) -> str:
    """把异常翻译成用户语言 + 一行原文（dim 呈现由调用方处理）。

    无规则命中时给出通用提示；原文恒保留一行，便于贴给维护者排查。
    """
    raw = str(e)
    low = raw.lower()
    for needle, message in _RULES:
        if needle in low:
            return f"{message}\n（详情：{raw[:_ORIGINAL_LIMIT]}）"
    return f"任务执行出错：{raw[:_ORIGINAL_LIMIT]}"
