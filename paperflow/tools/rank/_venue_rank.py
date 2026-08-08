"""venue 等级查询：本地映射表 + 等价表判定。

查询策略(配合 lookup_venue_rank 工具)：
1. 本地映射表 → 秒回(本模块)
2. LetPub 按 ISSN 在线查(中科院分区 + JCR 分区)
3. SJR 兜底
4. 全未命中 → "未找到等级"(不默认通过)

等级值：{"ccf": "A"|"B"|"C"|None, "jcr": "Q1".."Q4"|None, "cas": "一区".."四区"|None}

本模块只承载"本地数据 + 规范化 + 判定",不含网络;网络查询(LetPub/SJR)在
lookup_venue_rank.py。
"""
import re
from collections import OrderedDict

#: 规范化名称 → 等级。增量维护：新 venue 按此格式补键（查找前先过 normalize_venue）。
VENUE_RANKS: dict[str, dict] = {
    # 会议（CCF 推荐目录 A/B/C）
    "www": {"ccf": "A", "jcr": None, "cas": None},          # WWW / The Web Conference
    "kdd": {"ccf": "A", "jcr": None, "cas": None},
    "neurips": {"ccf": "A", "jcr": None, "cas": None},
    "icml": {"ccf": "A", "jcr": None, "cas": None},
    "iclr": {"ccf": "A", "jcr": None, "cas": None},
    "acl": {"ccf": "A", "jcr": None, "cas": None},
    "cvpr": {"ccf": "A", "jcr": None, "cas": None},
    "iccv": {"ccf": "A", "jcr": None, "cas": None},
    "nips": {"ccf": "A", "jcr": None, "cas": None},          # NeurIPS 旧名
    "aaai": {"ccf": "B", "jcr": None, "cas": None},
    "ijcai": {"ccf": "B", "jcr": None, "cas": None},
    "sigir": {"ccf": "B", "jcr": None, "cas": None},
    "emnlp": {"ccf": "B", "jcr": None, "cas": None},
    "naacl": {"ccf": "B", "jcr": None, "cas": None},
    "icde": {"ccf": "A", "jcr": None, "cas": None},
    "sigmod": {"ccf": "A", "jcr": None, "cas": None},
    "vldb": {"ccf": "A", "jcr": None, "cas": None},
    # 期刊（JCR / 中科院）
    "ieeetransactionsonpatternanalysisandmachineintelligence": {"ccf": None, "jcr": "Q1", "cas": "一区"},
    "ieeetransactionsonknowledgeanddataengineering": {"ccf": None, "jcr": "Q1", "cas": "一区"},
    "acmtransactionsoninformationsystems": {"ccf": None, "jcr": "Q1", "cas": "二区"},
    "ieeetransactionsonneuralnetworksandlearningsystems": {"ccf": None, "jcr": "Q1", "cas": "一区"},
}

#: 等级查询 LRU（venue 规范化名 + issn → rank），跨会话共享。
#: LRU 逐出逻辑在 lookup_venue_rank 的写缓存路径（超 RANK_CACHE_MAX 时 popitem(last=False)）。
RANK_CACHE: OrderedDict[tuple, dict] = OrderedDict()
RANK_CACHE_MAX = 200

#: 规范化时剥离的会议噪音前后缀(小写后匹配)。
#: 注意:不剥期刊前缀("ieeetransactionson"/"acmtransactionson")——期刊映射键是
#: "前缀+刊名"的完整小写串(如 ieeetransactionsonknowledgeanddataengineering),剥了
#: 反而失配。长前缀在前:先剥完整前缀,避免先剥 "proceedingsofthe" 留下 "acm…" 再失配。
_NOISE_WORDS = (
    "proceedingsoftheacm",
    "proceedingsofthe",
    "theacm",
    "internationalconferenceon",
    "conferenceon",
    "annualmeetingof",
    "the",
)

#: 规范化名 → VENUE_RANKS 主键 别名表：常见缩写/官方改名指向主键。
#: 主键命名规则是"小写全名去空格"，缩写（TPAMI）与改名（Web Conference←WWW）
#: 无法从全名直接推出，需显式别名。
_ALIASES = {
    "webconference": "www",   # "The Web Conference"（原 WWW 会议）官方改名
    "tpami": "ieeetransactionsonpatternanalysisandmachineintelligence",
}


def normalize_venue(name: str) -> str:
    """规范化：小写、去非字母数字，剥会议噪音前缀，再查别名表。返回空串则无法匹配。"""
    s = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    for w in _NOISE_WORDS:
        if s.startswith(w):
            s = s[len(w):]
    return _ALIASES.get(s, s)


def lookup_local(venue: str) -> dict | None:
    """本地映射表查询；未命中返回 None。"""
    if not venue:
        return None
    return VENUE_RANKS.get(normalize_venue(venue))


def passes_q2(rank: dict) -> bool:
    """等级是否达到「≥Q2」:期刊 JCR Q1/Q2 或中科院一/二区,或会议 CCF-A/B。其余不通过。"""
    if rank.get("jcr") in ("Q1", "Q2"):
        return True
    if rank.get("cas") in ("一区", "二区"):
        return True
    if rank.get("ccf") in ("A", "B"):
        return True
    return False
