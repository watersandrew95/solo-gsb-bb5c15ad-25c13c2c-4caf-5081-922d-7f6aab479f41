"""直流配电树压降校核核心算法。

计算流程：
1. 校验输入构成以 ``source`` 为根的连通树；
2. 自叶向根累计每段承载的全部下游电流；
3. 自根向叶计算各段压降与各节点电压；
4. 汇总超额定电流线段与欠压节点。

``capacity`` 在基线校核通过的前提下，评估目标节点可追加的最大恒定负载电流：
目标到 source 路径上的线段电流同步增加，任一节点的新增压降等于它与目标节点
两条根路径的公共线段电阻之和乘新增电流（兄弟支路经公共上游线段同样受影响）。

``unregistered_load`` 假定恰有一个非 source 节点存在未登记的正恒定负载，
将各测点实测电流与未舍入基线线段电流比较：显著正差（> 1e-6 A）须在 1e-6 A
内相互一致，据此估计负载电流，并按测点是否位于候选节点的祖先路径筛选相容节点。

``peak_risk`` 在至多 K 条负载同时升至峰值（peak_current，缺省等于 current）的
约束下，对每条线段 / 每个节点独立选取使其最坏化的峰值负载组合（峰值增量贡献
降序，贡献相同按负载输入序号升序，零贡献不计入），给出各线段最大承载电流、
各节点最低电压及相应的越限列表；越限判定与 ``evaluate`` 相同。

``outage_restoration`` 在故障线段断开后评估各候选跨接线：断开故障线段，树分裂为
source 所在分量（保持供电）与故障线段子节点子树构成的失电分量；候选跨接线须从
source 所在分量接入失电分量任意节点，失电分量从接入点重新定向（接入点到原子树
根路径上的线段方向翻转），再重算全树线段电流与节点电压。无越限的候选为可恢复，
选中可恢复候选中最低节点电压余量（min(节点电压 - 生效阈值)）最大者，平局按输入
顺序；越限判定与 ``evaluate`` 相同。
``load_shedding`` 接收按 ``loads`` 从 0 开始的输入序号指定的可切除负载列表，
枚举其任意切除组合（每条负载只能整条切除，其余保持供电），重算线段电流与节点
电压（等价于在移除被切除负载后的输入上执行 ``evaluate``），选出无超额定且无欠压、
被切除电流总和最小的方案；切除电流总和并列时依次取切除条数最少、升序序号列表
字典序最小者。原网已安全时选空集；全部可切除负载均切除仍越限时为无解。

``protection_selectivity`` 为每条线段配置一条分段线性保护曲线（曲线点按电流严格
递增，故障电流低于首点不动作、点间线性插值、超过末点沿用末点动作时间），对每个
故障沿故障节点至 source 的路径计算各级装置动作时间：距故障最近的装置必须动作，
所有会动作的上级装置须比它至少晚最小级差，违反者记为失配线段；并用基线线段电流
标出正常负荷下即会动作（可能误动）的装置。返回逐故障动作顺序、失配线段与总体结论。

``startup_order`` 在基线校核通过的前提下，为 1～10 条待投运负载（节点、稳态电流、
启动电流，启动电流须不小于稳态电流）搜索完整可行的投运顺序：每步仅本步新投运负载
按启动电流计入、此前投运负载按稳态电流计入，未投运负载不计入；用未舍入的线段电流
与节点电压按与 ``evaluate`` 相同的规则判定超额定与欠压（恰好触限允许）。可行性对
已投运集合具有单调性，按输入序号递增做子集状态深度优先搜索并记忆不可行集合，首个
完整顺序即按输入序号字典序最小者。

``loss_allocation`` 按未舍入的基线线段电流计算每段线损 P=I²R 与全树总线损（各段
线损之和），并按 ``loads`` 输入序号逐负载归属：负载 i 在其到 source 路径的每段上
承担 负载电流 × 该段总电流 × 该段电阻——同一段上全部下游负载的归属之和恰等于该段
线损，故逐负载归属之和与总线损一致（展示舍入差除外）。source 节点负载路径为空、
归属为零；同节点多条负载分别列出。计算与汇总使用未舍入值，展示值按 6 位小数舍入。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR
from typing import Optional, Sequence

ROOT_NODE = "source"
EPS = 1e-9
ROUND_DIGITS = 6
MEASURE_TOL = 1e-6  # 实测差值容差 (A)：|实测 - 基线| ≤ 1e-6 视为无差异


class InputValidationError(ValueError):
    """输入未通过业务校验（无效树、负载引用未知节点等）。"""

    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class BaselineViolationError(ValueError):
    """基线校核已存在超额定电流或欠压，无法评估可增容量。"""

    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass
class Segment:
    id: str
    parent: str
    child: str
    resistance: float
    rated_current: float


@dataclass
class _Tree:
    """校验后的配电树与基线潮流（电流、电压均为未舍入值）。"""

    segments: list[Segment]
    parent_of: dict[str, str]
    children_map: dict[str, list[Segment]]
    order: list[str]  # 自根向叶 BFS 顺序
    load_map: dict[str, float]
    min_voltage_override: dict[str, float]
    node_total: dict[str, float]
    seg_current: dict[str, float]
    node_voltage: dict[str, float]


def _round(value: float) -> float:
    return round(value, ROUND_DIGITS)


def _truncate(value: float) -> float:
    """向下截断至 6 位小数（先按 1e-9 容差吸附浮点噪声）。"""
    snapped = Decimal(repr(round(value, ROUND_DIGITS + 3)))
    return float(snapped.quantize(Decimal("0.000001"), rounding=ROUND_FLOOR))


def _validate_and_build_tree(segments_in: Sequence) -> tuple[list[Segment], dict[str, str]]:
    """校验树结构，返回线段列表与 child -> parent 映射。

    校验规则：
    - 线段 id 唯一（缺省自动生成 seg-<序号>）；
    - 不存在自环（父节点与子节点相同）；
    - 每个节点至多有一个父节点；
    - 根节点 source 不能作为子节点；
    - 所有父节点必须是 source 或某个子节点（连通性）；
    - 不存在环路。
    """
    segments: list[Segment] = []
    for index, s in enumerate(segments_in):
        seg_id = s.id or f"seg-{index + 1}"
        segments.append(
            Segment(
                id=seg_id,
                parent=s.parent,
                child=s.child,
                resistance=s.resistance,
                rated_current=s.rated_current,
            )
        )

    errors: list[str] = []

    seen_ids: set[str] = set()
    for seg in segments:
        if seg.id in seen_ids:
            errors.append(f"线段 id 重复: '{seg.id}'")
        seen_ids.add(seg.id)

    for seg in segments:
        if seg.parent == seg.child:
            errors.append(f"线段 '{seg.id}' 的父节点与子节点相同（'{seg.parent}'），存在自环")

    parent_of: dict[str, str] = {}
    for seg in segments:
        if seg.child in parent_of:
            errors.append(
                f"节点 '{seg.child}' 存在多个父节点（'{parent_of[seg.child]}' 与 '{seg.parent}'）"
            )
        else:
            parent_of[seg.child] = seg.parent

    if ROOT_NODE in parent_of:
        errors.append(f"根节点 '{ROOT_NODE}' 不能作为子节点")

    child_nodes = set(parent_of)
    for seg in segments:
        if seg.parent != ROOT_NODE and seg.parent not in child_nodes:
            errors.append(f"线段 '{seg.id}' 的父节点 '{seg.parent}' 未在树中定义，树不连通")

    if errors:
        raise InputValidationError(f"输入未构成以 '{ROOT_NODE}' 为根的连通树", errors)

    # 环路检测：沿父指针上溯必须到达根
    for seg in segments:
        visited: set[str] = set()
        node = seg.child
        while node != ROOT_NODE:
            if node in visited:
                raise InputValidationError(
                    f"输入未构成以 '{ROOT_NODE}' 为根的连通树",
                    [f"检测到环路，涉及节点 '{node}'"],
                )
            visited.add(node)
            node = parent_of[node]

    return segments, parent_of


def _aggregate_loads(loads_in: Sequence, known_nodes: set[str]) -> tuple[dict[str, float], dict[str, float]]:
    """汇总各节点负载电流（同节点多条负载累加）及节点级最低允许电压。"""
    errors: list[str] = []
    load_map: dict[str, float] = {}
    min_voltage_map: dict[str, float] = {}

    for load in loads_in:
        if load.node not in known_nodes:
            errors.append(f"负载节点 '{load.node}' 不存在于树中")
            continue
        load_map[load.node] = load_map.get(load.node, 0.0) + load.current
        if load.min_voltage is not None:
            previous = min_voltage_map.get(load.node)
            if previous is not None and abs(previous - load.min_voltage) > EPS:
                errors.append(f"节点 '{load.node}' 的最低允许电压被重复定义且不一致")
            else:
                min_voltage_map[load.node] = load.min_voltage

    if errors:
        raise InputValidationError("负载定义无效", errors)

    return load_map, min_voltage_map


def _prepare_tree(source_voltage: float, segments_in: Sequence, loads_in: Sequence) -> _Tree:
    """校验输入并计算基线潮流（各段电流、各节点电压，未舍入）。"""
    segments, parent_of = _validate_and_build_tree(segments_in)

    children_map: dict[str, list[Segment]] = {}
    for seg in segments:
        children_map.setdefault(seg.parent, []).append(seg)

    nodes = set(parent_of) | {ROOT_NODE}
    load_map, min_voltage_override = _aggregate_loads(loads_in, nodes)

    # 自根向叶 BFS 排序（父节点一定先于子节点出现）
    order = [ROOT_NODE]
    queue: deque[str] = deque([ROOT_NODE])
    while queue:
        node = queue.popleft()
        for seg in children_map.get(node, []):
            order.append(seg.child)
            queue.append(seg.child)

    # 自叶向根累计每段承载的全部下游电流
    node_total = {node: load_map.get(node, 0.0) for node in nodes}
    seg_current: dict[str, float] = {}
    for node in reversed(order):
        for seg in children_map.get(node, []):
            seg_current[seg.id] = node_total[seg.child]
            node_total[node] += node_total[seg.child]

    # 自根向叶计算各节点电压
    node_voltage = {ROOT_NODE: source_voltage}
    for node in order:
        for seg in children_map.get(node, []):
            node_voltage[seg.child] = node_voltage[node] - seg_current[seg.id] * seg.resistance

    return _Tree(
        segments=segments,
        parent_of=parent_of,
        children_map=children_map,
        order=order,
        load_map=load_map,
        min_voltage_override=min_voltage_override,
        node_total=node_total,
        seg_current=seg_current,
        node_voltage=node_voltage,
    )


def _node_threshold(tree: _Tree, node: str, min_voltage: float) -> float:
    """节点生效的最低允许电压（节点级覆盖优先于全局值）。"""
    return tree.min_voltage_override.get(node, min_voltage)


def _find_violations(tree: _Tree, min_voltage: float) -> tuple[list[dict], list[dict]]:
    """按 EPS 容差判定超额定电流线段与欠压节点（恰好触限不算越限）。"""
    over_current_segments = []
    for seg in tree.segments:  # 保持输入顺序
        current = tree.seg_current[seg.id]
        if current > seg.rated_current + EPS:
            over_current_segments.append(
                {
                    "segment_id": seg.id,
                    "parent": seg.parent,
                    "child": seg.child,
                    "current": _round(current),
                    "rated_current": seg.rated_current,
                    "excess": _round(current - seg.rated_current),
                }
            )

    under_voltage_nodes = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        voltage = tree.node_voltage[node]
        if voltage < threshold - EPS:
            under_voltage_nodes.append(
                {
                    "node": node,
                    "voltage": _round(voltage),
                    "min_voltage": threshold,
                    "deficit": _round(threshold - voltage),
                }
            )

    return over_current_segments, under_voltage_nodes


def _tree_results(tree: _Tree, min_voltage: float) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """生成线段 / 节点明细与越限列表（evaluate 风格，展示值按 6 位小数舍入）。"""
    over_current_segments, under_voltage_nodes = _find_violations(tree, min_voltage)

    segment_results = []
    for seg in tree.segments:  # 保持输入顺序
        current = tree.seg_current[seg.id]
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(current),
                "rated_current": seg.rated_current,
                "resistance": seg.resistance,
                "voltage_drop": _round(current * seg.resistance),
                "over_rated": current > seg.rated_current + EPS,
            }
        )

    node_results = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        voltage = tree.node_voltage[node]
        node_results.append(
            {
                "node": node,
                "voltage": _round(voltage),
                "min_voltage": threshold,
                "load_current": _round(tree.load_map.get(node, 0.0)),
                "under_voltage": voltage < threshold - EPS,
            }
        )

    return segment_results, node_results, over_current_segments, under_voltage_nodes


def evaluate(source_voltage: float, min_voltage: float, segments_in: Sequence, loads_in: Sequence) -> dict:
    """执行压降校核，返回可 JSON 序列化的结果字典。"""
    tree = _prepare_tree(source_voltage, segments_in, loads_in)
    segment_results, node_results, over_current_segments, under_voltage_nodes = _tree_results(
        tree, min_voltage
    )

    return {
        "ok": not over_current_segments and not under_voltage_nodes,
        "source_voltage": source_voltage,
        "min_voltage": min_voltage,
        "total_source_current": _round(tree.node_total[ROOT_NODE]),
        "segments": segment_results,
        "nodes": node_results,
        "over_current_segments": over_current_segments,
        "under_voltage_nodes": under_voltage_nodes,
    }


def capacity(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    target_node: str,
) -> dict:
    """评估目标节点可追加的最大恒定负载电流。

    新增电流 ΔI 仅改变目标到 source 路径上的线段电流；任一节点 n 的新增压降
    = R_common(n, target) × ΔI，R_common 为两条根路径公共线段的电阻之和
    （兄弟支路经公共上游线段同样受压）。最大 ΔI 受限于：
    - 路径上每段的额定电流余量；
    - 每个 R_common > 0 节点的欠压余量（零公共电阻节点不受影响）。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    if target_node == ROOT_NODE:
        raise InputValidationError("目标节点无效", [f"目标节点不能为根节点 '{ROOT_NODE}'"])
    if target_node not in tree.parent_of:
        raise InputValidationError("目标节点无效", [f"目标节点 '{target_node}' 不存在于树中"])

    over_current_segments, under_voltage_nodes = _find_violations(tree, min_voltage)
    if over_current_segments or under_voltage_nodes:
        details = [
            f"线段 '{v['segment_id']}' 已超额定电流：{v['current']} A（额定 {v['rated_current']} A）"
            for v in over_current_segments
        ]
        details += [
            f"节点 '{v['node']}' 已欠压：{v['voltage']} V（最低允许 {v['min_voltage']} V）"
            for v in under_voltage_nodes
        ]
        raise BaselineViolationError("基线校核已存在超额定电流或欠压，无法评估可增容量", details)

    # 目标到 source 路径上的线段
    seg_by_child = {seg.child: seg for seg in tree.segments}
    path_seg_ids: set[str] = set()
    node = target_node
    while node != ROOT_NODE:
        seg = seg_by_child[node]
        path_seg_ids.add(seg.id)
        node = seg.parent

    # 各节点与目标节点两条根路径的公共线段电阻之和（自根向叶累计）
    common_resistance = {ROOT_NODE: 0.0}
    for node in tree.order:
        for seg in tree.children_map.get(node, []):
            shared = seg.resistance if seg.id in path_seg_ids else 0.0
            common_resistance[seg.child] = common_resistance[node] + shared

    # 候选上限：(可追加电流, 限制类型, 标识)
    limits: list[tuple[float, str, str]] = []
    for seg in tree.segments:  # 保持输入顺序
        if seg.id not in path_seg_ids:
            continue
        limits.append((seg.rated_current - tree.seg_current[seg.id], "segment", seg.id))
    for node in tree.order:  # BFS 顺序，根在前
        r_common = common_resistance[node]
        if r_common <= 0.0:
            continue  # 零公共电阻：该节点电压不受新增负载影响
        headroom = tree.node_voltage[node] - _node_threshold(tree, node, min_voltage)
        limits.append((headroom / r_common, "node", node))

    raw_max = min(limit for limit, _, _ in limits)
    limiting_factors = [
        {"type": kind, "id": ident}
        for limit, kind, ident in limits
        if limit <= raw_max + EPS
    ]

    return {
        "target_node": target_node,
        "max_additional_current": _truncate(max(0.0, raw_max)),
        "limiting_factors": limiting_factors,
    }


