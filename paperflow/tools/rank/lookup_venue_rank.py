"""LookupVenueRankTool：查询论文发表 venue(期刊/会议)的等级。

reviewer 下载审查模式专属。查询链:本地映射 → LetPub(ISSN) → SJR → 未命中。
等级值带来源与证据链接,未命中显式返回"未找到等级"(不默认通过,交由人工核验)。

网络访问复用 _HttpClientMixin 的 SSRF 安全抓取(逐跳校验重定向),httpx 同步客户端
——工具已在线程池里跑。
"""
import re
import urllib.parse

import httpx

from paperflow.core.security.network import validate_url_target
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.common._http import _HttpClientMixin
from paperflow.tools.rank._venue_rank import lookup_local, normalize_venue, RANK_CACHE, RANK_CACHE_MAX


class _VenueClient(_HttpClientMixin):
    """LetPub/SJR 抓取客户端:SSRF 校验 + 超时 + 浏览器头 + 可选 MockTransport(测试注入)。

    浏览器头是反爬关键:LetPub 对无浏览器标识的请求可能返回验证页/空结果页,抓不到分区
    行;Accept/Accept-Language 模拟真实浏览器,降低被识别为脚本的概率。
    """

    #: 浏览器 UA + Accept 头——LetPub 反爬识别无 UA 客户端并返回空结果（见类 docstring）
    _BROWSER_HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(self, transport=None, ssrf_check=None):
        self.client = httpx.Client(transport=transport, timeout=20.0,
                                   headers=dict(self._BROWSER_HEADERS))
        # ssrf_check 默认真实验证；测试传 lambda 桩注入（配合 httpx.MockTransport 隔离网络）
        self.ssrf_check = ssrf_check or validate_url_target


def _parse_letpub(html: str) -> dict | None:
    """从 LetPub 结果页提取中科院分区;解析失败返回 None(诚实降级)。

    结果页每个数据行形如:<td>ISSN</td><td>刊名…</td><td>IF/h-index…</td><td>4区</td>,
    第 4 列即分区列(1-4 区/一-四区)。只取第一个数据行:精确检索时首行即目标期刊;
    不做整页扫描,避免命中列表里无关期刊的分区(歧义误判的根源)。"""
    # 阿拉伯数字分区（4区）转中文（四区），与等级值域 cas∈{一区..四区} 对齐
    _CN_NUM = {"1": "一", "2": "二", "3": "三", "4": "四"}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
        if len(cells) < 4:
            continue
        m = re.search(r"([1-4]|[一二三四])区", cells[3])
        if not m:
            continue
        zone = m.group(1)
        cas = (_CN_NUM[zone] + "区") if zone in _CN_NUM else (zone + "区")
        return {"ccf": None, "jcr": None, "cas": cas}
    return None


#: SJR 结果页里期刊名之后允许出现档位的窗口（字符）。SJR 搜索页是候选期刊
#: 列表，同一结果行的档位徽章紧跟期刊名之后；窗口限定在名称后一小段内，
#: 防止跨到列表里下一本期刊的档位（歧义误判的根源，见 _parse_sjr 注释）。
_SJR_WINDOW_CHARS = 500


def _parse_sjr(html: str, venue: str) -> dict | None:
    """从 SJR 搜索页提取目标期刊的 JCR 档位(Q1–Q4);失败返回 None。

    为什么限定窗口而非整页扫描:SJR 搜索页返回候选期刊列表,整页取首个 Q 档会命中
    列表里无关期刊的档位——venue 歧义(多本期刊含同名关键词)时误判「通过」,违反
    「等级未知 → 不默认通过」。修复:先定位规范化 venue 名在页内首次出现的位置,
    只在其后 ~500 字符窗口内找 Q 档;找不到 → None(走「未找到等级」,让 reviewer
    人工核验)。归一化(小写 + 去非字母数字)让标题与页面文本去掉标签/大小写干扰后对齐。
    """
    norm_page = re.sub(r"[^a-z0-9]", "", (html or "").lower())
    norm_venue = re.sub(r"[^a-z0-9]", "", (venue or "").lower())
    if not norm_venue:
        return None
    idx = norm_page.find(norm_venue)
    if idx < 0:
        return None
    # 只取名称起点的窗口：结果行档位徽章在标题之后；先到名称自身末尾再往后看 500 字符。
    # 窗口是归一化（小写）文本，Q 档正则须用小写 q 匹配（大写 Q 在归一化后恒失配）。
    window = norm_page[idx: idx + len(norm_venue) + _SJR_WINDOW_CHARS]
    m = re.search(r"q([1-4])", window)
    if not m:
        return None
    return {"ccf": None, "jcr": f"Q{m.group(1)}", "cas": None}


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
        """把等级 dict 渲染成 LLM 可见文本:等级、判定(≥Q2)、来源与证据链接。

        等级键用大写形式(CCF-A / JCR-Q1 / CAS-一区),便于 LLM 与测试按该形式断言。
        """
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
        """查询 venue 等级并返回渲染文本;全链路 fail-closed,绝不静默回退成「通过」。

        查询链:缓存 → 本地映射 → LetPub(优先按 ISSN 精确查,避开同名歧义)→ SJR →
        全未命中。网络/解析异常显式报错,未命中显式「请人工核验,不默认通过」。
        """
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
                       "&searchissn=" + urllib.parse.quote(issn))
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
            # ④ SJR 兜底（按名称；SJR 只给 JCR 档位，无中科院分区）。
            #    _parse_sjr 把 Q[1-4] 检索限定在 venue 名上下文内（歧义列表页
            #    不取无关期刊档位）——见函数注释。
            sjr_url = "https://www.scimagojr.com/journalsearch.php?q=" + urllib.parse.quote(venue)
            rs = client._get(sjr_url)
            rs.raise_for_status()
            rank = _parse_sjr(rs.text, venue)
            if rank:
                self._cache_put(ckey, rank)
                return ToolResult(text=self._rank_text(rank, "SJR", sjr_url))
        except Exception as e:
            # 网络/解析异常 → 显式报错，绝不静默回退成"通过"
            return ToolResult(text=f"等级在线查询失败: {e}")
        # ⑤ 全未命中 → 不默认通过（reviewer 需人工核验）
        return ToolResult(text=f"未找到等级（venue={venue}）——请人工核验，不默认通过")


def _venue_passes(rank: dict) -> bool:
    """按「期刊 JCR Q1/Q2、中科院一/二区、会议 CCF-A/B」判定是否通过。

    本地再导一次 passes_q2,避免与 _venue_rank 模块循环 import。
    """
    from paperflow.tools.rank._venue_rank import passes_q2
    return passes_q2(rank)
