"""请求 / 响应数据模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

class SegmentInput(BaseModel):
    """一条线段：电流自父节点流向子节点。"""

    id: Optional[str] = Field(default=None, description="线段标识，缺省自动生成 seg-<序号>")
    parent: str = Field(min_length=1, description="父节点名称，根节点固定为 source")
    child: str = Field(min_length=1, description="子节点名称")
    resistance: float = Field(ge=0, description="线段电阻 (Ω)，不允许为负")
    rated_current: float = Field(gt=0, description="线段额定电流 (A)，必须为正")


class LoadInput(BaseModel):
    """节点恒定负载。"""

    node: str = Field(min_length=1, description="节点名称")
    current: float = Field(ge=0, description="恒定负载电流 (A)，不允许为负")
    min_voltage: Optional[float] = Field(
        default=None, ge=0, description="该节点最低允许电压 (V)，缺省使用全局 min_voltage"
    )


class EvaluateRequest(BaseModel):
    source_voltage: float = Field(gt=0, description="电源电压 (V)，必须为正")
    min_voltage: float = Field(ge=0, description="全局最低允许电压 (V)")
    segments: list[SegmentInput] = Field(
        min_length=1, description="线段列表，须构成以 source 为根的连通树"
    )
    loads: list[LoadInput] = Field(default_factory=list, description="各节点恒定负载电流")


class SegmentResult(BaseModel):
    segment_id: str
    parent: str
    child: str
    current: float = Field(description="线段承载的全部下游电流 (A)")
    rated_current: float = Field(description="线段额定电流 (A)")
    resistance: float = Field(description="线段电阻 (Ω)")
    voltage_drop: float = Field(description="线段压降 (V)")
    over_rated: bool = Field(description="是否超过额定电流")


class NodeResult(BaseModel):
    node: str
    voltage: float = Field(description="节点电压 (V)")
    min_voltage: float = Field(description="该节点生效的最低允许电压 (V)")
    load_current: float = Field(description="节点恒定负载电流 (A)")
    under_voltage: bool = Field(description="是否欠压")


class OverCurrentViolation(BaseModel):
    segment_id: str
    parent: str
    child: str
    current: float
    rated_current: float
    excess: float = Field(description="超出额定值的电流 (A)")


class UnderVoltageViolation(BaseModel):
    node: str
    voltage: float
    min_voltage: float
    deficit: float = Field(description="低于最低允许电压的差值 (V)")


class EvaluateResponse(BaseModel):
    ok: bool = Field(description="无超额定且无欠压时为 true")
    source_voltage: float
    min_voltage: float
    total_source_current: float = Field(description="电源输出的总电流 (A)")
    segments: list[SegmentResult]
    nodes: list[NodeResult]
    over_current_segments: list[OverCurrentViolation] = Field(description="超额定电流的线段")
    under_voltage_nodes: list[UnderVoltageViolation] = Field(description="欠压节点")


class CapacityRequest(EvaluateRequest):
    """增容评估请求：在 /evaluate 参数基础上指定目标节点。"""

    target_node: str = Field(min_length=1, description="目标节点名称（须为已有的非 source 节点）")


class LimitingFactor(BaseModel):
    type: str = Field(description="限制类型：segment=线段额定电流，node=节点最低允许电压")
    id: str = Field(description="达到上限的线段 id 或节点名称")


class CapacityResponse(BaseModel):
    target_node: str = Field(description="目标节点名称")
    max_additional_current: float = Field(
        description="目标节点可增加的最大恒定负载电流 (A)，向下截断至 6 位小数"
    )
    limiting_factors: list[LimitingFactor] = Field(description="达到上限的线段 / 节点")


class MeasurementInput(BaseModel):
    """一条线段的实测电流。"""

    segment_id: str = Field(min_length=1, description="被测线段标识，须唯一且存在于树中")
    current: float = Field(ge=0, allow_inf_nan=False, description="实测电流 (A)，须为非负有限数")


class UnregisteredLoadRequest(EvaluateRequest):
    """未登记负载定位请求：在 /evaluate 参数基础上附加线段实测电流。"""

    measurements: list[MeasurementInput] = Field(
        default_factory=list, description="线段实测电流列表，测点可不覆盖全树"
    )


class MeasurementResult(BaseModel):
    segment_id: str
    baseline_current: float = Field(description="基线线段电流 (A)，展示值按 6 位小数舍入")
    measured_current: float = Field(description="实测电流 (A)")
    difference: float = Field(description="实测电流 - 基线电流 (A)，展示值按 6 位小数舍入")


class UnregisteredLoadResponse(BaseModel):
    status: str = Field(
        description="定位结果：located=已定位候选节点，undetermined=无显著正差无法确定，"
        "inconsistent=实测与基线矛盾"
    )
    load_current: Optional[float] = Field(
        default=None, description="未登记负载电流估计值 (A)，六位小数；status=located 时给出"
    )
    candidate_nodes: list[str] = Field(description="与全部测点相容的节点（BFS 顺序）")
    measurements: list[MeasurementResult] = Field(description="各测点与未舍入基线的对比")
    reasons: list[str] = Field(description="判定为 inconsistent 的原因")


class PeakLoadInput(LoadInput):
    """节点恒定负载，可声明峰值电流。"""

    peak_current: Optional[float] = Field(
        default=None, ge=0, description="峰值电流 (A)，缺省等于 current，须不小于 current"
    )

    @model_validator(mode="after")
    def _peak_not_below_current(self) -> "PeakLoadInput":
        if self.peak_current is not None and self.peak_current < self.current:
            raise ValueError("峰值电流不能小于负载电流 current")
        return self


class PeakRiskRequest(EvaluateRequest):
    """峰值风险评估请求：在 /evaluate 参数基础上限定同时升至峰值的负载数量。"""

    loads: list[PeakLoadInput] = Field(
        default_factory=list, description="各节点负载；同节点多条负载分别计数"
    )
    max_simultaneous_peaks: int = Field(
        ge=0, description="允许同时升至峰值的负载最大数量（非负整数）"
    )


class PeakSegmentResult(BaseModel):
    segment_id: str
    parent: str
    child: str
    current: float = Field(description="最坏情况下线段最大承载电流 (A)")
    rated_current: float = Field(description="线段额定电流 (A)")
    peak_loads: list[int] = Field(description="达到最坏值时处于峰值的负载输入序号（从 0 开始）")
    over_rated: bool = Field(description="最坏情况下是否超过额定电流")


class PeakNodeResult(BaseModel):
    node: str
    voltage: float = Field(description="最坏情况下节点最低电压 (V)")
    min_voltage: float = Field(description="该节点生效的最低允许电压 (V)")
    peak_loads: list[int] = Field(description="达到最坏值时处于峰值的负载输入序号（从 0 开始）")
    under_voltage: bool = Field(description="最坏情况下是否欠压")


class PeakOverCurrentViolation(BaseModel):
    segment_id: str
    parent: str
    child: str
    current: float
    rated_current: float
    excess: float = Field(description="超出额定值的电流 (A)")
    peak_loads: list[int] = Field(description="导致该最坏值的峰值负载输入序号（从 0 开始）")


class PeakUnderVoltageViolation(BaseModel):
    node: str
    voltage: float
    min_voltage: float
    deficit: float = Field(description="低于最低允许电压的差值 (V)")
    peak_loads: list[int] = Field(description="导致该最坏值的峰值负载输入序号（从 0 开始）")


class PeakRiskResponse(BaseModel):
    ok: bool = Field(description="最坏情况下无超额定且无欠压时为 true")
    max_simultaneous_peaks: int = Field(description="允许同时升至峰值的负载最大数量")
    segments: list[PeakSegmentResult]
    nodes: list[PeakNodeResult]
    over_current_segments: list[PeakOverCurrentViolation] = Field(
        description="最坏情况下超额定电流的线段"
    )
    under_voltage_nodes: list[PeakUnderVoltageViolation] = Field(
        description="最坏情况下欠压的节点"
    )


class JumperInput(BaseModel):
    """一条候选跨接线：自供电侧节点引向失电侧节点。"""

    id: str = Field(min_length=1, description="跨接线标识，须全局唯一（不与已有线段 id 重复）")
    supply_node: str = Field(min_length=1, description="供电侧节点，须位于 source 所在分量")
    outage_node: str = Field(min_length=1, description="失电侧节点，可为失电分量任意节点")
    resistance: float = Field(ge=0, description="跨接线电阻 (Ω)，不允许为负")
    rated_current: float = Field(gt=0, description="跨接线额定电流 (A)，必须为正")


class OutageRestorationRequest(EvaluateRequest):
    """故障恢复评估请求：在 /evaluate 参数基础上指定故障线段与候选跨接线。"""

    faulty_segment_id: str = Field(min_length=1, description="故障断开的线段标识，须存在于树中")
    jumpers: list[JumperInput] = Field(description="候选跨接线列表，逐条独立评估（可为空）")


class RestorationCandidateResult(BaseModel):
    jumper_id: str = Field(description="跨接线标识")
    supply_node: str = Field(description="供电侧节点")
    outage_node: str = Field(description="失电侧节点（接入点）")
    restorable: bool = Field(description="恢复后全树无超额定电流且无欠压时为 true")
    min_voltage_margin: float = Field(
        description="全树最低节点电压余量 (V)：min(节点电压 - 生效最低允许电压)，"
        "展示值按 6 位小数舍入"
    )
    segments: list[SegmentResult] = Field(
        description="恢复后全树线段（故障线段退出运行不计入，跨接线列于末尾；"
        "失电分量内方向翻转的线段以新方向展示）"
    )
    nodes: list[NodeResult] = Field(description="恢复后各节点电压（BFS 顺序，根在前）")
    over_current_segments: list[OverCurrentViolation] = Field(description="恢复后超额定电流的线段")
    under_voltage_nodes: list[UnderVoltageViolation] = Field(description="恢复后欠压的节点")


class OutageRestorationResponse(BaseModel):
    faulty_segment_id: str = Field(description="故障断开的线段标识")
    candidates: list[RestorationCandidateResult] = Field(
        description="各候选跨接线的评估结果（按输入顺序）"
    )
    selected_jumper_id: Optional[str] = Field(
        default=None,
        description="选中的跨接线 id：可恢复候选中最低节点电压余量最大者（平局按输入顺序）；"
        "无可恢复方案时为 null",
    )


class LoadSheddingRequest(EvaluateRequest):
    """切负荷方案评估请求：在 /evaluate 参数基础上指定可切除负载序号列表。"""

    sheddable_loads: list[int] = Field(
        default_factory=list,
        max_length=16,
        description="可切除负载的输入序号列表（loads 数组下标，从 0 开始）；"
        "至多 16 条，序号不得重复或越界，缺省 / 空列表表示无负载可切除",
    )


class ShedLoadDetail(BaseModel):
    index: int = Field(description="被切除负载的输入序号（loads 数组下标，从 0 开始）")
    node: str = Field(description="被切除负载所在节点")
    current: float = Field(description="被切除负载电流 (A)")


class LoadSheddingResponse(BaseModel):
    status: str = Field(
        description="求解结果：already_safe=原网已安全（选空集），"
        "solved=存在可行方案，infeasible=全部可切除负载切除后仍越限（无解）"
    )
    shed_loads: Optional[list[int]] = Field(
        default=None,
        description="被切除负载的输入序号（从 0 开始，升序）；"
        "already_safe 时为空列表，infeasible 时为 null",
    )
    shed_count: Optional[int] = Field(
        default=None, description="被切除负载条数；infeasible 时为 null"
    )
    shed_current: Optional[float] = Field(
        default=None,
        description="被切除电流总和 (A)，展示值按 6 位小数舍入；"
        "already_safe 时为 0.0，infeasible 时为 null",
    )
    shed_details: Optional[list[ShedLoadDetail]] = Field(
        default=None,
        description="各被切除负载的明细（序号、节点、电流），按序号升序；"
        "already_safe 时为空列表，infeasible 时为 null",
    )
    result: Optional[EvaluateResponse] = Field(
        default=None,
        description="切除后的 /evaluate 风格校核结果（重算后的线段电流、节点电压与越限列表）；"
        "already_safe 时为原网校核结果，infeasible 时为 null",
    )


class CurvePointInput(BaseModel):
    """保护曲线上的一个点：电流 -> 动作时间。"""

    current: float = Field(
        ge=0, allow_inf_nan=False, description="电流 (A)，曲线点按电流严格递增"
    )
    time: float = Field(ge=0, allow_inf_nan=False, description="动作时间 (s)，不允许为负")


class ProtectionCurveInput(BaseModel):
    """一条线段的分段线性保护曲线。"""

    segment_id: str = Field(
        min_length=1, description="线段标识，须存在于树中；每条线段恰配置一条曲线"
    )
    points: list[CurvePointInput] = Field(
        min_length=1, description="曲线点列表（至少 1 个），按电流严格递增"
    )

    @model_validator(mode="after")
    def _points_strictly_increasing(self) -> "ProtectionCurveInput":
        currents = [point.current for point in self.points]
        if any(b <= a for a, b in zip(currents, currents[1:])):
            raise ValueError("曲线点电流必须严格递增")
        return self


class FaultInput(BaseModel):
    """一个故障场景：故障节点与故障电流。"""

    node: str = Field(min_length=1, description="故障节点名称（须为已有的非 source 节点）")
    current: float = Field(gt=0, allow_inf_nan=False, description="故障电流 (A)，必须为正")


class ProtectionSelectivityRequest(EvaluateRequest):
    """保护选择性校核请求：在 /evaluate 参数基础上配置各线段保护曲线与故障场景。"""

    curves: list[ProtectionCurveInput] = Field(
        min_length=1, description="各线段保护曲线，须覆盖全部线段且不得重复"
    )
    faults: list[FaultInput] = Field(min_length=1, description="故障场景列表，逐个独立评估")
    min_grading_margin: float = Field(
        ge=0,
        allow_inf_nan=False,
        description="最小级差 (s)：会动作的上级装置须比距故障最近的装置至少晚该时间",
    )


class ProtectionDeviceResult(BaseModel):
    segment_id: str
    parent: str
    child: str
    level: int = Field(description="距故障的级数：0 为距故障最近（故障节点进线段），向 source 递增")
    operates: bool = Field(description="故障电流下是否动作（故障电流低于曲线首点时不动作）")
    operating_time: Optional[float] = Field(
        default=None, description="动作时间 (s)，展示值按 6 位小数舍入；不动作时为 null"
    )


class MiscoordinatedSegment(BaseModel):
    segment_id: str
    reason: str = Field(
        description="失配原因：nearest_not_operating=距故障最近的装置未动作，"
        "insufficient_margin=与最近装置的级差不足"
    )
    operating_time: Optional[float] = Field(
        default=None, description="该装置动作时间 (s)，展示值按 6 位小数舍入；未动作时为 null"
    )
    required_time: Optional[float] = Field(
        default=None,
        description="要求的最早动作时间 (s) = 最近装置动作时间 + 最小级差；"
        "最近装置未动作时为 null",
    )


class FaultSelectivityResult(BaseModel):
    node: str = Field(description="故障节点")
    fault_current: float = Field(description="故障电流 (A)")
    selective: bool = Field(
        description="距故障最近的装置动作且全部会动作的上级装置满足最小级差时为 true"
    )
    devices: list[ProtectionDeviceResult] = Field(
        description="自故障点向 source 的各级装置（level 升序）"
    )
    operating_sequence: list[str] = Field(
        description="会动作装置按动作时间升序的线段 id（时间相同时距故障近者在前）"
    )
    miscoordinated_segments: list[MiscoordinatedSegment] = Field(description="失配线段")


class MisoperationSegmentResult(BaseModel):
    segment_id: str
    parent: str
    child: str
    baseline_current: float = Field(description="基线线段电流 (A)，展示值按 6 位小数舍入")
    pickup_current: float = Field(description="曲线首点电流（启动电流）(A)")
    operating_time: float = Field(description="基线电流下的动作时间 (s)，展示值按 6 位小数舍入")


class ProtectionSelectivityResponse(BaseModel):
    ok: bool = Field(description="总体结论：全部故障满足选择性且无基线误动风险时为 true")
    min_grading_margin: float = Field(description="生效的最小级差 (s)")
    faults: list[FaultSelectivityResult] = Field(description="各故障的校核结果（按输入顺序）")
    misoperation_segments: list[MisoperationSegmentResult] = Field(
        description="基线电流下即会动作（可能误动）的装置，按线段输入顺序"
    )


class StartupLoadInput(BaseModel):
    """一条待投运负载：节点、稳态电流与启动电流。"""

    node: str = Field(min_length=1, description="待投运负载所在节点（须存在于树中，可为 source）")
    steady_current: float = Field(
        ge=0, allow_inf_nan=False, description="稳态电流 (A)，不允许为负"
    )
    startup_current: float = Field(
        ge=0, allow_inf_nan=False, description="启动电流 (A)，不允许为负且须不小于稳态电流"
    )

    @model_validator(mode="after")
    def _startup_not_below_steady(self) -> "StartupLoadInput":
        if self.startup_current < self.steady_current:
            raise ValueError("启动电流不能小于稳态电流")
        return self


class StartupOrderRequest(EvaluateRequest):
    """投运顺序评估请求：在 /evaluate 参数基础上指定 1～10 条待投运负载。"""

    startup_loads: list[StartupLoadInput] = Field(
        min_length=1,
        max_length=10,
        description="待投运负载列表（1～10 条）；每条给出节点、稳态电流与启动电流",
    )


class StartupStepResult(BaseModel):
    step: int = Field(description="投运步号（从 1 开始）")
    load_index: int = Field(description="本步新投运负载在 startup_loads 中的输入序号（从 0 开始）")
    node: str = Field(description="本步新投运负载所在节点")
    startup_current: float = Field(description="本步新投运负载计入的启动电流 (A)")
    min_voltage: float = Field(
        description="启动瞬间全树最小节点电压 (V)，展示值按 6 位小数舍入，判定使用未舍入值"
    )
    min_voltage_node: str = Field(description="取得最小电压的节点（平局取 BFS 顺序最先者）")
    max_loading_ratio: float = Field(
        description="启动瞬间全树最大线段负载率 = 线段电流 / 额定电流，"
        "展示值按 6 位小数舍入，判定使用未舍入值"
    )
    max_loading_segment_id: str = Field(
        description="取得最大负载率的线段 id（平局取线段输入顺序最先者）"
    )


class StartupOrderResponse(BaseModel):
    status: str = Field(
        description="求解结果：feasible=存在完整可行的投运顺序，infeasible=无完整可行顺序"
    )
    order: Optional[list[int]] = Field(
        default=None,
        description="按输入序号字典序最小的可行投运顺序（startup_loads 数组下标，从 0 开始）；"
        "infeasible 时为 null",
    )
    steps: Optional[list[StartupStepResult]] = Field(
        default=None,
        description="按 order 逐步给出的启动瞬间校核摘要；infeasible 时为 null",
    )


class LossAllocationRequest(EvaluateRequest):
    """线损归属评估请求：与 /evaluate 参数完全相同（min_voltage 参与校验但不影响线损计算）。"""


class LossSegmentResult(BaseModel):
    segment_id: str
    parent: str
    child: str
    current: float = Field(description="线段承载的全部下游电流 (A)，展示值按 6 位小数舍入")
    resistance: float = Field(description="线段电阻 (Ω)")
    loss: float = Field(description="线段线损 P=I²R (W)，展示值按 6 位小数舍入")


class LoadSegmentShare(BaseModel):
    segment_id: str = Field(description="负载到 source 路径上的线段标识")
    loss: float = Field(
        description="该负载在该段承担的线损 = 负载电流 × 该段总电流 × 该段电阻 (W)，"
        "展示值按 6 位小数舍入"
    )


class LoadLossResult(BaseModel):
    index: int = Field(description="负载输入序号（loads 数组下标，从 0 开始）")
    node: str = Field(description="负载所在节点")
    current: float = Field(description="负载电流 (A)")
    loss: float = Field(description="该负载承担的线损总和 (W)，展示值按 6 位小数舍入")
    segments: list[LoadSegmentShare] = Field(
        description="该负载在到 source 路径各段上的线损归属（自负载节点向 source 排序）；"
        "source 节点负载为空列表"
    )


class LossAllocationResponse(BaseModel):
    total_loss: float = Field(description="全树总线损 (W)：各段线损之和，展示值按 6 位小数舍入")
    segments: list[LossSegmentResult] = Field(description="逐线段线损（按线段输入顺序）")
    loads: list[LoadLossResult] = Field(
        description="逐负载线损归属（按 loads 输入序号）；同节点多条负载分别列出，"
        "逐负载归属之和与总线损一致（展示舍入差除外）"
    )
