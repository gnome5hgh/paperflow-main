"""Stage 1 追问检测测试：ADR 三边界案例 + 词表组合。"""
from paperflow.core.intent.followup_detector import detect_followup
from paperflow.core.intent.intent_schema import IntentType


class TestAdrBoundaryCases:
    """ADR §管线 的三个边界案例——行为契约，必须保持。"""

    def test_figure_reference_inherits(self):
        # "那 Figure 3 呢？" 引用上轮对象 → 继承 ✓
        assert detect_followup("那 Figure 3 呢？", IntentType.ASK_QUESTION) is True

    def test_new_action_verb_routes(self):
        # "再下载一篇" 含动作词"下载" → 正常路由 ✓
        assert detect_followup("再下载一篇", IntentType.SEARCH_PAPER) is False

    def test_quantifier_points_new_object(self):
        # "这篇呢？" "这篇"+量词 → 指向列表新对象 → 不继承 ✓
        assert detect_followup("这篇呢？", IntentType.SEARCH_PAPER) is False


class TestWordlists:
    def test_no_prev_intent(self):
        assert detect_followup("那 Figure 3 呢？", None) is False

    def test_no_marker(self):
        assert detect_followup("搜索 circRNA 文献", IntentType.SEARCH_PAPER) is False

    def test_marker_without_verb_inherits(self):
        assert detect_followup("然后呢？", IntentType.GENERATE_NOTE) is True

    def test_marker_with_verb_routes(self):
        assert detect_followup("再搜索一篇", IntentType.SEARCH_PAPER) is False