def unregistered_load(
    source_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    measurements_in: Sequence,
) -> dict:
    """定位唯一未登记正恒定负载，返回可 JSON 序列化的结果字典。

    假定恰有一个非 source 节点存在未登记的正恒定负载（测点可不覆盖全树）。
    对每个测点计算 差值 = 实测电流 - 未舍入基线线段电流：

    - 差值 < -1e-6 A（实测低于基线），或显著正差（> 1e-6 A）相互差异超过
      1e-6 A → ``inconsistent``；
    - 无显著正差 → ``undetermined``；
    - 否则负载电流取显著正差的均值（按 6 位小数返回），并筛选候选节点：
      显著正差测点须位于候选节点的祖先路径上，零差测点须不在其祖先路径上；
      未测支路允许多解，无相容节点 → ``inconsistent``。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    # 测点校验：线段 id 唯一且存在于树中
    known_seg_ids = {seg.id for seg in tree.segments}
    errors: list[str] = []
    seen_ids: set[str] = set()
    for m in measurements_in:
        if m.segment_id in seen_ids:
            errors.append(f"测量线段 id 重复: '{m.segment_id}'")
        seen_ids.add(m.segment_id)
        if m.segment_id not in known_seg_ids:
            errors.append(f"测量线段 '{m.segment_id}' 不存在于树中")
    if errors:
        raise InputValidationError("实测电流定义无效", errors)

    # 与未舍入基线比较（差值 = 实测 - 基线）
    diffs: list[tuple[str, float]] = []
    measurement_results = []
    for m in measurements_in:
        baseline = tree.seg_current[m.segment_id]
        diff = m.current - baseline
        diffs.append((m.segment_id, diff))
        measurement_results.append(
            {
                "segment_id": m.segment_id,
                "baseline_current": _round(baseline),
                "measured_current": m.current,
                "difference": _round(diff),
            }
        )

    def _result(status: str, load_current: Optional[float], candidates: list[str], reasons: list[str]) -> dict:
        return {
            "status": status,
            "load_current": load_current,
            "candidate_nodes": candidates,
            "measurements": measurement_results,
            "reasons": reasons,
        }

    # 负差：实测低于基线，与“仅新增正负载”的前提矛盾
    negative = [(seg_id, diff) for seg_id, diff in diffs if diff < -MEASURE_TOL]
    if negative:
        reasons = [
            f"线段 '{seg_id}' 实测电流低于基线电流（差值 {_round(diff)} A）"
            for seg_id, diff in negative
        ]
        return _result("inconsistent", None, [], reasons)

    positives = [(seg_id, diff) for seg_id, diff in diffs if diff > MEASURE_TOL]
    if not positives:
        return _result("undetermined", None, [], [])

    # 显著正差须在 1e-6 A 内相互一致
    spread = max(diff for _, diff in positives) - min(diff for _, diff in positives)
    if spread > MEASURE_TOL:
        detail = "、".join(f"线段 '{seg_id}' 为 {_round(diff)} A" for seg_id, diff in positives)
        return _result("inconsistent", None, [], [f"显著正差相互矛盾（{detail}），差异超过 1e-6 A"])

    load_current = sum(diff for _, diff in positives) / len(positives)

    # 候选筛选：显著正差测点须在候选节点祖先路径上，零差测点须不在
    seg_by_child = {seg.child: seg for seg in tree.segments}
    positive_ids = {seg_id for seg_id, _ in positives}
    zero_ids = {seg_id for seg_id, diff in diffs if abs(diff) <= MEASURE_TOL}

    candidates: list[str] = []
    for node in tree.order:  # BFS 顺序，根在前
        if node == ROOT_NODE:
            continue
        path_ids: set[str] = set()
        current = node
        while current != ROOT_NODE:
            seg = seg_by_child[current]
            path_ids.add(seg.id)
            current = seg.parent
        if positive_ids <= path_ids and zero_ids.isdisjoint(path_ids):
            candidates.append(node)

    if not candidates:
        return _result(
            "inconsistent",
            None,
            [],
            ["测点位置相互矛盾：不存在使显著正差测点位于祖先路径且零差测点不在祖先路径的节点"],
        )

    return _result("located", _round(load_current), candidates, [])


def peak_risk(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    max_simultaneous_peaks: int,
) -> dict:
    """评估至多 K 条负载同时升至峰值时的最坏越限风险，返回可 JSON 序列化的结果字典。

    每条负载的峰值增量 ΔI_i = peak_current - current（缺省 peak_current = current，
    增量为零）；同节点多条负载分别计数。对每条线段 / 每个节点独立选取使其最坏化的
    至多 K 条峰值负载（贡献降序，贡献相同按负载输入序号升序，零贡献不计入），
    不同约束可对应不同峰值组合：

    - 线段 s：负载 i 位于 s 下游（s 在其根路径上）时贡献 ΔI_i，
      最大承载电流 = 基线电流 + 所选 ΔI_i 之和；
    - 节点 n：负载 i 贡献 ΔI_i × R_common(n, node_i)，R_common 为两条根路径
      公共线段电阻之和，最低电压 = 基线电压 - 所选贡献之和。

    越限判定沿用 ``evaluate`` 的额定电流、节点最低允许电压与 EPS 容差规则。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    deltas: list[float] = []
    for load in loads_in:
        peak = load.current if load.peak_current is None else load.peak_current
        deltas.append(peak - load.current)

    k = max(0, max_simultaneous_peaks)

    seg_by_child = {seg.child: seg for seg in tree.segments}

    # 每条负载的根路径线段集合，以及各节点与该负载节点两条根路径的公共电阻
    load_paths: list[set[str]] = []
    load_common_resistance: list[dict[str, float]] = []
    for load in loads_in:
        path_ids: set[str] = set()
        node = load.node
        while node != ROOT_NODE:
            seg = seg_by_child[node]
            path_ids.add(seg.id)
            node = seg.parent
        load_paths.append(path_ids)

        common = {ROOT_NODE: 0.0}
        for parent in tree.order:
            for seg in tree.children_map.get(parent, []):
                shared = seg.resistance if seg.id in path_ids else 0.0
                common[seg.child] = common[parent] + shared
        load_common_resistance.append(common)

    def _top_k(contribs: list[tuple[float, int]]) -> tuple[list[int], float]:
        """取贡献为正的至多 K 条（贡献降序、序号升序），返回升序序号列表与贡献和。"""
        positive = [(c, i) for c, i in contribs if c > 0.0]
        positive.sort(key=lambda item: (-item[0], item[1]))
        chosen = positive[:k]
        return sorted(i for _, i in chosen), sum(c for c, _ in chosen)

    segment_results = []
    over_current_segments = []
    for seg in tree.segments:  # 保持输入顺序
        contribs = [(deltas[i], i) for i, path in enumerate(load_paths) if seg.id in path]
        peak_loads, extra = _top_k(contribs)
        worst = tree.seg_current[seg.id] + extra
        over = worst > seg.rated_current + EPS
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(worst),
                "rated_current": seg.rated_current,
                "peak_loads": peak_loads,
                "over_rated": over,
            }
        )
        if over:
            over_current_segments.append(
                {
                    "segment_id": seg.id,
                    "parent": seg.parent,
                    "child": seg.child,
                    "current": _round(worst),
                    "rated_current": seg.rated_current,
                    "excess": _round(worst - seg.rated_current),
                    "peak_loads": peak_loads,
                }
            )

    node_results = []
    under_voltage_nodes = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        contribs = [
            (deltas[i] * load_common_resistance[i][node], i)
            for i in range(len(deltas))
        ]
        peak_loads, drop = _top_k(contribs)
        worst = tree.node_voltage[node] - drop
        under = worst < threshold - EPS
        node_results.append(
            {
                "node": node,
                "voltage": _round(worst),
                "min_voltage": threshold,
                "peak_loads": peak_loads,
                "under_voltage": under,
            }
        )
        if under:
            under_voltage_nodes.append(
                {
                    "node": node,
                    "voltage": _round(worst),
                    "min_voltage": threshold,
                    "deficit": _round(threshold - worst),
                    "peak_loads": peak_loads,
                }
            )

    return {
        "ok": not over_current_segments and not under_voltage_nodes,
        "max_simultaneous_peaks": k,
        "segments": segment_results,
        "nodes": node_results,
        "over_current_segments": over_current_segments,
        "under_voltage_nodes": under_voltage_nodes,
    }


