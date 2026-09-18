"""YAML frontmatter + Markdown 正文解析 —— AgentRegistry 与 SkillRegistry 共用。

「frontmatter + 正文」是本项目两种插件文件（AGENT.md / SKILL.md）的共同形态，
解析逻辑只有一份，避免两套注册表行为漂移。
"""

import re

import yaml

#: 匹配 ``---\n...\n---\n`` 头部；DOTALL 让 . 跨行。
#: 与 AgentRegistry 原正则的唯一差异：``(.*?)\n`` 整体可选化（``(?:(.*?)\n)?``），
#: 使空 frontmatter（``---\n---\n正文``）在原正则必然失配时也能命中——
#: 此时 group(1) 未参与匹配（None，函数体内归一为空串再交给 safe_load，
#: 因本环境 yaml.safe_load(None) 会直接抛 AttributeError）。
#: 只要存在合法闭合围栏，可选分支优先参与且分组与原实现逐字一致：
#: 值中的行内 ``---`` 不会被误判为闭合围栏，块标量等对末尾换行敏感的写法也不受影响。
#: 解析契约见 parse_frontmatter docstring。
_FRONTMATTER_RE = re.compile(r"^---\s*\n(?:(.*?)\n)?---\s*\n?(.*)", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """分离文件开头的 YAML frontmatter 与 Markdown 正文。

    :param text: 文件全文
    :returns: (frontmatter 字典, body 文本)。无 frontmatter 时 frontmatter 为空字典、
              body 为原文；frontmatter 为空段时返回空字典
              （group(1) 未参与匹配或为空串，归一为空串 → yaml.safe_load('') → None
              → or {} → {}；注意 safe_load(None) 在本环境会抛 AttributeError，故先归一）。
    :注意: 使用 safe_load 只解析基本类型，不执行任意代码。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1) or "") or {}, m.group(2).strip()
