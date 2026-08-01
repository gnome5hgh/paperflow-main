# paperflow/core/intent/__init__.py
"""
意图识别框架服务（ADR 0007）。

Task 1 为契约层占位：路由契约类型（Route / RouteChoice）在 ``schema.py``，
意图输出契约（IntentType / IntentStep / IntentOutput / IntentionResult）在
``intent_schema.py``。后续任务（实体提取 / 管线 / 路由器组装）加入各子模块后，
再在包级 __init__ 汇总导出，参照 ``paperflow.core.memory`` 的导出风格。
"""