def outage_restoration(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    faulty_segment_id: str,
    jumpers_in: Sequence,
) -> dict:
    """断开故障线段后逐候选评估跨接线恢复方案，返回可 JSON 序列化的结果字典。

    断开故障线段后，树分裂为 source 所在分量（保持供电）与故障线段子节点子树
    构成的失电分量。每条候选跨接线从供电侧节点（须在 source 所在分量）引向
    失电侧节点（可为失电分量任意节点）；失电分量从接入点重新定向——接入点到
    原子树根（故障线段子节点）路径上的线段方向翻转，其余线段方向不变——再在
    恢复后的拓扑上重算全树线段电流与节点电压（故障线段退出运行不计入，跨接线
    作为新线段计入）。

    越限判定沿用 ``evaluate`` 的额定电流、节点最低允许电压（含节点级覆盖）与
    EPS 容差规则；无越限的候选为可恢复。从可恢复候选中选中最低节点电压余量
    （min(节点电压 - 生效阈值)，按未舍入值比较，EPS 内视为平局）最大者，平局
    按输入顺序；无可恢复方案时选中项为 None。
    """
    base = _prepare_tree(source_voltage, segments_in, loads_in)

    faulty = next((seg for seg in base.segments if seg.id == faulty_segment_id), None)
    if faulty is None:
        raise InputValidationError("故障线段无效", [f"故障线段 '{faulty_segment_id}' 不存在于树中"])

    # 失电分量：故障线段子节点的子树（断开故障线段后与 source 断开的一侧）
    lost_nodes: set[str] = set()
    stack = [faulty.child]
    while stack:
        node = stack.pop()
        lost_nodes.add(node)
        stack.extend(seg.child for seg in base.children_map.get(node, []))

    # 跨接线校验：id 唯一且不与已有线段重复；供电侧 / 失电侧节点须分别位于两个分量
    all_nodes = set(base.parent_of) | {ROOT_NODE}
    existing_ids = {seg.id for seg in base.segments}
    errors: list[str] = []
    seen_jumper_ids: set[str] = set()
    for j in jumpers_in:
        if j.id in seen_jumper_ids:
            errors.append(f"跨接线 id 重复: '{j.id}'")
        seen_jumper_ids.add(j.id)
        if j.id in existing_ids:
            errors.append(f"跨接线 id '{j.id}' 与已有线段 id 重复")
        if j.supply_node not in all_nodes:
            errors.append(f"跨接线 '{j.id}' 的供电侧节点 '{j.supply_node}' 不存在于树中")
        elif j.supply_node in lost_nodes:
            errors.append(
                f"跨接线 '{j.id}' 的供电侧节点 '{j.supply_node}' 位于失电分量，"
                f"须位于 '{ROOT_NODE}' 所在分量"
            )
        if j.outage_node not in all_nodes:
            errors.append(f"跨接线 '{j.id}' 的失电侧节点 '{j.outage_node}' 不存在于树中")
        elif j.outage_node not in lost_nodes:
            errors.append(
                f"跨接线 '{j.id}' 的失电侧节点 '{j.outage_node}' 位于 '{ROOT_NODE}' 所在分量，"
                "须位于失电分量"
            )
    if errors:
        raise InputValidationError("跨接线定义无效", errors)

    seg_by_child = {seg.child: seg for seg in base.segments}

    candidates = []
    margins: list[float] = []
    for j in jumpers_in:
        # 失电分量从接入点重新定向：接入点到原子树根路径上的线段方向翻转
        flip_seg_ids: set[str] = set()
        node = j.outage_node
        while node != faulty.child:
            seg = seg_by_child[node]
            flip_seg_ids.add(seg.id)
            node = seg.parent

        restored: list[Segment] = []
        for seg in base.segments:
            if seg.id == faulty_segment_id:
                continue  # 故障线段断开，退出运行
            if seg.id in flip_seg_ids:
                restored.append(
                    Segment(
                        id=seg.id,
                        parent=seg.child,
                        child=seg.parent,
                        resistance=seg.resistance,
                        rated_current=seg.rated_current,
                    )
                )
            else:
                restored.append(seg)
        restored.append(
            Segment(
                id=j.id,
                parent=j.supply_node,
                child=j.outage_node,
                resistance=j.resistance,
                rated_current=j.rated_current,
            )
        )

        tree = _prepare_tree(source_voltage, restored, loads_in)
        segment_results, node_results, over_current_segments, under_voltage_nodes = _tree_results(
            tree, min_voltage
        )

        margin = min(
            tree.node_voltage[node] - _node_threshold(tree, node, min_voltage)
            for node in tree.order
        )
        margins.append(margin)
        candidates.append(
            {
                "jumper_id": j.id,
                "supply_node": j.supply_node,
                "outage_node": j.outage_node,
                "restorable": not over_current_segments and not under_voltage_nodes,
                "min_voltage_margin": _round(margin),
                "segments": segment_results,
                "nodes": node_results,
                "over_current_segments": over_current_segments,
                "under_voltage_nodes": under_voltage_nodes,
            }
        )

    # 可恢复候选中最低节点电压余量最大者（未舍入值比较，EPS 内平局按输入顺序）
    selected_jumper_id: Optional[str] = None
    best_margin = 0.0
    for candidate, margin in zip(candidates, margins):
        if not candidate["restorable"]:
            continue
        if selected_jumper_id is None or margin > best_margin + EPS:
            selected_jumper_id = candidate["jumper_id"]
            best_margin = margin

    return {
        "faulty_segment_id": faulty_segment_id,
        "candidates": candidates,
        "selected_jumper_id": selected_jumper_id,
    }


