# tests/intent/test_statement_detector.py
"""任务性判别器单测：区分「下任务」与「陈述上下文」。"""
from paperflow.core.intent.statement_detector import detect_task_requested


class TestDetectTaskRequested:
    def test_imperative_returns_true(self):
        for q in ["帮我搜索circRNA的最新论文", "请下载这篇论文",
                  "能不能总结一下这篇笔记", "麻烦整理一下这些文献",
                  "搜索一下circRNA"]:
            assert detect_task_requested(q) is True

    def test_statement_direction_returns_false(self):
        # 原失败句逐字断言：陈述方向必须判成非任务
        assert detect_task_requested(
            "我的课题是做一个circRNA关联预测框架，"
            "同时预测circRNA-疾病/药物/miRNA的关联。这是我的目前方向"
        ) is False

    def test_statement_frames_variants(self):
        for q in ["我的方向是研究circRNA的搜索算法",
                  "目前我在做一个预测框架",
                  "我想研究circRNA关联预测",
                  "我对circRNA关联预测感兴趣"]:
            assert detect_task_requested(q) is False

    def test_mixed_imperative_wins(self):
        # 混合句：祈使标记优先于陈述框架
        assert detect_task_requested(
            "我的课题是circRNA预测，帮我找找相关论文") is True

    def test_neither_is_conservative_true(self):
        # 两者皆无 → 保守 True（走既有路由 + 澄清门，行为与现状一致）
        for q in ["circRNA关联预测", "下载最新论文", "那这个呢"]:
            assert detect_task_requested(q) is True
