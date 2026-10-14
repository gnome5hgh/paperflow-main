"""``ToolResult.summary`` 的跨模块键。

summary 是工具向 Agent 回传结构化结论的通道，其中几个键**跨 core↔tools 两侧读写**
（工具侧写、runtime 读）。拼错的后果不是报错而是功能静默失效——例如裁决已提交、
但 ReAct 循环认不出终止标记就继续空转——所以键名只有一个声明点。

工具自己的局部键（figures / key / created 之类）只被单个工具与它的消费者使用，
留在各自的工具文件里，不进这里。
"""

__all__ = ["SUMMARY_DECISION", "SUMMARY_TERMINAL", "SUMMARY_VIOLATIONS"]

#: 终止标记：runtime 见到即结束本轮 ReAct 循环（submit 类工具提交裁决后置位）。
#: 语义是「本 agent 的任务已终结」——返回值即裁决全文，不能再进下一轮 LLM 调用。
SUMMARY_TERMINAL = "terminal"

#: 决策结果键：被拒（user_denied / auto_denied）或安全拦截（security_blocked）时写入。
#: 两条路径的取值来自两个值域重叠的词表——审批结果（ApprovalOutcome）与决策类型
#: （ToolDecision）；这个键回答的是「这次调用为什么没执行」，落盘值就是那个字符串。
SUMMARY_DECISION = "decision"

#: 违规明细键：内容防护命中的规则列表，与决策结果一同写入
SUMMARY_VIOLATIONS = "violations"