def _shed_tree(
    base: _Tree,
    source_voltage: float,
    shed_set: set[int],
    load_nodes: Sequence[str],
    load_currents: Sequence[float],
) -> tuple[_Tree, float]:
    """在基线树 ``base`` 上切除 ``shed_set``（loads 输入序号集合）后重算潮流。

    被切除负载整条置零（不改变树结构与节点最低允许电压覆盖），自叶向根重算线段
    电流、自根向叶重算节点电压；返回校核树与未舍入的被切除电流总和。
    """
    load_map = dict(base.load_map)
    node_total = {node: load_map.get(node, 0.0) for node in base.order}
    for i in shed_set:
        node = load_nodes[i]
        current = load_currents[i]
        node_total[node] -= current
        load_map[node] = load_map.get(node, 0.0) - current

    seg_current: dict[str, float] = {}
    for node in reversed(base.order):
        for seg in base.children_map.get(node, []):
            seg_current[seg.id] = node_total[seg.child]
            node_total[node] += node_total[seg.child]

    node_voltage = {ROOT_NODE: source_voltage}
    for node in base.order:
        for seg in base.children_map.get(node, []):
            node_voltage[seg.child] = node_voltage[node] - seg_current[seg.id] * seg.resistance

    tree = replace(
        base,
        load_map=load_map,
        node_total=node_total,
        seg_current=seg_current,
        node_voltage=node_voltage,
    )
    shed_current = sum(load_currents[i] for i in shed_set)
    return tree, shed_current


