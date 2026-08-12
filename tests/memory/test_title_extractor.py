# tests/memory/test_title_extractor.py
"""TitleExtractor 五级标题提取链：降级顺序 + 禁文件名。"""
from paperflow.core.memory.services.title_extractor import TitleExtractor, TitleResult


class _StubGrobid:
    def extract_title(self, pdf_path):
        return None            # 模拟 GROBID 失败


class _StubLLM:
    async def extract(self, prompt, schema, fallback=None):
        return None            # 模拟 LLM 失败


def test_uses_search_metadata_first():
    ex = TitleExtractor(grobid=_StubGrobid(), llm=_StubLLM(), use_pdftitle=False,
                        use_pymupdf=False)
    r = ex.extract(search_meta={"title": "异构图神经网络", "source": "arxiv:2301.001"})
    assert r.title == "异构图神经网络" and r.source == "arxiv:2301.001"


def test_pymupdf_fallback_not_filename():
    """禁文件名：PyMuPDF 层失败时返回失败，绝不返回文件名当标题。"""
    ex = TitleExtractor(grobid=_StubGrobid(), llm=_StubLLM(), use_pdftitle=False,
                        use_pymupdf=False)     # 全部禁用 → 无层可用
    r = ex.extract(pdf_path="/tmp/G-Merging- 论文标题.pdf")
    assert r.title is None
    assert "G-Merging" not in (r.title or "")     # 文件名不得当标题


def test_fallback_order_calls_layers_in_sequence():
    calls = []
    class _G:
        def extract_title(self, p):
            calls.append("grobid"); return None
    ex = TitleExtractor(grobid=_G(), llm=_StubLLM(), use_pdftitle=False,
                        use_pymupdf=False)
    ex.extract(pdf_path="/tmp/x.pdf")
    assert calls == ["grobid"]     # 无搜索元数据 → 只试 GROBID（后续层被禁用则停）


def test_llm_layer_uses_structured_output():
    import asyncio
    class _R:                       # StructuredOutput stub
        def __init__(self): self.prompt = None
        async def extract(self, prompt, schema, fallback=None):
            self.prompt = prompt
            class _O: title = "LLM 判断的标题"
            return _O()
    llm = _R()
    ex = TitleExtractor(grobid=None, llm=llm, use_pdftitle=False, use_pymupdf=False)
    # _read_first_page 依赖真实 PDF——用 stub 替换
    ex._read_first_page = lambda p: "First page text here"
    r = ex.extract(pdf_path="/tmp/fake.pdf")
    assert r.title == "LLM 判断的标题" and r.source == "llm"
