"""图检测：FigureDetector（pdffigures2 的 FigureDetector.scala 移植）。

给一页已分类的文本（正文/图内文本）+ 图形区 + 图注，为每个图注构建候选图区域
（proposal），再枚举「每图注各取一个 proposal」的笛卡尔积配置、给配置打分，
取总分最高的配置作为本页图结果。三个阶段：

1. buildProposals：对每个图注向四个方向（上/下/左/右）试探扩展，得到候选区域。
   扩展被非图内容（正文、其它图注、非图图形）挡住；再经 cropToCenter（双栏
   按栏缝裁剪）、clipUpwardRegion（向上聚类裁剪）、crop 收边、拦腰切图/贴边界
   过滤，得到每个图注的一组候选。
2. splitProposals：把同一配置里相互重叠的 proposal 尝试水平拆分（上下叠的双图
   靠此把两块区域分开）。
3. scoreProposal + no-overlap：每个 proposal 按面积占比打分，加分项是包含图形/
   大图形、拆分空白；但任一 proposal 与同配置其它 proposal 重叠 → 判非法（None），
   非法 proposal 使整配置总分被惩罚，从而选出「各图注各占一块、互不重叠」的配置。
   这正是上下叠双图场景能正确配对的关键：不判重叠的话，上方图注会吞掉下方整片
   图区域（面积分最高），把两张图误配给一个图注。

坐标约定沿用 geometry：x 向右、y 向下，(x1,y1) 左上、(x2,y2) 右下，闭区间。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product

from paperflow.vision.parsers.caption import Caption, CaptionParagraph
from paperflow.vision.parsers.document_layout import DocumentLayout
from paperflow.vision.geometry import (
    Box,
    Box_container,
    Box_crop,
    Paragraph,
    find_empty_horizontal_blocks,
)
from paperflow.vision.detectors.region_classifier import PageWithBodyText

# ---- 常量（照 FigureDetector.scala 全量照抄）----
# 候选图区域的最小尺寸：小于该尺寸的 proposal 视为无意义，直接丢弃
MinProposalHeight = 15
MinProposalWidth = 20

# clipUpwardRegion 聚类/裁剪用常量：大图形种子面积、文本按面积分档的距离阈值
ClipRegionMinGraphicSize = 5000
ClipRegionLargeTextSize = 3000
ClipRegionMaxTextDistance = 4
ClipRegionMaxLargeTextDistance = 10
ClipRegionMaxGraphicDistance = 20

# scoreProposal 打分权重（刻意固定，改动需谨慎）
LeftRightFigurePenalty = 0.75      # 左/右方向（图注在侧面）的折扣
SplitDifferentTypesPenalty = 0.75  # 拆开的上下两半类型不同（图/表）的折扣
SplitSameTypesPenalty = 0.5        # 拆开的上下两半同类型（图/图）的折扣
SplitWhitespaceBonus = 2           # 拆分空白带面积的加分系数
LargeGraphicThreshold = 2000       # 「大图形」面积门槛
ContainsGraphicBonus = 0.1         # 区域内包含图形 → 加分
ContainsLargeGraphicBonus = 0.15   # 区域内包含大图形 → 额外加分

# 上下拆分时空白带须离上下边界都超过 区域高/4 才认（否则只是页边空白）
SplitVerticalRegionMinHeightFraction = 4

# 过滤：proposal 与某图形元素相交面积占该元素 10%~90% → 拦腰切图，丢弃；
# 区域贴页面左/上边界 30pt 内 → 很可能误扩，丢弃
cutFilterIntervalMin = 0.1
cutFilterIntervalMax = 0.9
boundaryFilterMinDistance = 30


@dataclass(frozen=True)
class Proposal:
    """一个「图注 + 候选图区域」的 proposal。

    Attributes:
        region: 候选图区域。
        caption: 对应的图注。
        dir: 区域相对图注的扩展方向（Up/Left/Down/Right）。
        split_with: 水平拆分后记下被拆开的另一半图注与中间空白带
            （(CaptionParagraph, Box)），供打分加分与类型惩罚；未拆为 None。
    """

    region: Box
    caption: CaptionParagraph
    dir: str
    split_with: tuple[CaptionParagraph, Box] | None = None


@dataclass(frozen=True)
class PageWithFigures:
    """一页图检测结果。

    Attributes:
        page_number: 页码。
        non_figure_text: 非图文本段落（未被图区域吞掉的正文/图内文本 + 失败图注段落）。
        classified_text: 全页文本（透传 PageWithBodyText.classified_text）。
        figures: 检测出的图（dict，键对齐 Figure.scala：name/fig_type/page/
            caption_text/image_text/caption_boundary/region_boundary），由 extractor
            桥接成 schemas.Figure。
        failed_captions: 配不到图的图注（精简版 Caption）。
    """

    page_number: int
    non_figure_text: list[Paragraph]
    classified_text: str
    figures: list[dict]
    failed_captions: list[Caption]


def _paragraph_sort_key(p: Paragraph) -> tuple[float, float]:
    """段落阅读序排序键：先上后下、同行先左后右。

    本移植的 Paragraph 没有行号（Scala 用 startLineNumber 排序），故用
    上缘 y + 左缘 x 近似阅读序。正文/图内文本/图注已各自按阅读序排列，
    合并排序时用该键即可得到整体阅读序。

    Args:
        p: Paragraph，待排序段落

    Returns:
        (上缘 y, 左缘 x) 排序键，近似阅读序（先上后下、同行先左后右）。
    """
    return (p.boundary.y1, p.boundary.x1)


def _sorted_paragraphs(paragraphs: list[Paragraph]) -> list[Paragraph]:
    """按阅读序返回排序后的段落副本。

    Args:
        paragraphs: list[Paragraph]，待排序段落

    Returns:
        按 _paragraph_sort_key 排序的新列表。
    """
    return sorted(paragraphs, key=_paragraph_sort_key)


def _content_of_page(page: PageWithBodyText) -> tuple[list[Box], list[Box]]:
    """一页的内容框：非图内容与可能图内容（对应 nonFigureContent/possibleFigureContent）。

    非图内容（正文 + 图注 + 非图图形）挡住 proposal 的扩展边界；可能图内容
    （图形 + 图内文本）用于切图判断与双栏中心线检测。两组合并即全页内容外接。

    Args:
        page: PageWithBodyText，已分类的页面

    Returns:
        (非图内容框, 可能图内容框) 两个 Box 列表。
    """
    non_figure_content = (
        [p.boundary for p in page.body_text]
        + [c.boundary for c in page.captions]
        + page.non_figure_graphics
    )
    possible_figure_content = page.graphics + [p.boundary for p in page.other_text]
    return non_figure_content, possible_figure_content


# ---------------------------------------------------------------------------
# proposal 构建辅助：中心线、拆分、聚类、裁剪
# ---------------------------------------------------------------------------


def _find_center_column(page: PageWithBodyText) -> tuple[float, float]:
    """估算双栏页中栏缝的 x1/x2（照 findCenterColumn）。

    先取全页文本（正文+图内文本+图注）外接矩形的水平中心做基准，再尝试用正文
    栏边界修正：左栏最右 x2、右栏最左 x1 若与中心相距 <10pt 就用它们。栏缝
    估计不准会直接带偏左右方向的候选区域，这是双栏 proposal 的关键输入。

    Args:
        page: 已分类的页面。

    Returns:
        (中心线左边界, 中心线右边界) 的元组，即栏缝的左右位置。
    """
    all_boundaries = [p.boundary for p in page.body_text + page.other_text] + [
        c.boundary for c in page.captions
    ]
    text_center = Box_container(all_boundaries).xCenter
    left_side = [p for p in page.body_text if p.boundary.x2 < text_center]
    right_side = [p for p in page.body_text if p.boundary.x1 > text_center]
    center_x1 = text_center
    if left_side:
        left_x2 = max(p.boundary.x2 for p in left_side)
        if abs(left_x2 - text_center) < 10:
            center_x1 = left_x2
    center_x2 = text_center
    if right_side:
        right_x1 = min(p.boundary.x1 for p in right_side)
        if abs(right_x1 - text_center) < 10:
            center_x2 = right_x1
    return (center_x1, center_x2)


def _split_region_horizontally(
    proposal_region: Box, content: list[Box]
) -> tuple[Box, Box, Box] | None:
    """尝试把 proposal_region 水平拆成上下两块（照 splitRegionHorizontally）。

    启发式：用 findEmptyHorizontalBlocks 找出区域内的水平空白带，空白带须离上下
    边界都超过 区域高/SplitVerticalRegionMinHeightFraction（否则只是页边空白）、
    且高 >2pt。取面积最大的空白带，沿它把区域裁成上下两块（crop 到内容外接）。
    拆不出或拆出的任一块过小（<MinProposalHeight）返回 None。

    Args:
        proposal_region: 待拆分的区域。
        content: 内容框列表（用于裁剪）。

    Returns:
        若成功拆分，返回 (上块, 下块, 空白带)；否则 None。
    """
    intersects = [c for c in content if c.intersects(proposal_region)]
    empty_blocks = find_empty_horizontal_blocks(proposal_region, intersects)
    fraction = proposal_region.height / SplitVerticalRegionMinHeightFraction
    near_center = [
        e
        for e in empty_blocks
        if (e.y1 - proposal_region.y1) > fraction
        and (proposal_region.y2 - e.y2) > fraction
        and e.height > 2
    ]
    if not near_center:
        return None
    largest = max(near_center, key=lambda b: b.area)
    upper = Box_crop(proposal_region.copy(y2=largest.y1), intersects, -1)
    lower = Box_crop(proposal_region.copy(y1=largest.y2), intersects, -1)
    if (
        upper is not None
        and lower is not None
        and upper.height > MinProposalHeight
        and lower.height > MinProposalHeight
    ):
        return (upper, lower, largest)
    return None


def _split_proposals(proposals: list[Proposal], content: list[Box]) -> list[Proposal]:
    """把相互重叠的 proposal 分组，尝试对「上下叠的一对」做水平拆分（照 splitProposals）。

    先把相互重叠（容差 -2）的 proposal 聚成组（组内成员与组头重叠，组间不再
    重叠）。只处理最保守的「一对」情形：组内恰为一上一下两个方向、且一个区域
    包含另一个时，对两者外接矩形做水平拆分，拆出的上块归上方图注（Down 方向，
    图注在图上方）、下块归下方图注（Up 方向），并各自记下 splitWith（另一半
    图注 + 空白带）供打分。拆不动（没有居中的空白带）就原样保留重叠的组——
    这类配置会被 no-overlap 打分判非法而淘汰。

    Args:
        proposals: 同一配置中的 proposal 列表。
        content: 内容框列表（用于拆分时的裁剪）。

    Returns:
        拆分（或原样）后的 proposal 列表。
    """
    grouped_by_collision: list[list[Proposal]] = []
    proposal_to_check = list(proposals)
    while proposal_to_check:
        head = proposal_to_check[0]
        tail = proposal_to_check[1:]
        collides = []
        rest = []
        for other in tail:
            (collides if other.region.intersects(head.region, -2) else rest).append(other)
        grouped_by_collision.append([head] + collides)
        proposal_to_check = rest

    split_proposals: list[Proposal] = []
    for group in grouped_by_collision:
        if len(group) == 1:
            split_proposals.append(group[0])
        elif (
            len(group) == 2
            and any(p.dir == "Up" for p in group)
            and any(p.dir == "Down" for p in group)
            and (
                group[0].region.contains(group[1].region)
                or group[1].region.contains(group[0].region)
            )
        ):
            region = Box_container([p.region for p in group])
            split_attempt = _split_region_horizontally(region, content)
            if split_attempt is not None:
                upper_split, lower_split, whitespace = split_attempt
                # Up 方向表示图注在区域下方，因此拆分后上块属于 Down 方向的 proposal
                upper_prop = [p for p in group if p.dir == "Down"]
                lower_prop = [p for p in group if p.dir == "Up"]
                split_proposals.append(
                    replace(
                        upper_prop[0],
                        region=upper_split,
                        split_with=(lower_prop[0].caption, whitespace),
                    )
                )
                split_proposals.append(
                    replace(
                        lower_prop[0],
                        region=lower_split,
                        split_with=(upper_prop[0].caption, whitespace),
                    )
                )
            else:
                split_proposals.extend(group)
        else:
            split_proposals.extend(group)
    return split_proposals


def _crop_to_center(caption: Box, proposal: Box, in_center: list[Box], center: float) -> Box:
    """图注不跨栏缝时，把跨栏缝的候选区域裁到栏缝为止（照 cropToCenter）。

    双栏页中图通常只占一栏：若图注本身不跨栏缝、候选区域却跨了，且区域内没有
    跨栏缝的元素，就把区域裁到栏缝一侧（图注在哪侧裁哪侧），避免区域横跨两栏。
    区域含跨栏缝元素时不裁（可能是通栏大图）。

    Args:
        caption: 图注边界。
        proposal: 候选区域边界。
        in_center: 跨越栏缝的元素列表（图形/图内文本）。
        center: 栏缝位置（x 坐标）。

    Returns:
        裁剪后的 proposal 区域（可能不变）。
    """
    caption_crosses = caption.x2 > center and caption.x1 <= center
    proposal_crosses = proposal.x2 > center and proposal.x1 < center
    if proposal_crosses and not caption_crosses:
        if all(not proposal.contains(b) for b in in_center):
            if caption.x1 > center:
                return proposal.copy(x1=center)
            return proposal.copy(x2=center)
    return proposal


def _clip_upward_region(
    caption: Box,
    region: Box,
    graphics: list[Box],
    other_text: list[Paragraph],
) -> Box:
    """向上方向 proposal 的聚类裁剪：把图元素周围的元素聚成一簇，裁掉簇外杂散区域。

    正文分类并不完美，图上方可能残留被误判为正文的段落。做法：区域内的大图形
    （> ClipRegionMinGraphicSize）作种子，反复把紧贴簇上缘的图内文本与图形并入簇
    （距离阈值按文本面积大小区分，图形用更宽的阈值），直到没有新的并入对象；
    最后把簇与区域求交得到裁剪后的区域。簇内没有大图形时（纯文本/小图形）
    无法定位图本体，不做裁剪，原样返回。

    Args:
        caption: 图注边界（未直接使用，但保留以匹配 Scala 接口）。
        region: 初始候选区域（向上扩展后的区域）。
        graphics: 图形区列表。
        other_text: 图内文本段落列表。

    Returns:
        裁剪后的区域（若无可聚类的大图形则返回原 region）。
    """
    contained_graphics = [
        g
        for g in graphics
        if g.area > 0 and region.intersectArea(g) / g.area > 0.95
    ]
    significant = [g for g in contained_graphics if g.area > ClipRegionMinGraphicSize]
    if not significant:
        return region
    cluster = Box_container(significant)
    remaining_graphics = [g for g in contained_graphics if g.area <= ClipRegionMinGraphicSize]
    remaining_other_text = [p for p in other_text if region.intersects(p.boundary)]
    done = False
    # 反复扩展簇：将紧邻簇上缘的图内文本和小图形并入簇
    while not done:
        in_cluster_text = []
        out_cluster_text = []
        for p in remaining_other_text:
            y_dist = cluster.y1 - p.boundary.y2
            threshold = (
                ClipRegionMaxLargeTextDistance
                if p.boundary.area < ClipRegionLargeTextSize
                else ClipRegionMaxTextDistance
            )
            (in_cluster_text if y_dist < threshold else out_cluster_text).append(p)
        in_cluster_graphics = []
        out_cluster_graphics = []
        for g in remaining_graphics:
            y_dist = cluster.y1 - g.y2
            (in_cluster_graphics if y_dist < ClipRegionMaxGraphicDistance else out_cluster_graphics).append(g)
        remaining_graphics = out_cluster_graphics
        remaining_other_text = out_cluster_text
        new_boxes = in_cluster_graphics + [p.boundary for p in in_cluster_text]
        if new_boxes:
            merged = Box_container([cluster] + new_boxes).intersectRegion(region)
            # 并入的框都源自 region 内部，与 region 求交保证非空；防御 None 以免死循环
            if merged is not None:
                cluster = merged
            else:
                done = True
        else:
            done = True
    return cluster


# ---------------------------------------------------------------------------
# proposal 打分与过滤
# ---------------------------------------------------------------------------


def _box_alignment(box1: Box, box2: Box) -> tuple[int, int]:
    """box1 相对 box2 的水平/垂直位置：-1 左/上、0 重叠、1 右/下（照 boxAlignment）。

    Args:
        box1: 第一个框。
        box2: 第二个框。

    Returns:
        (水平关系, 垂直关系) 元组。
    """
    h = -1 if box1.x2 < box2.x1 else (1 if box1.x1 > box2.x2 else 0)
    v = -1 if box1.y2 < box2.y1 else (1 if box1.y1 > box2.y2 else 0)
    return (h, v)


def _box_expand_lr(box: Box, boxes: list[Box], bounds: Box) -> Box:
    """水平方向尽量扩展：不受竖直对齐（v==0）的相邻内容阻挡即扩到 bounds。

    用于上/下方向 proposal：区域上下边界已定，左右要尽量张开到内容外接，
    但被同一竖直带里的左右邻内容挡住时停在它们边缘。

    Args:
        box: 待扩展的框。
        boxes: 阻挡内容列表。
        bounds: 最大扩展边界。

    Returns:
        扩展后的框。
    """
    x1 = bounds.x1
    x2 = bounds.x2
    for other in boxes:
        h, v = _box_alignment(box, other)
        if v == 0:
            if h == 1:
                x1 = max(x1, other.x2)
            elif h == -1:
                x2 = min(x2, other.x1)
    return box.copy(x1=x1, x2=x2)


def _box_expand_ud(box: Box, boxes: list[Box], bounds: Box) -> Box:
    """垂直方向尽量扩展：不受水平对齐（h==0）的相邻内容阻挡即扩到 bounds。

    用于左/右方向 proposal：区域左右边界已定，上下要尽量张开，
    但被同一水平带里的上下邻内容挡住时停在它们边缘。

    Args:
        box: 待扩展的框。
        boxes: 阻挡内容列表。
        bounds: 最大扩展边界。

    Returns:
        扩展后的框。
    """
    y1 = bounds.y1
    y2 = bounds.y2
    for other in boxes:
        h, v = _box_alignment(box, other)
        if h == 0:
            if v == 1:
                y1 = max(y1, other.y2)
            elif v == -1:
                y2 = min(y2, other.y1)
    return box.copy(y1=y1, y2=y2)


def _score_proposal(
    proposal: Proposal,
    graphics: list[Box],
    other_text: list[Box],
    other_proposals: list[Proposal],
    bounds: Box,
) -> float | None:
    """给单个 proposal 打分；与同配置其它 proposal 重叠则判非法返回 None。

    基础分 = 区域面积 / 全页内容外接面积；区域内包含图形/大图形各加分；拆分过的
    proposal 另加空白带面积加权分，并按上下两半是否同类型打折。左/右方向（图注在
    侧面）整体折扣。None 表示该区域与其它候选抢同一块地——配置里出现 None 会被
    计数惩罚，从而偏向「各图注各占一块」的配置。

    Args:
        proposal: 待打分的 proposal。
        graphics: 图形区列表。
        other_text: 图内文本边界列表（未直接使用，保留接口）。
        other_proposals: 同配置中的其他 proposal（用于重叠检测）。
        bounds: 全页内容外接矩形。

    Returns:
        得分（float）或 None（若与其它 proposal 重叠）。
    """
    boundary = proposal.region
    if any(p.region.intersects(boundary, -2) for p in other_proposals):
        return None
    area_score = boundary.area / bounds.area
    if any(boundary.contains(g) for g in graphics):
        area_score += ContainsGraphicBonus
    if any(g.area > LargeGraphicThreshold and boundary.contains(g) for g in graphics):
        area_score += ContainsLargeGraphicBonus
    if proposal.split_with is not None:
        split_with, whitespace = proposal.split_with
        area_score += whitespace.area * SplitWhitespaceBonus / bounds.area
        if split_with.fig_type != proposal.caption.fig_type:
            area_score *= SplitDifferentTypesPenalty
        else:
            area_score *= SplitSameTypesPenalty
    elif proposal.dir in ("Left", "Right"):
        area_score *= LeftRightFigurePenalty
    return area_score


def _in_cut_interval(d: float) -> bool:
    """判断比值是否落在拦腰切图区间内 [0.1, 0.9]。

    Args:
        d: float，相交面积占比（0~1）

    Returns:
        True 表示该比值落在拦腰切图区间 [0.1, 0.9] 内。
    """
    return cutFilterIntervalMin <= d <= cutFilterIntervalMax


def _box_on_boundary(box: Box) -> bool:
    """proposal 是否贴页面左/上边界（x1/y1 ≤ 30）。贴边区域多半是误扩。

    Args:
        box: Box，候选 proposal 区域

    Returns:
        True 表示贴页面左/上边界（多半是误扩）。
    """
    return box.x1 <= boundaryFilterMinDistance or box.y1 <= boundaryFilterMinDistance


def _box_cuts_figure(box: Box, possible_figure_content: list[Box]) -> bool:
    """proposal 是否拦腰切开图形元素（与其相交面积占其自身 10%~90%）。

    完全包含（≈100%）或完全无关（0%）才放行；拦腰切掉一半的区域多半是误把
    图形切分，丢弃。零面积图元素无法算占比，跳过。

    Args:
        box: 候选区域。
        possible_figure_content: 可能的图内容（图形 + 图内文本边界）。

    Returns:
        True 若该区域拦腰切开了某个图形元素。
    """
    for fig in possible_figure_content:
        if fig.area == 0:
            continue
        if _in_cut_interval(fig.intersectArea(box) / fig.area):
            return True
    return False


def _cartesian_product(xss: list[list[Proposal]]) -> list[list[Proposal]]:
    """多组候选的笛卡尔积：每图注各取一个 proposal 的排列组合。

    itertools.product 等价实现，顺序与 Scala 的手写递归一致（第一组为最外层）。
    空输入返回 [[]]。对空列表不做特殊处理——product() 恰好产出单个空元组。

    Args:
        xss: list[list[Proposal]]，各组候选 proposal

    Returns:
        所有组合的列表（每组取一个；空输入返回 [[]]）。
    """
    return [list(combo) for combo in product(*xss)]


# ---------------------------------------------------------------------------
# buildProposals：为每个图注生成一组候选区域
# ---------------------------------------------------------------------------


def _build_proposals(page: PageWithBodyText, layout: DocumentLayout) -> list[list[Proposal]]:
    """为每个图注生成一组候选图区域（照 buildProposals）。

    对每个图注：先算它四周被非图内容挡住的最大扩展边界（x1 左/y1 上/x2 右/y2 下），
    再朝四个方向分别生成候选——有空间（> MinProposalWidth/Height）才生成。
    左/右/下方向的区域直接 crop 收边；上方向多一步 clipUpwardRegion 聚类裁剪
    （上图注常与正文混排，正文分类又可能出错）。最后统一 crop 到内容外接，并
    按尺寸、拦腰切图、贴边界三关过滤。双栏时左右/上下方向还做中心线裁剪。

    Args:
        page: 已分类的页面。
        layout: 文档布局统计。

    Returns:
        列表的列表：外层索引对应 page.captions 中的图注，内层是该图注的 Proposal 列表。
    """
    non_figure_content, possible_figure_content = _content_of_page(page)
    all_content = non_figure_content + possible_figure_content
    bounds = Box_container(all_content)

    # 图内文本的词边界：用来过滤「proposal 边界从词中间穿过」的区域。
    # PDFBox 对词高常严重高估，这里保守地裁到高 5 再参与过滤，宁少勿错。
    other_text_words_bbs = []
    for paragraph in page.other_text:
        for line in paragraph.lines:
            for word in line.words:
                b = word.boundary
                if b.height < 10:
                    other_text_words_bbs.append(b)
                else:
                    other_text_words_bbs.append(b.copy(y1=b.y2 - 5.0))

    two_column = layout.two_columns
    center_column = _find_center_column(page) if two_column else None
    page_center = (center_column[0] + center_column[1]) / 2.0 if center_column else None
    crosses_center = (
        [b for b in possible_figure_content if b.x1 < center_column[0] and b.x2 > center_column[1]]
        if center_column is not None
        else []
    )

    proposals_per_caption: list[list[Proposal]] = []
    for caption in page.captions:
        capt_box = caption.boundary
        # 四周扩展上限：被非图内容挡住（上下/左右邻），初值 = 全页内容外接。
        # y1/y2 语义 = 向上/向下扩展极限（最近的上方内容底缘 / 下方内容顶缘），
        # x1/x2 = 向左/向右扩展极限。与图注同一竖直带/水平带的内容才算阻挡。
        x1, y1, x2, y2 = bounds.x1, bounds.y1, bounds.x2, bounds.y2
        for other in non_figure_content:
            h, v = _box_alignment(capt_box, other)
            if h == 0:
                if v == 1:
                    y1 = max(y1, other.y2)
                elif v == -1:
                    y2 = min(y2, other.y1)
            elif v == 0:
                if h == 1:
                    x1 = max(x1, other.x2)
                elif h == -1:
                    x2 = min(x2, other.x1)

        proposals: list[Proposal] = []
        # 左侧有 ≥MinProposalWidth 空当 → 左方向候选（图注在图右侧）
        if x1 < capt_box.x1 - MinProposalWidth:
            prop = _box_expand_ud(capt_box.copy(x1=x1), non_figure_content, bounds).copy(
                x2=capt_box.x1
            )
            if two_column:
                contains_center_element = any(prop.contains(b, 2) for b in crosses_center)
                if contains_center_element or not (prop.x1 < page_center and prop.x2 > page_center):
                    proposals.insert(0, Proposal(prop, caption, "Left"))
            else:
                proposals.insert(0, Proposal(prop, caption, "Left"))
        # 右侧有 ≥MinProposalWidth 空当 → 右方向候选
        if x2 > capt_box.x2 + MinProposalWidth:
            prop = _box_expand_ud(capt_box.copy(x2=x2), non_figure_content, bounds).copy(
                x1=capt_box.x2
            )
            if two_column:
                contains_center_element = any(prop.contains(b, 2) for b in crosses_center)
                if contains_center_element or not (prop.x1 < page_center and prop.x2 > page_center):
                    proposals.insert(0, Proposal(prop, caption, "Right"))
            else:
                proposals.insert(0, Proposal(prop, caption, "Right"))
        # 上方有 ≥MinProposalHeight 空当 → 上方向候选（图注在图下方，最常见版式）
        if y1 < capt_box.y1 - MinProposalHeight:
            prop = _box_expand_lr(capt_box.copy(y1=y1), non_figure_content, bounds).copy(
                y2=capt_box.y1
            )
            cropped = (
                _crop_to_center(capt_box, prop, crosses_center, page_center)
                if two_column
                else prop
            )
            pruned = _clip_upward_region(capt_box, cropped, page.graphics, page.other_text)
            proposals.insert(0, Proposal(pruned, caption, "Up"))
        # 下方有 ≥MinProposalHeight 空当 → 下方向候选
        if y2 > capt_box.y2 + MinProposalHeight:
            prop = _box_expand_lr(capt_box.copy(y2=y2), non_figure_content, bounds).copy(
                y1=capt_box.y2
            )
            cropped = (
                _crop_to_center(capt_box, prop, crosses_center, page_center)
                if two_column
                else prop
            )
            proposals.insert(0, Proposal(cropped, caption, "Down"))

        # 统一收边：crop 到内容外接（-1 容差使共享边界的内容不算相交），区域过小
        # 丢弃；任何图内词边界从区域探出（partial 相交）也丢弃。
        pruned_proposals: list[Proposal] = []
        for prop in proposals:
            cropped = Box_crop(prop.region, all_content, -1)
            if (
                cropped is not None
                and cropped.width > MinProposalWidth
                and cropped.height > MinProposalHeight
            ):
                partially_intersects_word = any(
                    b.intersects(cropped, -2) and not cropped.contains(b, 1)
                    for b in other_text_words_bbs
                )
                if not partially_intersects_word:
                    pruned_proposals.append(replace(prop, region=cropped))
        # 拦腰切图 / 贴页面边界的 proposal 丢弃（顺序照 Scala）
        proposals_per_caption.append(
            [
                p
                for p in pruned_proposals
                if not _box_cuts_figure(p.region, possible_figure_content)
                and not _box_on_boundary(p.region)
            ]
        )
    return proposals_per_caption


# ---------------------------------------------------------------------------
# 主入口：located_figures
# ---------------------------------------------------------------------------


def _remove_text_in_regions(paragraphs: list[Paragraph], regions: list[Box]) -> list[Paragraph]:
    """剔除被图区域整体包含的行（该行属于图内容，不应留在非图文本里）。

    一行被任一图区域包含即从段落中移除；段落所有行都被移除则该段整体丢弃。
    剩余行重新合成段落，边界按剩余行重算（对应 Scala 的 Paragraph.apply）。

    Args:
        paragraphs: 待清理的段落列表（通常为 body_text + other_text）。
        regions: 已确定的图区域列表。

    Returns:
        清理后的段落列表（仅含不在图区域内的行）。
    """
    if not regions:
        return list(paragraphs)
    cleaned = []
    for paragraph in paragraphs:
        filtered_lines = [
            line
            for line in paragraph.lines
            if not any(region.contains(line.boundary) for region in regions)
        ]
        if filtered_lines:
            cleaned.append(
                Paragraph(filtered_lines, Box_container([l.boundary for l in filtered_lines]))
            )
    return cleaned


def located_figures(
    page: PageWithBodyText, layout: DocumentLayout | None
) -> PageWithFigures:
    """为每页图注找到图区域，产出 PageWithFigures（照 locatedFigures）。

    Args:
        page: 已分类的一页（正文/图内文本/图注/图形区）。
        layout: 文档级布局统计；None（信息不足）时防御性返回空图 + 全部文本归非图。

    Returns:
        该页图检测结果：figures 是每张图的占位 dict，failed_captions 是配不到图
        的图注。

    算法：
        1. 若 layout 为 None，直接返回空结果（无图检测）。
        2. 调用 _build_proposals 为每个图注生成候选区域列表。
        3. 筛选出有候选的图注，计算所有配置的笛卡尔积数量；若超过 50 万则放弃枚举。
        4. 枚举每个配置：
           a. 对配置中的 proposals 调用 _split_proposals 尝试拆分重叠项。
           b. 对拆分后的每个 proposal 调用 _score_proposal 打分（若与同配置其他 proposal 重叠则返回 None）。
           c. 计算总分为有效得分之和减去非法项数量（惩罚）。
        5. 选择总分最高的配置，其中得分为 None 的 proposal 视为失败图注。
        6. 从成功 proposal 中构建 figures 列表，失败图注归入 failed_captions。
        7. 清理非图文本：移除被图区域包含的行，加上失败图注段落，按阅读序排序。
    """
    # layout 信息不足：无从判断双栏/中心线，也缺少可靠边界，直接放弃检测
    if layout is None:
        failed = [Caption.from_paragraph(c) for c in page.captions]
        non_figure_text = _sorted_paragraphs(
            page.body_text + page.other_text + [c.paragraph for c in page.captions]
        )
        return PageWithFigures(page.page_number, non_figure_text, page.classified_text, [], failed)

    proposals = _build_proposals(page, layout)
    proposals_with_captions = list(zip(page.captions, proposals))

    _, possible_figure_content = _content_of_page(page)
    all_content = possible_figure_content + [
        p.boundary for p in page.body_text
    ] + [c.boundary for c in page.captions] + page.non_figure_graphics
    bounds = Box_container(all_content)

    captions_with_no_proposals = [c for c, ps in proposals_with_captions if not ps]
    valid_proposals = [ps for _, ps in proposals_with_captions if ps]
    configuration_count = 1
    for ps in valid_proposals:
        configuration_count *= len(ps)
    # 有些论文配置数可达数十亿，来不及逐条打分，超过 50 万就放弃枚举：
    # 把全部图注归为失败、文本全归非图（照 locatedFigures 的 give up 分支）
    if not valid_proposals or configuration_count > 500000:
        non_figure_text = _sorted_paragraphs(
            page.other_text
            + page.body_text
            + [c.paragraph for c in captions_with_no_proposals]
        )
        return PageWithFigures(
            page.page_number,
            non_figure_text,
            page.classified_text,
            [],
            [Caption.from_paragraph(c) for c in captions_with_no_proposals],
        )

    # 枚举每图注各取一个 proposal 的所有配置，取总分最高者。每配置内先 splitProposals
    # 拆分重叠项，再逐项打分（打分时与同配置其它 proposal 判重叠），总分减去非法
    # 项个数作为惩罚。max 取首个最高分（与 Scala 的 maxBy 一致）。
    best_pairs: list[tuple[Proposal, float | None]] = []
    best_score = float("-inf")
    for proposals_to_use in _cartesian_product(valid_proposals):
        props = list(_split_proposals(proposals_to_use, all_content))
        scored: list[Proposal] = []
        scores: list[float | None] = []
        while props:
            prop = props[0]
            props = props[1:]
            score = _score_proposal(
                prop,
                page.graphics,
                [p.boundary for p in page.other_text],
                scored + props,
                bounds,
            )
            scored.insert(0, prop)
            scores.insert(0, score)
        overall_score = sum(s for s in scores if s is not None) - scores.count(None)
        if overall_score > best_score:
            best_score = overall_score
            best_pairs = list(zip(scored, scores))[::-1]

    good_pairs = [(p, s) for p, s in best_pairs if s is not None]
    bad_pairs = [(p, s) for p, s in best_pairs if s is None]

    figures: list[dict] = []
    for proposal, _ in good_pairs:
        # 图内文本 = 落在图区域内的图内文本词（容差 1，照 locatedFigures 的 imageText）
        image_text = [
            word.text
            for p in page.other_text
            for line in p.lines
            for word in line.words
            if proposal.region.contains(word.boundary, 1)
        ]
        caption = proposal.caption
        figures.append(
            {
                "name": caption.name,
                "fig_type": caption.fig_type,
                "page": caption.page,
                "caption_text": caption.text,
                "image_text": image_text,
                "caption_boundary": caption.boundary,
                "region_boundary": proposal.region,
            }
        )
    failed_captions = [p.caption for p, _ in bad_pairs] + captions_with_no_proposals
    # 非图文本 = 去掉被图区域吞掉的文本行 + 失败图注段落，按阅读序排序
    non_figure_text = _remove_text_in_regions(
        page.other_text + page.body_text,
        [f["region_boundary"] for f in figures],
    ) + [c.paragraph for c in failed_captions]
    return PageWithFigures(
        page.page_number,
        _sorted_paragraphs(non_figure_text),
        page.classified_text,
        figures,
        [Caption.from_paragraph(c) for c in failed_captions],
    )