def _enumerate_feasible(
    base: _Tree,
    min_voltage: float,
    selected: Sequence[int],
    load_nodes: Sequence[str],
    load_currents: Sequence[float],
) -> Optional[tuple[int, float]]:
    """枚举 ``selected``（升序负载序号）的切除组合，返回最优 (位掩码, 切除电流)。

    切除负载 i 使线段电流、节点电压对切除集合呈线性关系，每条约束写成余量：

    - 线段 e：基线电流 − Σ(在 e 下游的被切除负载电流) ≤ 额定电流 + EPS；
    - 节点 v：基线电压 + Σ R_common(v, i) × 被切除负载电流 ≥ 生效阈值 − EPS，
      R_common(v, i) 为 v 与负载 i 两条根路径的公共线段电阻之和（与
      ``peak_risk`` 的节点贡献口径一致，数值等价于切除后重算潮流的电压回升量）。

    搜索为按负载顺序的深度优先枚举（不选 / 选），可行性对集合包含单调：

    - 到达叶节点且无越限约束即为可行解；
    - 下界剪枝：某约束当前剩余越限量大于剩余未决策负载对它的贡献之和时，
      该分支不可能恢复，立即剪枝（据此根节点即可判定无解）；
    - 上界剪枝：当前切除电流已严格大于已知最优（超过 EPS）时剪枝；与最优
      电流并列的分支继续搜索，平局按显式构造的升序序号元组比较，完成「条数
      最少、序号列表字典序最小」的判定。
    """
    seg_by_child = {seg.child: seg for seg in base.segments}
    n_seg = len(base.segments)
    n_node = len(base.order)
    n_con = n_seg + n_node
    node_pos = {node: pos for pos, node in enumerate(base.order)}

    def _path_seg_ids(node: str) -> set[str]:
        path: set[str] = set()
        while node != ROOT_NODE:
            seg = seg_by_child[node]
            path.add(seg.id)
            node = seg.parent
        return path

    # 每个候选负载对各约束的贡献（行=约束：先线段后节点，列=候选负载位）
    seg_rows: list[list[float]] = [[] for _ in range(n_seg)]
    node_rows: list[list[float]] = [[] for _ in range(n_node)]
    m = len(selected)
    for idx in selected:
        path = _path_seg_ids(load_nodes[idx])
        current = load_currents[idx]
        for k, seg in enumerate(base.segments):
            seg_rows[k].append(current if seg.id in path else 0.0)
        common = [0.0] * n_node
        for parent in base.order:  # BFS：父先于子
            pp = node_pos[parent]
            for seg in base.children_map.get(parent, []):
                cp = node_pos[seg.child]
                common[cp] = common[pp] + (seg.resistance if seg.id in path else 0.0)
        for p in range(n_node):
            node_rows[p].append(current * common[p])

    rows = seg_rows + node_rows
    # suffix[t][j]：第 j 位及以后的负载对约束 t 能提供的最大缓解量，用于下界剪枝
    suffix = [[0.0] * (m + 1) for _ in range(n_con)]
    for t in range(n_con):
        for j in range(m - 1, -1, -1):
            suffix[t][j] = suffix[t][j + 1] + rows[t][j]

    # 基线各约束的剩余越限量（正值 = 越限量；<= EPS 视为不越限）
    initial = [base.seg_current[seg.id] - seg.rated_current for seg in base.segments]
    initial += [
        _node_threshold(base, node, min_voltage) - base.node_voltage[node]
        for node in base.order
    ]

    currents = [load_currents[idx] for idx in selected]

    # 仅初始越限的约束需要跟踪（切除只会使其余约束更安全）
    watched = [t for t in range(n_con) if initial[t] > EPS]
    if not watched:
        return 0, 0.0  # 不会发生：调用方已确认基线越限
    # 根下界剪枝：全部可切除负载都切除仍无法消除某约束越限 -> 无解
    for t in watched:
        if initial[t] - suffix[t][0] > EPS:
            return None

    remaining = [initial[t] for t in watched]
    n_watch = len(watched)

    best_mask: Optional[int] = None
    best_current = 0.0
    best_size = 0
    best_tuple: tuple[int, ...] = ()

    def _index_tuple(mask: int) -> tuple[int, ...]:
        return tuple(j for j in range(m) if mask >> j & 1)

    def _search(j: int, mask: int, count: int, shed_sum: float) -> None:
        nonlocal best_mask, best_current, best_size, best_tuple

        if j == m:
            if all(v <= EPS for v in remaining):
                index_tuple = _index_tuple(mask)
                if best_mask is None or shed_sum < best_current - EPS or (
                    abs(shed_sum - best_current) <= EPS
                    and (count < best_size or (count == best_size and index_tuple < best_tuple))
                ):
                    best_mask = mask
                    best_current = shed_sum
                    best_size = count
                    best_tuple = index_tuple
            return

        # 上界剪枝：已严格超出已知最优切除电流
        if best_mask is not None and shed_sum > best_current + EPS:
            return
        # 下界剪枝：任一越限约束无法被剩余负载（j..m-1）完全缓解
        for w in range(n_watch):
            if remaining[w] - suffix[watched[w]][j] > EPS:
                return

        # 不选第 j 位（字典序优先）
        _search(j + 1, mask, count, shed_sum)

        # 选第 j 位：扣除该负载对各跟踪约束的贡献后递归，再还原
        contribution = [rows[t][j] for t in watched]
        for w in range(n_watch):
            remaining[w] -= contribution[w]
        _search(j + 1, mask | (1 << j), count + 1, shed_sum + currents[j])
        for w in range(n_watch):
            remaining[w] += contribution[w]

    _search(0, 0, 0, 0.0)

    if best_mask is None:
        return None
    return best_mask, best_current


