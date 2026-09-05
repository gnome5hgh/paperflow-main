"""SemanticScholarClient：Semantic Scholar Graph API 纯搜索客户端（工具本体在 web_search.py）。

SSRF 校验走共享 paperflow/tools/common/_http.py。只读操作,写盘/下载由 FetchPdfTool
承担。用于按相关性找「最相似已存在工作」(选题发现的新颖性检索)：S2 的 relevance 检索
比 arxiv 关键词更贴近该用途。api key 可选——经环境变量 PAPERFLOW_S2_API_KEY 注入
(客户端 __init__ 的 api_key 供测试直传),配置后带 x-api-key 头走高配额端点,留空则
公共端点(429/失败沿源级熔断/降级)。
"""
import os
import urllib.parse

import httpx

from paperflow.core.security.network import resolve_url_target, validate_url_target
from paperflow.tools.common._http import _HttpClientMixin

_API = "https://api.semanticscholar.org/graph/v1/paper/search"


class SemanticScholarClient(_HttpClientMixin):
    def __init__(self, transport=None, ssrf_check=None, api_key: str = ""):
        """建 httpx 同步客户端;transport/ssrf_check 供测试注入;api_key 未传时读环境变量。"""
        self.client = httpx.Client(transport=transport, timeout=30.0)
        self.ssrf_check = ssrf_check or validate_url_target
        # key 遵循仓库「从 .env/环境变量读取」惯例;.env 由 from_env 的 load_dotenv 加载
        self.api_key = api_key or os.environ.get("PAPERFLOW_S2_API_KEY", "")

    def search(self, query: str, max_results: int = 5,
               year_from: int | None = None, year_to: int | None = None) -> list[dict]:
        """调 S2 Graph API 相关性检索,返回论文 dict 列表。

        年份用 year 区间过滤(闭区间,缺侧开放),与检索词分离。结果字段对齐现有
        schema:doi 用于跨源去重(最高优先级),arxiv_id 兜底,标题规范化去重兜底。
        开放获取 pdf 未必总返回,downloadable 据此判定;venue 尽量给出(issn 缺失不
        强求——等级由 reviewer 后查)。有 key 走带 x-api-key 头的 GET;两条路径都做
        同样的 SSRF 逐跳校验。
        """
        params = {
            "query": query,
            "limit": max_results,
            # 相关性检索只要标题+摘要+必要元数据,字段瘦身省带宽
            "fields": "title,year,abstract,venue,externalIds,openAccessPdf,citationCount",
        }
        # 年份用 S2 的 year 字段做区间过滤(闭区间,缺侧开放),不拼进自由文本 query
        if year_from or year_to:
            params["year"] = f"{year_from or ''}-{year_to or ''}"
        url = _API + "?" + urllib.parse.urlencode(params)
        if self.api_key:
            headers = {"x-api-key": self.api_key}
            # 带 key 路径:先校验原始 URL,再 resolve 重定向逐跳、最终 URL 再校验一次
            self.ssrf_check(url)
            resolved = resolve_url_target(url)
            self.ssrf_check(resolved)
            r = self.client.get(resolved, headers=headers)
        else:
            # 无 key 走共享 _get(内部已做同样的 SSRF 逐跳校验)
            r = self._get(url)
        r.raise_for_status()
        papers = []
        for p in r.json().get("data", []):
            ext = p.get("externalIds") or {}
            papers.append({
                "title": p.get("title", ""),
                "year": p.get("year"),
                "citation_count": p.get("citationCount", 0),
                "paperId": p.get("paperId", ""),
                # DOI 是跨源去重最高优先级键(doi > arxiv_id > 规范化标题)
                "doi": ext.get("DOI"),
                "arxiv_id": ext.get("ArXiv"),
                "venue": p.get("venue"),
                "issn": None,                       # S2 不直接给 ISSN;等级由 reviewer 后查
                "pdf_url": (p.get("openAccessPdf") or {}).get("url"),
                "downloadable": bool((p.get("openAccessPdf") or {}).get("url")),
                "externalIds": ext,
            })
        return papers
