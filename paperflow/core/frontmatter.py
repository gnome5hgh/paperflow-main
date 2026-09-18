"""YAML frontmatter + Markdown 正文解析 —— AgentRegistry 与 SkillRegistry 共用。

「frontmatter + 正文」是本项目两种插件文件（AGENT.md / SKILL.md）的共同形态，
解析逻辑只有一份，避免两套注册表行为漂移。
"""

import re

import yaml

#: 匹配 ``---\n...\n---\n`` 头部；DOTALL 让 . 跨行。
#: 闭合围栏前的 ``\n?`` 使空 frontmatter（``---\n---\n正文``）也能命中，避免围栏
#: 泄漏进 body；除此一处外与 AgentRegistry 原实现等价（详见模块 docstring 契约）。
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n?---\s*\n?(.*)", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """分离文件开头的 YAML frontmatter 与 Markdown 正文。

    :param text: 文件全文
    :returns: (frontmatter 字典, body 文本)。无 frontmatter 时 frontmatter 为空字典、
              body 为原文；frontmatter 为空段时返回空字典（yaml.safe_load(None) → {}）。
    :注意: 使用 safe_load 只解析基本类型，不执行任意代码。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, m.group(2).strip()