def load_shedding(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    sheddable_indices: Sequence[int],
) -> dict:
    """枚举可切除负载组合，选出消除全部越限的最优切除方案。

    序号按 ``loads`` 从 0 开始指定可切除负载（至多 16 条，不得重复或越界，
    由请求模型 / 本函数共同校验）；每条负载只能整条切除，其余保持供电。对每种
    切除组合重算线段电流与节点电压（越限判定沿用 ``evaluate`` 的额定电流、节点
    最低允许电压与 EPS 容差，恰好触限不算越限）：

    - 原网已安全：选空集，``status=already_safe``；
    - 存在安全组合：``status=solved``，取被切除电流总和（未舍入值比较，EPS 内
      视为并列）最小者；并列时取切除条数最少，再并列时取升序序号列表字典序
      最小者；
    - 全部可切除负载切除后仍越限：``status=infeasible``，无切除方案。
    """
    base = _prepare_tree(source_voltage, segments_in, loads_in)

    # 序号业务校验：不得越界或重复（非整数 / 负数 / 超长已由请求模型拦截）
    errors: list[str] = []
    seen: set[int] = set()
    for idx in sheddable_indices:
        if not 0 <= idx < len(loads_in):
            range_hint = "（当前 loads 为空）" if not loads_in else f"（须在 0..{len(loads_in) - 1} 内）"
            errors.append(f"可切除负载序号 {idx} 越界{range_hint}")
        elif idx in seen:
            errors.append(f"可切除负载序号 {idx} 重复")
        seen.add(idx)
    if errors:
        raise InputValidationError("可切除负载列表无效", errors)

    load_currents = [load.current for load in loads_in]
    load_nodes = [load.node for load in loads_in]
    selected = sorted(seen)  # 去重后升序（重复已在上面拦截）

    def _safe_result(tree: _Tree) -> dict:
        segment_results, node_results, over_current_segments, under_voltage_nodes = _tree_results(
            tree, min_voltage
        )
        return {
            "ok": not over_current_segments and not under_voltage_nodes,
            "source_voltage": source_voltage,
            "min_voltage": min_voltage,
            "total_source_current": _round(tree.node_total[ROOT_NODE]),
            "segments": segment_results,
            "nodes": node_results,
            "over_current_segments": over_current_segments,
            "under_voltage_nodes": under_voltage_nodes,
        }

    base_over, base_under = _find_violations(base, min_voltage)
    if not base_over and not base_under:
        return {
            "status": "already_safe",
            "shed_loads": [],
            "shed_count": 0,
            "shed_current": 0.0,
            "shed_details": [],
            "result": _safe_result(base),
        }

    # 分支限界枚举全部组合（至多 2^16 = 65536 种），按（切除电流总和, 切除条数,
    # 升序序号列表）字典序选最优；可行性对集合包含单调，无解在根节点即可判定。
    found = _enumerate_feasible(base, min_voltage, selected, load_nodes, load_currents)
    if found is None:
        return {
            "status": "infeasible",
            "shed_loads": None,
            "shed_count": None,
            "shed_current": None,
            "shed_details": None,
            "result": None,
        }

    best_mask, shed_current = found
    shed_indices = [selected[j] for j in range(len(selected)) if best_mask >> j & 1]
    best_tree, _ = _shed_tree(base, source_voltage, set(shed_indices), load_nodes, load_currents)

    shed_details = [
        {"index": i, "node": load_nodes[i], "current": load_currents[i]}
        for i in shed_indices
    ]
    return {
        "status": "solved",
        "shed_loads": shed_indices,
        "shed_count": len(shed_indices),
        "shed_current": _round(shed_current),
        "shed_details": shed_details,
        "result": _safe_result(best_tree),
    }


