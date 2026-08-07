"""LookupVenueRankTool：查询论文 venue 等级（A2，spec §3.2）。

reviewer 下载审查模式专属。三层：本地映射 → LetPub(ISSN) → SJR → 未命中。
等级值带来源与证据链接，未命中显式返回"未找到等级"（不默认通过）。

网络访问复用 _SearchClientMixin 的 SSRF 安全抓取（validate_url_target +
resolve_url_target 逐跳校验重定向），httpx 同步客户端——工具已在线程池跑。
"""
import re
import urllib.parse

import httpx

from paperflow.core.security.network import validate_url_target
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools._search_common import _SearchClientMixin
from paperflow.tools._venue_rank import lookup_local, normalize_venue, RANK_CACHE, RANK_CACHE_MAX


class _VenueClient(_SearchClientMixin):
    """LetPub/SJR 抓取客户端：SSRF 校验 + 超时 + 可选 MockTransport（测试注入）。"""

    def __init__(self, transport=None, ssrf_check=None):
        self.client = httpx.Client(transport=transport, timeout=20.0)
        # ssrf_check 默认真实验证；测试传 lambda 桩注入（配合 httpx.MockTransport 隔离网络）
        self.ssrf_check = ssrf_check or validate_url_target


def _parse_letpub(html: str) -> dict | None:
    """从 LetPub 结果页提取 {jcr, cas}。解析失败返回 None（诚实降级）。

    匹配策略：页内找 JCR 分区（Q1–Q4）与中科院分区（一~四区）标记；
    找不到任一 → None（不编造）。"""
    found: dict = {}
    m = re.search(r"JCR\s*分区[:：]?\s*(Q[1-4])", html)
    if m:
        found["jcr"] = m.group(1)
    m = re.search(r"中科院分区[:：]?\s*([一二三四])区", html)
    if m:
        found["cas"] = m.group(1) + "区"
    return found if found else None


class LookupVenueRankTool(Tool):
    name = "lookup_venue_rank"
    description = ("查询论文发表 venue（期刊/会议）的等级：本地 CCF/JCR/中科院映射 + "
                   "LetPub/SJR 在线兜底。返回等级、判定（是否 ≥Q2）与证据链接。"
                   "reviewer 下载审查用——判定依据等价表 B。")
    parameters = {
        "type": "object",
        "properties": {
            "venue": {"type": "string", "description": "venue 名（OpenAlex source.display_name）"},
            "issn": {"type": "string", "description": "ISSN（可选；精确查期刊，避开同名歧义）"},
        },
        "required": ["venue"],
    }
    risk_level = "low"
    side_effects = ["network"]
    output_scan = "mark"                     # 返回外部内容（网页解析结果）→ 未校验横幅

    def __init__(self):
        super().__init__()
        # 懒建客户端：本地命中/缓存命中不走网络；仅在线兜底路径才实例化
        self._client = None

    @classmethod
    def _make_client(cls, transport=None, ssrf_check=None):
        """供测试注入 httpx.MockTransport 与 ssrf 桩（对齐 Arxiv/OpenAlex 测试模式）。"""
        return _VenueClient(transport=transport, ssrf_check=ssrf_check)

    def _rank_text(self, rank: dict, source: str, evidence: str) -> str:
        # 等级展示用 "CCF-A" / "JCR-Q1" / "CAS-一区" 大写键-值形式，
        # 便于 LLM 与 brief 测试按 "CCF-A" 断言（小写 "ccf=A" 无法命中该断言）
        parts = [f"{k.upper()}-{v}" for k, v in rank.items() if v]
        verdict = "通过（≥Q2）" if _venue_passes(rank) else "不通过"
        return (f"venue 等级：{'；'.join(parts) or '无'} | 判定：{verdict} | "
                f"来源：{source} | 证据：{evidence}")

    def _cache_put(self, ckey: tuple, rank: dict) -> None:
        """写缓存并维持 LRU 语义：新写入视为最近使用（移末尾），
        超上限逐出队头（最久未用）。brief 代码只写了写入，逐出逻辑在此补全。"""
        RANK_CACHE[ckey] = rank
        RANK_CACHE.move_to_end(ckey)
        while len(RANK_CACHE) > RANK_CACHE_MAX:
            RANK_CACHE.popitem(last=False)

    def execute(self, venue: str, issn: str | None = None) -> ToolResult:
        # ① 缓存命中（LRU：命中即视为最近使用，move_to_end）
        ckey = (normalize_venue(venue), issn)
        cached = RANK_CACHE.get(ckey)
        if cached is not None:
            RANK_CACHE.move_to_end(ckey)
            return ToolResult(text=self._rank_text(cached, "缓存", "RANK_CACHE"))
        # ② 本地映射 → 秒回，跳过网络
        local = lookup_local(venue)
        if local:
            self._cache_put(ckey, local)
            return ToolResult(text=self._rank_text(local, "本地映射表", "paperflow/tools/_venue_rank.py"))
        # ③ LetPub 在线（优先按 ISSN 精确查，避开同名期刊歧义）
        client = self._client or self._make_client()
        try:
            if issn:
                url = ("https://www.letpub.com.cn/index.php?page=journalapp&view=search"
                       "&searchissn=" + urllib.parse.quote(issn) + "&searchfield=all")
            else:
                url = ("https://www.letpub.com.cn/index.php?page=journalapp&view=search"
                       "&searchname=" + urllib.parse.quote(venue))
            r = client._get(url)
            r.raise_for_status()
            parsed = _parse_letpub(r.text)
            if parsed:
                parsed.setdefault("ccf", None)   # 等级值三字段统一，缺省字段显式置 None
                self._cache_put(ckey, parsed)
                return ToolResult(text=self._rank_text(parsed, "LetPub", url))
            # ④ SJR 兜底（按名称；SJR 只给 JCR 档位，无中科院分区）
            sjr_url = "https://www.scimagojr.com/journalsearch.php?q=" + urllib.parse.quote(venue)
            rs = client._get(sjr_url)
            rs.raise_for_status()
            m = re.search(r"Q([1-4])", rs.text)
            if m:
                rank = {"ccf": None, "jcr": f"Q{m.group(1)}", "cas": None}
                self._cache_put(ckey, rank)
                return ToolResult(text=self._rank_text(rank, "SJR", sjr_url))
        except Exception as e:
            # 网络/解析异常 → 显式报错，绝不静默回退成"通过"
            return ToolResult(text=f"等级在线查询失败: {e}")
        # ⑤ 全未命中 → 不默认通过（reviewer 需人工核验）
        return ToolResult(text=f"未找到等级（venue={venue}）——请人工核验，不默认通过")


def _venue_passes(rank: dict) -> bool:
    """等价表 B 判定（避免 _venue_rank 与工具循环 import，本地再导一次）。"""
    from paperflow.tools._venue_rank import passes_q2
    return passes_q2(rank)
