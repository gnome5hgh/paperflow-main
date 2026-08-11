# tests/test_text_util.py
"""未配对 surrogate 清洗测试（Layer 4 实测暴露：PDF 提取脏文本 → embed/LLM 崩溃）。"""
from paperflow.core.security.text import sanitize_surrogates
from paperflow.core.llm import Message, _message_to_openai


class TestSanitizeSurrogates:
    def test_normal_text_unchanged(self):
        assert sanitize_surrogates("正常中文与 ascii text") == "正常中文与 ascii text"
        assert sanitize_surrogates("") == ""

    def test_replaces_lone_surrogate_with_replacement_char(self):
        # 孤立高代理位（\ud800）与低代理位（\udcff）都替换为 U+FFFD
        assert sanitize_surrogates("a\ud800b") == "a�b"
        assert sanitize_surrogates("a\udcffb") == "a�b"

    def test_valid_surrogate_pair_untouched(self):
        # 合法代理对（如 emoji）不是"未配对"，不应被误伤
        s = "emoji \U0001f600 保留"
        assert sanitize_surrogates(s) == s

    def test_repairs_surrogateescape_sequence_lossless(self):
        # 不 = E4 B8 8D 被 surrogateescape 拆成 \udce4\udcb8\udc8d → 回环无损还原
        assert sanitize_surrogates("\udce4\udcb8\udc8d") == "不"

    def test_repairs_whole_sentence_mangled_by_surrogateescape(self):
        # 整句中文字符全被 surrogateescape 逐字节拆开 → 全部无损还原（原会降级成 �）
        mangled = "".join(bytes([b]).decode("utf-8", "surrogateescape")
                          for b in "做一个".encode("utf-8"))
        assert sanitize_surrogates(mangled) == "做一个"

    def test_isolated_byte_falls_back_to_replacement(self):
        # 0xE4 孤立（截断的 3 字节序列）→ 无法还原 → 降级 U+FFFD（不崩溃）
        assert sanitize_surrogates("a\udce4b") == "a�b"


class TestBoundarySanitization:
    def test_llm_message_content_sanitized(self):
        """出站消息内容含 surrogate → 转 OpenAI dict 时已替换（openai SDK UTF-8 编码不炸）。"""
        msg = _message_to_openai(Message(role="user", content="a\udcffb"))
        assert msg["content"] == "a�b"

    def test_llm_clean_message_unchanged(self):
        msg = _message_to_openai(Message(role="user", content="干净文本"))
        assert msg["content"] == "干净文本"