def _curve_operating_time(points: Sequence[tuple[float, float]], current: float) -> Optional[float]:
    """分段线性保护曲线在给定电流下的动作时间。

    电流严格低于首点电流时装置不动作（返回 None）；落在相邻两点间时线性插值；
    达到 / 超过末点电流时沿用末点动作时间。
    """
    if current < points[0][0]:
        return None
    if current >= points[-1][0]:
        return points[-1][1]
    for (c0, t0), (c1, t1) in zip(points, points[1:]):
        if current <= c1:
            return t0 + (t1 - t0) * (current - c0) / (c1 - c0)
    return points[-1][1]  # 数值兜底（逻辑上不可达）


def protection_selectivity(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    curves_in: Sequence,
    faults_in: Sequence,
    min_grading_margin: float,
) -> dict:
    """校核各故障下保护曲线的动作选择性，并用基线电流标出可能误动的装置。

    每条线段恰配置一条曲线，曲线为分段线性：故障电流严格低于首点电流不动作，
    点间线性插值，达到 / 超过末点沿用末点动作时间。对每个故障，故障电流仅流经
    故障节点至 source 路径上的线段：

    - 距故障最近的装置（故障节点的进线段）必须动作，否则该段记
      ``nearest_not_operating`` 失配；
    - 所有会动作的上级装置动作时间须 ≥ 最近装置动作时间 + 最小级差，否则记
      ``insufficient_margin`` 失配（恰好满足不算失配，按未舍入值、EPS 容差比较）；
    - 动作顺序为会动作装置按动作时间升序（未舍入值排序），时间相同时距故障近者
      在前。

    误动检查对全部线段使用基线（正常负荷）线段电流：基线电流达到曲线首点电流即
    视为正常负荷下会动作，列入 ``misoperation_segments``。总体结论 ``ok`` 要求
    全部故障满足选择性且无误动装置。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    # 曲线校验：线段 id 存在、不重复且覆盖全部线段
    known_seg_ids = {seg.id for seg in tree.segments}
    errors: list[str] = []
    seen_ids: set[str] = set()
    curve_map: dict[str, list[tuple[float, float]]] = {}
    for curve in curves_in:
        seg_id = curve.segment_id
        if seg_id in seen_ids:
            errors.append(f"线段 '{seg_id}' 的保护曲线重复定义")
        seen_ids.add(seg_id)
        if seg_id not in known_seg_ids:
            errors.append(f"保护曲线线段 '{seg_id}' 不存在于树中")
        elif seg_id not in curve_map:
            curve_map[seg_id] = [(point.current, point.time) for point in curve.points]
    for seg in tree.segments:  # 保持输入顺序
        if seg.id not in curve_map:
            errors.append(f"线段 '{seg.id}' 缺少保护曲线")
    if errors:
        raise InputValidationError("保护曲线定义无效", errors)

    # 故障校验：节点存在且不能为根节点 source
    errors = []
    for fault in faults_in:
        if fault.node == ROOT_NODE:
            errors.append(f"故障节点不能为根节点 '{ROOT_NODE}'")
        elif fault.node not in tree.parent_of:
            errors.append(f"故障节点 '{fault.node}' 不存在于树中")
    if errors:
        raise InputValidationError("故障定义无效", errors)

    seg_by_child = {seg.child: seg for seg in tree.segments}

    fault_results = []
    for fault in faults_in:
        # 自故障节点向 source 的装置路径（level 0 = 距故障最近的进线段）
        path: list[Segment] = []
        node = fault.node
        while node != ROOT_NODE:
            seg = seg_by_child[node]
            path.append(seg)
            node = seg.parent

        times = [_curve_operating_time(curve_map[seg.id], fault.current) for seg in path]

        devices = [
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "level": level,
                "operates": operating_time is not None,
                "operating_time": _round(operating_time) if operating_time is not None else None,
            }
            for level, (seg, operating_time) in enumerate(zip(path, times))
        ]

        # 动作顺序：会动作装置按（未舍入动作时间, 级数）升序
        operating_sequence = [
            seg.id
            for _, _, seg in sorted(
                (
                    (operating_time, level, seg)
                    for level, (seg, operating_time) in enumerate(zip(path, times))
                    if operating_time is not None
                ),
                key=lambda item: (item[0], item[1]),
            )
        ]

        miscoordinated = []
        nearest_time = times[0]
        if nearest_time is None:
            miscoordinated.append(
                {
                    "segment_id": path[0].id,
                    "reason": "nearest_not_operating",
                    "operating_time": None,
                    "required_time": None,
                }
            )
        else:
            required_time = nearest_time + min_grading_margin
            for seg, operating_time in zip(path[1:], times[1:]):
                if operating_time is not None and operating_time < required_time - EPS:
                    miscoordinated.append(
                        {
                            "segment_id": seg.id,
                            "reason": "insufficient_margin",
                            "operating_time": _round(operating_time),
                            "required_time": _round(required_time),
                        }
                    )

        fault_results.append(
            {
                "node": fault.node,
                "fault_current": fault.current,
                "selective": not miscoordinated,
                "devices": devices,
                "operating_sequence": operating_sequence,
                "miscoordinated_segments": miscoordinated,
            }
        )

    # 基线（正常负荷）电流下即会动作（首点电流 ≤ 基线电流）的装置可能误动
    misoperation_segments = []
    for seg in tree.segments:  # 保持输入顺序
        points = curve_map[seg.id]
        baseline_current = tree.seg_current[seg.id]
        operating_time = _curve_operating_time(points, baseline_current)
        if operating_time is not None:
            misoperation_segments.append(
                {
                    "segment_id": seg.id,
                    "parent": seg.parent,
                    "child": seg.child,
                    "baseline_current": _round(baseline_current),
                    "pickup_current": points[0][0],
                    "operating_time": _round(operating_time),
                }
            )

    return {
        "ok": all(result["selective"] for result in fault_results)
        and not misoperation_segments,
        "min_grading_margin": min_grading_margin,
        "faults": fault_results,
        "misoperation_segments": misoperation_segments,
    }


def _energized_tree(
    base: _Tree,
    source_voltage: float,
    startup_in: Sequence,
    energized: set[int],
    new_index: Optional[int],
) -> _Tree:
    """在基线树 ``base`` 上叠加待投运负载后重算潮流。

    ``energized`` 中（此前已投运）的负载按稳态电流计入；``new_index``（本步新投运
    负载）按启动电流计入；其余（未投运）负载不计入。树结构与节点最低允许电压覆盖
    保持不变，自叶向根重算线段电流、自根向叶重算节点电压（均为未舍入值）。
    """
    extra: dict[str, float] = {}
    for i in energized:
        load = startup_in[i]
        extra[load.node] = extra.get(load.node, 0.0) + load.steady_current
    if new_index is not None:
        load = startup_in[new_index]
        extra[load.node] = extra.get(load.node, 0.0) + load.startup_current

    node_total = {
        node: base.load_map.get(node, 0.0) + extra.get(node, 0.0) for node in base.order
    }
    seg_current: dict[str, float] = {}
    for node in reversed(base.order):
        for seg in base.children_map.get(node, []):
            seg_current[seg.id] = node_total[seg.child]
            node_total[node] += node_total[seg.child]

    node_voltage = {ROOT_NODE: source_voltage}
    for node in base.order:
        for seg in base.children_map.get(node, []):
            node_voltage[seg.child] = node_voltage[node] - seg_current[seg.id] * seg.resistance

    return replace(base, node_total=node_total, seg_current=seg_current, node_voltage=node_voltage)


def startup_order(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    startup_loads_in: Sequence,
) -> dict:
    """搜索待投运负载完整可行的投运顺序，返回可 JSON 序列化的结果字典。

    接收 1～10 条待投运负载（条数由请求模型保证），其节点须存在于树中，启动电流
    不小于稳态电流由请求模型保证。基线（仅既有负载）已越限时抛
    ``BaselineViolationError``；否则按输入序号递增做子集状态深度优先搜索：

    - 每步新投运负载按启动电流计入，此前投运负载按稳态电流计入，未投运负载不计入；
    - 越限判定沿用 ``evaluate`` 的额定电流、节点最低允许电压（含节点级覆盖）与
      EPS 容差规则（未舍入值比较，恰好触限不算越限）；
    - 可行性对已投运集合单调（新增负载只增电流、只降电压），不可行集合记忆化；
    - 首个完整顺序即按输入序号字典序最小者；不存在完整顺序时 ``status=infeasible``。
    """
    base = _prepare_tree(source_voltage, segments_in, loads_in)

    known_nodes = set(base.parent_of) | {ROOT_NODE}
    errors: list[str] = []
    for index, load in enumerate(startup_loads_in):
        if load.node not in known_nodes:
            errors.append(f"第 {index + 1} 条待投运负载的节点 '{load.node}' 不存在于树中")
    if errors:
        raise InputValidationError("待投运负载定义无效", errors)

    over_current_segments, under_voltage_nodes = _find_violations(base, min_voltage)
    if over_current_segments or under_voltage_nodes:
        details = [
            f"线段 '{v['segment_id']}' 已超额定电流：{v['current']} A（额定 {v['rated_current']} A）"
            for v in over_current_segments
        ]
        details += [
            f"节点 '{v['node']}' 已欠压：{v['voltage']} V（最低允许 {v['min_voltage']} V）"
            for v in under_voltage_nodes
        ]
        raise BaselineViolationError("基线校核已存在超额定电流或欠压，无法安排投运顺序", details)

    n = len(startup_loads_in)
    dead: set[int] = set()  # 记忆化：已证实不存在完整可行顺序的已投运集合（位掩码）

    def _search(mask: int, order: list[int]) -> Optional[list[int]]:
        energized = set(order)
        for i in range(n):
            if mask >> i & 1:
                continue
            candidate = mask | (1 << i)
            if candidate in dead:
                continue
            tree = _energized_tree(base, source_voltage, startup_loads_in, energized, i)
            over, under = _find_violations(tree, min_voltage)
            if over or under:
                continue  # 该负载本步启动瞬间越限（恰好触限不算越限）
            if candidate == (1 << n) - 1:
                return order + [i]
            result = _search(candidate, order + [i])
            if result is not None:
                return result
        dead.add(mask)
        return None

    order = _search(0, [])
    if order is None:
        return {"status": "infeasible", "order": None, "steps": None}

    steps = []
    for step_no, index in enumerate(order, start=1):
        energized = set(order[: step_no - 1])
        tree = _energized_tree(base, source_voltage, startup_loads_in, energized, index)

        # 最小电压节点：BFS 顺序最先（未舍入值比较，EPS 内视为平局）
        min_node = base.order[0]
        min_voltage_value = tree.node_voltage[min_node]
        for node in base.order[1:]:
            if tree.node_voltage[node] < min_voltage_value - EPS:
                min_voltage_value = tree.node_voltage[node]
                min_node = node

        # 最大负载率线段：输入顺序最先（未舍入值比较，EPS 内视为平局）
        max_seg = base.segments[0]
        max_ratio = tree.seg_current[max_seg.id] / max_seg.rated_current
        for seg in base.segments[1:]:
            ratio = tree.seg_current[seg.id] / seg.rated_current
            if ratio > max_ratio + EPS:
                max_ratio = ratio
                max_seg = seg

        steps.append(
            {
                "step": step_no,
                "load_index": index,
                "node": startup_loads_in[index].node,
                "startup_current": startup_loads_in[index].startup_current,
                "min_voltage": _round(min_voltage_value),
                "min_voltage_node": min_node,
                "max_loading_ratio": _round(max_ratio),
                "max_loading_segment_id": max_seg.id,
            }
        )

    return {"status": "feasible", "order": order, "steps": steps}


def loss_allocation(
    source_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
) -> dict:
    """计算逐段线损并按负载逐条归属，返回可 JSON 序列化的结果字典。

    每段线损 P = I²R，I 为未舍入的基线线段电流；总线损为各段线损之和。负载 i
    （按 ``loads`` 输入序号）在其到 source 路径的每段上承担
    ``负载电流 × 该段总电流 × 该段电阻``——同一段上全部下游负载的归属之和恰等于
    该段线损，故逐负载归属之和与总线损一致（展示舍入差除外）。source 节点负载
    路径为空、归属为零；同节点多条负载分别列出。计算与汇总使用未舍入值，展示值
    按 6 位小数舍入。基线是否存在超额定 / 欠压不影响线损计算（不做越限拦截）。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    segment_results = []
    total_loss = 0.0
    for seg in tree.segments:  # 保持输入顺序
        current = tree.seg_current[seg.id]
        loss = current * current * seg.resistance
        total_loss += loss
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(current),
                "resistance": seg.resistance,
                "loss": _round(loss),
            }
        )

    seg_by_child = {seg.child: seg for seg in tree.segments}
    load_results = []
    for index, load in enumerate(loads_in):
        # 负载节点到 source 的路径（自负载节点向 source 排序）；source 节点为空路径
        path: list[Segment] = []
        node = load.node
        while node != ROOT_NODE:
            seg = seg_by_child[node]
            path.append(seg)
            node = seg.parent

        shares = []
        load_loss = 0.0
        for seg in path:
            share = load.current * tree.seg_current[seg.id] * seg.resistance
            load_loss += share
            shares.append({"segment_id": seg.id, "loss": _round(share)})
        load_results.append(
            {
                "index": index,
                "node": load.node,
                "current": load.current,
                "loss": _round(load_loss),
                "segments": shares,
            }
        )

    return {
        "total_loss": _round(total_loss),
        "segments": segment_results,
        "loads": load_results,
    }
