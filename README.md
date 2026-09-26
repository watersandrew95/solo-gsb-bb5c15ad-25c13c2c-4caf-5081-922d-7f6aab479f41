# 直流配电树压降校核 API

基于 FastAPI 的直流配电树校核服务：输入以 `source` 为根的配电树（电源电压、线段电阻与额定电流、各节点恒定负载、最低允许电压），自叶向根累计每段承载的全部下游电流，再自根向叶计算压降与节点电压，返回各段电流、各节点电压以及超额定电流 / 欠压位置；并可评估指定节点可追加的最大恒定负载电流（`/capacity`），根据若干线段的实测电流定位唯一未登记正恒定负载的候选节点（`/unregistered-load`），评估至多 K 条负载同时升至峰值时各线段 / 各节点的最坏越限风险（`/peak-risk`），在故障线段断开后评估各候选跨接线的恢复方案并择优（`/outage-restoration`），枚举可切除负载组合、选出以最小切除电流消除全部超额定与欠压的切负荷方案（`/load-shedding`），按各线段保护曲线校核故障下各级保护的动作选择性并标出基线误动风险（`/protection-selectivity`），为 1～10 条待投运负载搜索每步启动瞬间均不越限的完整可行投运顺序（`/startup-order`），或按未舍入基线线段电流计算逐段线损（P=I²R）并按负载输入序号逐条归属（`/loss-allocation`）。

## 快速启动（单条命令）

```bash
docker compose up --build
```

容器启动时会自动安装依赖（`pip install -r requirements.txt`）并启动服务。启动后服务地址为 `http://localhost:8000`（端口可用 `PORT` 环境变量修改），交互式文档见 `http://localhost:8000/docs`。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 宿主机映射端口，如 `PORT=9000 docker compose up --build` |

## API 地址

- 服务地址：`http://localhost:8000`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`GET http://localhost:8000/health`
- 校核接口：`POST http://localhost:8000/evaluate`
- 增容评估接口：`POST http://localhost:8000/capacity`
- 未登记负载定位接口：`POST http://localhost:8000/unregistered-load`
- 峰值风险评估接口：`POST http://localhost:8000/peak-risk`
- 故障恢复评估接口：`POST http://localhost:8000/outage-restoration`
- 切负荷方案评估接口：`POST http://localhost:8000/load-shedding`
- 保护选择性校核接口：`POST http://localhost:8000/protection-selectivity`
- 投运顺序评估接口：`POST http://localhost:8000/startup-order`
- 线损归属评估接口：`POST http://localhost:8000/loss-allocation`

## 请求格式

`POST /evaluate`，Content-Type: `application/json`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `source_voltage` | number | > 0 | 电源电压 (V) |
| `min_voltage` | number | ≥ 0 | 全局最低允许电压 (V) |
| `segments` | array | 至少 1 条 | 线段列表，须构成以 `source` 为根的连通树 |
| `segments[].id` | string | 可选，全局唯一 | 线段标识，缺省自动生成 `seg-<序号>` |
| `segments[].parent` | string | 必填 | 父节点名称，根节点固定为 `source` |
| `segments[].child` | string | 必填 | 子节点名称 |
| `segments[].resistance` | number | ≥ 0 | 线段电阻 (Ω) |
| `segments[].rated_current` | number | > 0 | 线段额定电流 (A) |
| `loads` | array | 可空 | 各节点恒定负载；同一节点多条负载自动累加 |
| `loads[].node` | string | 必填 | 节点名称（须存在于树中，可为 `source`） |
| `loads[].current` | number | ≥ 0 | 恒定负载电流 (A) |
| `loads[].min_voltage` | number | 可选，≥ 0 | 该节点最低允许电压，覆盖全局值 |

### 请求示例

```bash
curl -X POST http://localhost:8000/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ]
  }'
```

### 响应示例（200）

```json
{
  "ok": true,
  "source_voltage": 48.0,
  "min_voltage": 40.0,
  "total_source_current": 15.0,
  "segments": [
    {"segment_id": "s1", "parent": "source", "child": "a", "current": 15.0, "rated_current": 20.0, "resistance": 0.1, "voltage_drop": 1.5, "over_rated": false},
    {"segment_id": "s2", "parent": "a", "child": "b", "current": 5.0, "rated_current": 10.0, "resistance": 0.2, "voltage_drop": 1.0, "over_rated": false},
    {"segment_id": "s3", "parent": "a", "child": "c", "current": 8.0, "rated_current": 15.0, "resistance": 0.05, "voltage_drop": 0.4, "over_rated": false}
  ],
  "nodes": [
    {"node": "source", "voltage": 48.0, "min_voltage": 40.0, "load_current": 0.0, "under_voltage": false},
    {"node": "a", "voltage": 46.5, "min_voltage": 40.0, "load_current": 2.0, "under_voltage": false},
    {"node": "b", "voltage": 45.5, "min_voltage": 40.0, "load_current": 5.0, "under_voltage": false},
    {"node": "c", "voltage": 46.1, "min_voltage": 40.0, "load_current": 8.0, "under_voltage": false}
  ],
  "over_current_segments": [],
  "under_voltage_nodes": []
}
```

- `segments[].current`：该段承载的全部下游电流（自叶向根累计）。
- `nodes[].voltage`：自根向叶逐段扣除 `电流 × 电阻` 压降后的节点电压。
- `over_current_segments`：超额定电流的线段（含 `excess` 超出量）。
- `under_voltage_nodes`：欠压节点（含 `deficit` 欠压差值）。
- `ok`：无超额定且无欠压时为 `true`。

## 增容评估：POST /capacity

`POST /capacity`，Content-Type: `application/json`。请求沿用 `/evaluate` 的全部参数，并额外指定：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `target_node` | string | 必填 | 目标节点，须为已有的非 `source` 节点 |

计算规则：

- 先按 `/evaluate` 的校核逻辑计算基线；若基线已存在超额定电流或欠压，返回 `400 baseline_violation` 并在 `details` 中列出越限位置；
- 新增电流 ΔI 使目标到 `source` 路径上的线段电流同步增加；
- 任一节点的新增压降 = 它与目标节点两条根路径的公共线段电阻之和 × ΔI（兄弟支路经公共上游线段同样计入）；
- 最大 ΔI 取「路径各段额定电流余量」与「各受影响节点欠压余量 ÷ 公共电阻」的最小值；零公共电阻的节点不受影响；
- 结果向下截断至 6 位小数；恰好触限（电流等于额定、电压等于最低允许电压）视为可行。

### 请求示例

```bash
curl -X POST http://localhost:8000/capacity \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ],
    "target_node": "b"
  }'
```

### 响应示例（200）

```json
{
  "target_node": "b",
  "max_additional_current": 5.0,
  "limiting_factors": [
    {"type": "segment", "id": "s1"},
    {"type": "segment", "id": "s2"}
  ]
}
```

- `max_additional_current`：目标节点可追加的最大恒定负载电流 (A)，向下截断至 6 位小数。
- `limiting_factors`：达到上限的限制因素；`type=segment` 表示线段额定电流限制，`type=node` 表示节点欠压限制，`id` 为线段 id 或节点名称。多个因素同时触限时全部列出。

## 未登记负载定位：POST /unregistered-load

`POST /unregistered-load`，Content-Type: `application/json`。请求沿用 `/evaluate` 的全部参数（`min_voltage` 参与校验但不影响定位），并额外附加若干线段的实测电流：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `measurements` | array | 可空 | 线段实测电流列表，测点可不覆盖全树 |
| `measurements[].segment_id` | string | 必填，唯一且存在于树中 | 被测线段标识（重复或不存在返回 `400 invalid_input`） |
| `measurements[].current` | number | ≥ 0 且为有限数 | 实测电流 (A)（负数或 NaN/Infinity 返回 `422`） |

判定规则（假定恰有一个非 `source` 节点存在未登记的正恒定负载）：

- 对每个测点计算 **差值 = 实测电流 − 未舍入基线线段电流**；|差值| ≤ 1e-6 A 视为无差异；
- 出现负差（差值 < -1e-6 A，实测低于基线）→ `inconsistent`；
- 显著正差（差值 > 1e-6 A）须在 1e-6 A 内相互一致，否则 → `inconsistent`；
- 无显著正差 → `undetermined`（未登记负载为零、过小或位于未测支路，无法确定）；
- 差值可确定时，未登记负载电流取显著正差的均值（按六位小数返回），并筛选候选节点：显著正差测点须位于候选节点的祖先路径上、零差测点须不在其祖先路径上；未测支路允许多解（返回多个候选节点），无相容节点 → `inconsistent`。

### 请求示例

```bash
curl -X POST http://localhost:8000/unregistered-load \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ],
    "measurements": [
      {"segment_id": "s1", "current": 18.0},
      {"segment_id": "s3", "current": 8.0}
    ]
  }'
```

### 响应示例（200，located）

```json
{
  "status": "located",
  "load_current": 3.0,
  "candidate_nodes": ["a", "b"],
  "measurements": [
    {"segment_id": "s1", "baseline_current": 15.0, "measured_current": 18.0, "difference": 3.0},
    {"segment_id": "s3", "baseline_current": 8.0, "measured_current": 8.0, "difference": 0.0}
  ],
  "reasons": []
}
```

- `status`：`located`（已定位，返回候选节点与负载电流）、`undetermined`（无显著正差，无法确定）、`inconsistent`（实测与基线矛盾）。
- `load_current`：未登记负载电流估计值 (A)，六位小数；仅 `located` 时给出，其余为 `null`。
- `candidate_nodes`：与全部测点相容的节点（BFS 顺序）；仅 `located` 时非空。上例中 s1 正差 +3A 而 s3 无差异，负载在 s1 下游但不在 c 支路，故候选为 `a`、`b`。
- `measurements`：各测点与未舍入基线的对比（展示值按 6 位小数舍入，判定使用未舍入值）。
- `reasons`：判定为 `inconsistent` 的原因列表，其余情况为空。

## 峰值风险评估：POST /peak-risk

`POST /peak-risk`，Content-Type: `application/json`。请求沿用 `/evaluate` 的全部参数，`loads` 中每条负载可额外声明峰值电流，并额外指定同时升至峰值的负载数量上限：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `loads[].peak_current` | number | 可选，≥ 0 且 ≥ 该负载 `current` | 峰值电流 (A)，缺省等于 `current`（即无峰值） |
| `max_simultaneous_peaks` | integer | 必填，≥ 0 | 允许同时升至峰值的负载最大数量 K |

计算规则：

- 每条负载的峰值增量 ΔI = `peak_current` − `current`；同节点多条负载分别计数（各占一个峰值名额）；
- 对**每条线段**独立选取使其承载电流最大的至多 K 条峰值负载：负载位于该线段下游（线段在其根路径上）时贡献 ΔI，取贡献最大的至多 K 条，**最大承载电流 = 基线电流 + 所选 ΔI 之和**；
- 对**每个节点**独立选取使其电压最低的至多 K 条峰值负载：负载 i 的贡献 = ΔI_i × 它与被校核节点两条根路径的公共线段电阻之和（兄弟支路经公共上游线段同样计入），**最低电压 = 基线电压 − 所选贡献之和**；
- 贡献相同时按负载输入序号（`loads` 数组下标，从 0 开始）选取；贡献为零的负载不计入（不影响最坏值）；不同约束可对应不同峰值组合；
- 越限判定沿用 `/evaluate` 的额定电流、节点最低允许电压（含节点级 `min_voltage` 覆盖）与容差规则：最坏电流 > 额定电流判超额定，最坏电压 < 生效阈值判欠压，恰好触限不算越限。

### 请求示例

```bash
curl -X POST http://localhost:8000/peak-risk \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0, "peak_current": 6.0},
      {"node": "b", "current": 5.0, "peak_current": 9.0},
      {"node": "c", "current": 8.0}
    ],
    "max_simultaneous_peaks": 2
  }'
```

### 响应示例（200）

```json
{
  "ok": false,
  "max_simultaneous_peaks": 2,
  "segments": [
    {"segment_id": "s1", "parent": "source", "child": "a", "current": 23.0, "rated_current": 20.0, "peak_loads": [0, 1], "over_rated": true},
    {"segment_id": "s2", "parent": "a", "child": "b", "current": 9.0, "rated_current": 10.0, "peak_loads": [1], "over_rated": false},
    {"segment_id": "s3", "parent": "a", "child": "c", "current": 8.0, "rated_current": 15.0, "peak_loads": [], "over_rated": false}
  ],
  "nodes": [
    {"node": "source", "voltage": 48.0, "min_voltage": 40.0, "peak_loads": [], "under_voltage": false},
    {"node": "a", "voltage": 45.7, "min_voltage": 40.0, "peak_loads": [0, 1], "under_voltage": false},
    {"node": "b", "voltage": 43.9, "min_voltage": 40.0, "peak_loads": [0, 1], "under_voltage": false},
    {"node": "c", "voltage": 45.3, "min_voltage": 40.0, "peak_loads": [0, 1], "under_voltage": false}
  ],
  "over_current_segments": [
    {"segment_id": "s1", "parent": "source", "child": "a", "current": 23.0, "rated_current": 20.0, "excess": 3.0, "peak_loads": [0, 1]}
  ],
  "under_voltage_nodes": []
}
```

- `segments[].current` / `nodes[].voltage`：该约束在最坏峰值组合下的最大承载电流 / 最低电压（展示值按 6 位小数舍入）。
- `peak_loads`：达到该最坏值时处于峰值的负载输入序号（`loads` 数组下标，从 0 开始，升序）；空数组表示该约束不受峰值影响。上例中负载 2 未声明 `peak_current`（增量为零），故不出现在任何峰值组合中。
- `over_current_segments` / `under_voltage_nodes`：最坏情况下越限的线段 / 节点，各附导致越限的 `peak_loads`。
- `ok`：最坏情况下无超额定且无欠压时为 `true`。

## 故障恢复评估：POST /outage-restoration

`POST /outage-restoration`，Content-Type: `application/json`。请求沿用 `/evaluate` 的全部参数，并额外指定故障线段与若干候选跨接线：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `faulty_segment_id` | string | 必填，须存在于树中 | 故障断开的线段标识 |
| `jumpers` | array | 可空 | 候选跨接线列表，逐条独立评估 |
| `jumpers[].id` | string | 必填，全局唯一 | 跨接线标识，不得与已有线段 id 重复 |
| `jumpers[].supply_node` | string | 必填 | 供电侧节点，须位于 `source` 所在分量 |
| `jumpers[].outage_node` | string | 必填 | 失电侧节点，可为失电分量任意节点 |
| `jumpers[].resistance` | number | ≥ 0 | 跨接线电阻 (Ω) |
| `jumpers[].rated_current` | number | > 0 | 跨接线额定电流 (A) |

计算规则：

- 断开故障线段后，树分裂为 `source` 所在分量（保持供电）与故障线段子节点子树构成的失电分量；
- 每条候选跨接线从供电侧节点引向失电侧节点（电流方向：供电侧 → 失电侧）；失电分量从接入点**重新定向**——接入点到原子树根（故障线段子节点）路径上的线段方向翻转，其余线段方向不变；
- 在恢复后的拓扑上重算全树线段电流与节点电压：故障线段退出运行不计入结果，跨接线作为新线段计入；
- 越限判定沿用 `/evaluate` 的额定电流、节点最低允许电压（含节点级 `min_voltage` 覆盖）与容差规则，恰好触限不算越限；无越限的候选为可恢复（`restorable=true`）；
- 从可恢复候选中选中**最低节点电压余量**（min(节点电压 − 生效阈值)，比较使用未舍入值）最大的方案，平局按输入顺序；无可恢复方案时 `selected_jumper_id` 为 `null`；
- 非法跨接线（id 重复或与已有线段重复、供电侧 / 失电侧节点不存在或不在对应分量）返回 `400 invalid_input`；故障线段不存在同样返回 `400`。

### 请求示例

```bash
curl -X POST http://localhost:8000/outage-restoration \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 30.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0},
      {"id": "s4", "parent": "c",      "child": "d", "resistance": 0.1,  "rated_current": 10.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 3.0},
      {"node": "c", "current": 3.0},
      {"node": "d", "current": 4.0}
    ],
    "faulty_segment_id": "s3",
    "jumpers": [
      {"id": "j1", "supply_node": "a", "outage_node": "c", "resistance": 0.05, "rated_current": 15.0},
      {"id": "j2", "supply_node": "a", "outage_node": "d", "resistance": 0.05, "rated_current": 15.0}
    ]
  }'
```

### 响应示例（200）

```json
{
  "faulty_segment_id": "s3",
  "candidates": [
    {
      "jumper_id": "j1",
      "supply_node": "a",
      "outage_node": "c",
      "restorable": true,
      "min_voltage_margin": 6.05,
      "segments": [
        {"segment_id": "s1", "parent": "source", "child": "a", "current": 12.0, "rated_current": 30.0, "resistance": 0.1, "voltage_drop": 1.2, "over_rated": false},
        {"segment_id": "s2", "parent": "a", "child": "b", "current": 3.0, "rated_current": 10.0, "resistance": 0.2, "voltage_drop": 0.6, "over_rated": false},
        {"segment_id": "s4", "parent": "c", "child": "d", "current": 4.0, "rated_current": 10.0, "resistance": 0.1, "voltage_drop": 0.4, "over_rated": false},
        {"segment_id": "j1", "parent": "a", "child": "c", "current": 7.0, "rated_current": 15.0, "resistance": 0.05, "voltage_drop": 0.35, "over_rated": false}
      ],
      "nodes": [
        {"node": "source", "voltage": 48.0, "min_voltage": 40.0, "load_current": 0.0, "under_voltage": false},
        {"node": "a", "voltage": 46.8, "min_voltage": 40.0, "load_current": 2.0, "under_voltage": false},
        {"node": "b", "voltage": 46.2, "min_voltage": 40.0, "load_current": 3.0, "under_voltage": false},
        {"node": "c", "voltage": 46.45, "min_voltage": 40.0, "load_current": 3.0, "under_voltage": false},
        {"node": "d", "voltage": 46.05, "min_voltage": 40.0, "load_current": 4.0, "under_voltage": false}
      ],
      "over_current_segments": [],
      "under_voltage_nodes": []
    },
    {
      "jumper_id": "j2",
      "supply_node": "a",
      "outage_node": "d",
      "restorable": true,
      "min_voltage_margin": 6.15,
      "segments": [
        {"segment_id": "s1", "parent": "source", "child": "a", "current": 12.0, "rated_current": 30.0, "resistance": 0.1, "voltage_drop": 1.2, "over_rated": false},
        {"segment_id": "s2", "parent": "a", "child": "b", "current": 3.0, "rated_current": 10.0, "resistance": 0.2, "voltage_drop": 0.6, "over_rated": false},
        {"segment_id": "s4", "parent": "d", "child": "c", "current": 3.0, "rated_current": 10.0, "resistance": 0.1, "voltage_drop": 0.3, "over_rated": false},
        {"segment_id": "j2", "parent": "a", "child": "d", "current": 7.0, "rated_current": 15.0, "resistance": 0.05, "voltage_drop": 0.35, "over_rated": false}
      ],
      "nodes": [
        {"node": "source", "voltage": 48.0, "min_voltage": 40.0, "load_current": 0.0, "under_voltage": false},
        {"node": "a", "voltage": 46.8, "min_voltage": 40.0, "load_current": 2.0, "under_voltage": false},
        {"node": "b", "voltage": 46.2, "min_voltage": 40.0, "load_current": 3.0, "under_voltage": false},
        {"node": "d", "voltage": 46.45, "min_voltage": 40.0, "load_current": 4.0, "under_voltage": false},
        {"node": "c", "voltage": 46.15, "min_voltage": 40.0, "load_current": 3.0, "under_voltage": false}
      ],
      "over_current_segments": [],
      "under_voltage_nodes": []
    }
  ],
  "selected_jumper_id": "j2"
}
```

- `candidates[]`：按输入顺序逐候选评估；`segments` 为恢复后的全树线段（故障线段退出运行，跨接线列于末尾），失电分量内方向翻转的线段以新方向展示——上例 j2 接入 d 点，s4 由 c→d 翻转为 d→c，仅承载 c 的 3A。
- `min_voltage_margin`：该候选恢复后全树最低节点电压余量 (V)（展示值按 6 位小数舍入，选中比较使用未舍入值）。
- `restorable`：恢复后无超额定电流且无欠压时为 `true`；`over_current_segments` / `under_voltage_nodes` 列出越限位置。
- `selected_jumper_id`：可恢复候选中最低节点电压余量最大者（平局按输入顺序）；无可恢复方案时为 `null`。上例 j2 的最低余量 6.15V 优于 j1 的 6.05V，故选中 j2。

## 切负荷方案评估：POST /load-shedding

`POST /load-shedding`，Content-Type: `application/json`。请求沿用 `/evaluate` 的全部参数，并额外指定允许切除的负载序号列表：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `sheddable_loads` | array of integer | 缺省为空，至多 16 条 | 可切除负载的输入序号（`loads` 数组下标，**从 0 开始**）；序号不得重复或越界 |

计算规则：

- 序号按 `loads` 从 0 开始，每条负载只能**整条切除**（同节点多条负载分别计数），不在列表中的负载一律保持供电；
- 对可切除列表的任意切除组合（至多 2^16 = 65536 种）重算线段电流与节点电压，等价于在移除被切除负载后的输入上执行 `/evaluate`；节点级 `min_voltage` 覆盖保持不变；
- 越限判定沿用 `/evaluate` 的额定电流、节点最低允许电压（含节点级覆盖）与容差规则，恰好触限不算越限；
- **原网已安全时选空集**（`status=already_safe`），不切除任何负载；
- 存在安全组合时（`status=solved`）选出**被切除电流总和最小**的方案；切除电流总和并列时，依次取**切除条数最少**、**升序序号列表字典序最小**者；
- 全部可切除负载均切除后仍有超额定或欠压时为无解（`status=infeasible`），方案字段为 `null`。

### 请求示例

```bash
curl -X POST http://localhost:8000/load-shedding \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 14.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ],
    "sheddable_loads": [0, 1, 2]
  }'
```

### 响应示例（200，solved）

上例 s1 承载 15A 超过额定 14A；切序号 0（a 点 2A）即可降至 13A，切除电流小于切序号 1（5A）或序号 2（8A），故选中 `[0]`：

```json
{
  "status": "solved",
  "shed_loads": [0],
  "shed_count": 1,
  "shed_current": 2.0,
  "shed_details": [
    {"index": 0, "node": "a", "current": 2.0}
  ],
  "result": {
    "ok": true,
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "total_source_current": 13.0,
    "segments": [
      {"segment_id": "s1", "parent": "source", "child": "a", "current": 13.0, "rated_current": 14.0, "resistance": 0.1, "voltage_drop": 1.3, "over_rated": false},
      {"segment_id": "s2", "parent": "a", "child": "b", "current": 5.0, "rated_current": 10.0, "resistance": 0.2, "voltage_drop": 1.0, "over_rated": false},
      {"segment_id": "s3", "parent": "a", "child": "c", "current": 8.0, "rated_current": 15.0, "resistance": 0.05, "voltage_drop": 0.4, "over_rated": false}
    ],
    "nodes": [
      {"node": "source", "voltage": 48.0, "min_voltage": 40.0, "load_current": 0.0, "under_voltage": false},
      {"node": "a", "voltage": 46.7, "min_voltage": 40.0, "load_current": 0.0, "under_voltage": false},
      {"node": "b", "voltage": 45.7, "min_voltage": 40.0, "load_current": 5.0, "under_voltage": false},
      {"node": "c", "voltage": 46.3, "min_voltage": 40.0, "load_current": 8.0, "under_voltage": false}
    ],
    "over_current_segments": [],
    "under_voltage_nodes": []
  }
}
```

- `status`：`already_safe`（原网已安全，选空集）、`solved`（存在可行方案）、`infeasible`（全部可切除负载切除后仍越限，无解）。
- `shed_loads`：被切除负载的输入序号（升序）；`already_safe` 时为 `[]`，`infeasible` 时为 `null`。
- `shed_count` / `shed_current`：切除条数与被切除电流总和 (A)（展示值按 6 位小数舍入，择优比较使用未舍入值）。
- `shed_details`：各被切除负载的序号、节点、电流，按序号升序。
- `result`：切除后的 `/evaluate` 风格校核结果（重算后的线段电流、节点电压与越限列表）；`already_safe` 时即原网结果，`infeasible` 时为 `null`。

`infeasible` 响应示例：

```json
{
  "status": "infeasible",
  "shed_loads": null,
  "shed_count": null,
  "shed_current": null,
  "shed_details": null,
  "result": null
}
```

## 保护选择性校核：POST /protection-selectivity

`POST /protection-selectivity`，Content-Type: `application/json`。请求沿用 `/evaluate` 的全部参数（`min_voltage` 参与校验但不影响选择性分析），并额外为每条线段配置一条保护曲线、给出故障场景与最小级差：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `curves` | array | 必填，覆盖全部线段 | 各线段保护曲线，线段 id 不得重复或不存在 |
| `curves[].segment_id` | string | 必填 | 曲线所属线段标识；每条线段恰配置一条曲线 |
| `curves[].points` | array | 至少 1 个点 | 曲线点列表，按 `current` **严格递增**（相等或递减返回 `422`） |
| `curves[].points[].current` | number | ≥ 0 且为有限数 | 曲线点电流 (A) |
| `curves[].points[].time` | number | ≥ 0 且为有限数 | 曲线点动作时间 (s) |
| `faults` | array | 至少 1 条 | 故障场景列表，逐个独立评估 |
| `faults[].node` | string | 必填 | 故障节点，须为已有的非 `source` 节点（`source` 或不存在返回 `400`） |
| `faults[].current` | number | > 0 且为有限数 | 故障电流 (A) |
| `min_grading_margin` | number | ≥ 0 且为有限数 | 最小级差 (s)：会动作的上级装置须比距故障最近的装置至少晚该时间 |

曲线求值规则（给定电流 I）：

- I **严格低于首点电流** → 装置不动作；
- I 落在相邻两点之间 → **线性插值**（不要求动作时间单调，反时限、定时限曲线均可）；
- I 达到 / **超过末点电流** → 沿用末点动作时间。

逐故障校核规则（故障电流仅流经故障节点至 `source` 路径上的线段，装置自故障点向 `source` 排序，`level=0` 为故障节点进线段，即距故障最近的装置）：

- **距故障最近的装置必须动作**；不动作时该段记 `nearest_not_operating` 失配；
- 所有**会动作的上级装置**动作时间须 ≥ 最近装置动作时间 + 最小级差；不满足时记 `insufficient_margin` 失配（恰好满足不算失配，按未舍入值比较）；不动作的上级装置不参与判定；
- `operating_sequence` 为会动作装置按动作时间升序排列的线段 id（时间相同时距故障近者在前）；
- `misoperation_segments` 用**基线（正常负荷）线段电流**检查全部线段：基线电流达到曲线首点电流（启动电流）即视为正常负荷下会动作，标出为可能误动的装置；
- 总体结论 `ok=true` 当且仅当**全部故障满足选择性且无误动装置**；时间 / 电流展示值按 6 位小数舍入，判定使用未舍入值。

### 请求示例

```bash
curl -X POST http://localhost:8000/protection-selectivity \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ],
    "curves": [
      {"segment_id": "s1", "points": [{"current": 20.0, "time": 1.0}, {"current": 100.0, "time": 0.4}]},
      {"segment_id": "s2", "points": [{"current": 10.0, "time": 0.6}, {"current": 100.0, "time": 0.2}]},
      {"segment_id": "s3", "points": [{"current": 15.0, "time": 0.6}, {"current": 100.0, "time": 0.2}]}
    ],
    "faults": [
      {"node": "b", "current": 50.0},
      {"node": "c", "current": 30.0}
    ],
    "min_grading_margin": 0.2
  }'
```

### 响应示例（200）

```json
{
  "ok": true,
  "min_grading_margin": 0.2,
  "faults": [
    {
      "node": "b",
      "fault_current": 50.0,
      "selective": true,
      "devices": [
        {"segment_id": "s2", "parent": "a", "child": "b", "level": 0, "operates": true, "operating_time": 0.422222},
        {"segment_id": "s1", "parent": "source", "child": "a", "level": 1, "operates": true, "operating_time": 0.775}
      ],
      "operating_sequence": ["s2", "s1"],
      "miscoordinated_segments": []
    },
    {
      "node": "c",
      "fault_current": 30.0,
      "selective": true,
      "devices": [
        {"segment_id": "s3", "parent": "a", "child": "c", "level": 0, "operates": true, "operating_time": 0.529412},
        {"segment_id": "s1", "parent": "source", "child": "a", "level": 1, "operates": true, "operating_time": 0.925}
      ],
      "operating_sequence": ["s3", "s1"],
      "miscoordinated_segments": []
    }
  ],
  "misoperation_segments": []
}
```

- b 点 50A：s2 插值为 0.6 − 0.4×(50−10)/90 = 0.422222s（最近），s1 插值为 1.0 − 0.6×(50−20)/80 = 0.775s，要求上级不早于 0.422222 + 0.2 = 0.622222s，满足。
- c 点 30A：s3 = 0.529412s，s1 = 0.925s，要求 0.729412s，满足。
- 基线段电流 s1=15、s2=5、s3=8A，均低于各自曲线首点电流（20 / 10 / 15A），无装置在正常负荷下动作，故 `misoperation_segments` 为空、总体 `ok=true`。

失配时 `selective=false`，`miscoordinated_segments` 列出失配线段：

- `reason=nearest_not_operating`：距故障最近的装置未动作（故障电流低于其曲线首点），`operating_time` / `required_time` 均为 `null`；
- `reason=insufficient_margin`：上级装置动作过早，`operating_time` 为其动作时间，`required_time` 为最近装置动作时间 + 最小级差（要求的最早动作时间）。

## 投运顺序评估：POST /startup-order

`POST /startup-order`，Content-Type: `application/json`。请求沿用 `/evaluate` 的配电树与既有负载（`loads` 即已在运的负载，参与基线潮流），并额外指定 1～10 条**待投运负载**：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `startup_loads` | array | 必填，1～10 条 | 待投运负载列表，序号为数组下标（从 0 开始） |
| `startup_loads[].node` | string | 必填，须存在于树中 | 待投运负载所在节点（可为 `source`，不存在返回 `400 invalid_input`） |
| `startup_loads[].steady_current` | number | ≥ 0 且为有限数 | 稳态电流 (A) |
| `startup_loads[].startup_current` | number | ≥ 0 且为有限数，须 ≥ `steady_current` | 启动电流 (A)，小于稳态电流返回 `422` |

计算规则：

- 先按 `/evaluate` 的校核逻辑计算**基线**（仅既有 `loads`）；基线已存在超额定电流或欠压时返回 `400 baseline_violation`，`details` 列出越限位置；
- 每条待投运负载恰好投运一次。第 k 步把一条新负载投入，该步**启动瞬间**计入的电流为：本步新投运负载按 `startup_current`、此前各步已投运负载按 `steady_current`、尚未投运负载不计入（电流为 0）；既有 `loads` 全程在运；
- 每步叠加后用**未舍入**的线段电流与节点电压，按 `/evaluate` 相同规则判定：线段电流 > 额定电流判超额定，节点电压 < 生效最低允许电压（含节点级 `min_voltage` 覆盖）判欠压，**恰好触限允许**；
- 用子集状态深度优先搜索完整可行顺序：每步按待投运负载的输入序号（0 起）递增尝试，不可行的已投运集合记忆化剪枝（可行性对已投运集合单调——新增负载只会增大电流、降低电压）；
- 返回找到的**首个**完整可行顺序，即按输入序号**字典序最小**的顺序；任何顺序都无法完成全部投运时返回 `status=infeasible`；
- 每步返回该步启动瞬间的**最小节点电压**（平局取 BFS 顺序最先的节点）与**最大线段负载率**（线段电流 ÷ 额定电流，平局取线段输入顺序最先者）；电压 / 负载率展示值按 6 位小数舍入，判定与极值比较均使用未舍入值。

### 请求示例

```bash
curl -X POST http://localhost:8000/startup-order \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 21.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ],
    "startup_loads": [
      {"node": "b", "steady_current": 3.0, "startup_current": 4.0},
      {"node": "c", "steady_current": 2.0, "startup_current": 6.0}
    ]
  }'
```

### 响应示例（200，feasible）

上例 s1 基线 15A、额定 21A（仅余 6A）：顺序 `[0,1]` 的第 2 步中 0 号已按稳态 3A 在运，1 号启动再加 6A，s1 = 15+3+6 = 24A 越限；而 `[1,0]` 第 1 步 s1 = 21A 恰触额定，第 2 步 1 号回到稳态 2A、0 号启动加 4A，s1 = 15+2+4 = 21A 仍恰触额定，故可行顺序为 `[1,0]`：

```json
{
  "status": "feasible",
  "order": [1, 0],
  "steps": [
    {
      "step": 1,
      "load_index": 1,
      "node": "c",
      "startup_current": 6.0,
      "min_voltage": 44.9,
      "min_voltage_node": "b",
      "max_loading_ratio": 1.0,
      "max_loading_segment_id": "s1"
    },
    {
      "step": 2,
      "load_index": 0,
      "node": "b",
      "startup_current": 4.0,
      "min_voltage": 44.1,
      "min_voltage_node": "b",
      "max_loading_ratio": 1.0,
      "max_loading_segment_id": "s1"
    }
  ]
}
```

- `status`：`feasible`（存在完整可行顺序）或 `infeasible`（无完整可行顺序）。
- `order`：按输入序号字典序最小的可行投运顺序（`startup_loads` 数组下标，从 0 开始）；`infeasible` 时为 `null`。
- `steps[]`：与 `order` 对应的逐步启动瞬间摘要：`step` 从 1 开始，`load_index` / `node` / `startup_current` 标识本步新投运负载；`min_voltage` / `min_voltage_node` 为全树最小节点电压及取得它的节点（BFS 顺序最先），`max_loading_ratio` / `max_loading_segment_id` 为全树最大线段负载率（电流 ÷ 额定电流）及取得它的线段（输入顺序最先）；`infeasible` 时 `steps` 为 `null`。

`infeasible` 响应示例：

```json
{
  "status": "infeasible",
  "order": null,
  "steps": null
}
```

## 线损归属评估：POST /loss-allocation

`POST /loss-allocation`，Content-Type: `application/json`。请求与 `/evaluate` 完全相同（`min_voltage` 参与校验但不影响线损计算），无额外字段。

计算规则：

- 每段线损 **P = I²R**：I 为该段未舍入的基线线段电流（自叶向根累计的全部下游电流），R 为该段电阻；**总线损**为各段线损之和；
- 按 `loads` 输入序号（从 0 开始）**逐负载归属**：负载 i 在其到 `source` 路径的每段上承担 **负载电流 × 该段总电流 × 该段电阻**——同一段上全部下游负载的归属之和恰等于该段线损，故逐负载归属之和与总线损一致（展示舍入差除外）；
- `source` 节点的负载路径为空、归属为零；同一节点的多条负载按输入序号分别列出；
- 计算与汇总均使用未舍入值，输出展示值保留六位小数；基线存在超额定 / 欠压时不拦截，仍正常返回线损归属。

### 请求示例

```bash
curl -X POST http://localhost:8000/loss-allocation \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ]
  }'
```

### 响应示例（200）

```json
{
  "total_loss": 30.7,
  "segments": [
    {"segment_id": "s1", "parent": "source", "child": "a", "current": 15.0, "resistance": 0.1, "loss": 22.5},
    {"segment_id": "s2", "parent": "a", "child": "b", "current": 5.0, "resistance": 0.2, "loss": 5.0},
    {"segment_id": "s3", "parent": "a", "child": "c", "current": 8.0, "resistance": 0.05, "loss": 3.2}
  ],
  "loads": [
    {"index": 0, "node": "a", "current": 2.0, "loss": 3.0, "segments": [{"segment_id": "s1", "loss": 3.0}]},
    {"index": 1, "node": "b", "current": 5.0, "loss": 12.5, "segments": [{"segment_id": "s2", "loss": 5.0}, {"segment_id": "s1", "loss": 7.5}]},
    {"index": 2, "node": "c", "current": 8.0, "loss": 15.2, "segments": [{"segment_id": "s3", "loss": 3.2}, {"segment_id": "s1", "loss": 12.0}]}
  ]
}
```

- `segments[].loss`：该段线损 I²R (W)。上例 s1 承载 15A，线损 15² × 0.1 = 22.5W；总线损 22.5 + 5.0 + 3.2 = 30.7W。
- `loads[].loss`：该负载承担的线损总和 (W)；`loads[].segments` 为其到 `source` 路径各段上的归属明细（自负载节点向 `source` 排序，`source` 节点负载为空列表）。上例负载 1（b 点 5A）在 s2 上承担 5 × 5 × 0.2 = 5.0W、在 s1 上承担 5 × 15 × 0.1 = 7.5W，合计 12.5W。
- 逐负载归属之和 = 3.0 + 12.5 + 15.2 = 30.7W，与总线损一致。

## 错误说明

所有错误均返回统一结构：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数校验失败",
    "details": [{"field": "source_voltage", "message": "电源电压必须为正数（> 0）"}]
  }
}
```

| 状态码 | code | 触发场景 |
| --- | --- | --- |
| `422` | `validation_error` | 非正电源电压、负电阻、负负载电流、非正额定电流、实测电流为负或不是有限数（NaN/Infinity）、`peak_current` 为负或小于该负载 `current`、`max_simultaneous_peaks` 为负或缺失、`sheddable_loads` 超过 16 条 / 元素非整数、跨接线电阻为负 / 额定电流非正 / 必填字段缺失、保护曲线点电流非严格递增 / 曲线电流或动作时间为负或非有限数 / 曲线点为空 / 曲线或故障列表为空、故障电流非正或非有限数、最小级差为负或非有限数、`startup_loads` 不在 1～10 条范围内 / 待投运负载的稳态电流或启动电流为负或非有限数 / 启动电流小于稳态电流、字段缺失或类型错误等 |
| `400` | `invalid_input` | 无效树（不连通、存在环路、节点多父节点、`source` 作为子节点、线段 id 重复）、负载引用不存在的节点、`target_node` 为 `source` / 不存在于树中、测量线段 id 重复 / 不存在于树中、故障线段不存在于树中，或跨接线 id 重复 / 与已有线段重复、供电侧 / 失电侧节点不存在或不在对应分量、`sheddable_loads` 序号越界（含负数）或重复、保护曲线未覆盖全部线段 / 线段 id 重复 / 曲线线段不存在于树中、故障节点不存在或为 `source`、待投运负载节点不存在于树中 |
| `400` | `baseline_violation` | `/capacity` 与 `/startup-order` 基线校核（仅既有负载）已存在超额定电流或欠压，`details` 列出越限线段与节点 |

注意：`/unregistered-load` 的 `inconsistent` / `undetermined` 是正常分析结论，以 `200` 返回并体现在响应体的 `status` 字段中，不属于上表错误。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --reload --port 8000
```

## 项目结构

```
app/
  main.py    # FastAPI 应用与错误处理
  core.py    # 树校验 + 电流累计 + 压降计算 + 增容评估 + 未登记负载定位 + 峰值风险评估 + 故障恢复评估 + 切负荷方案 + 保护选择性校核 + 投运顺序搜索 + 线损归属
  models.py  # 请求 / 响应模型
tests/
  test_evaluate.py
  test_capacity.py
  test_unregistered_load.py
  test_peak_risk.py
  test_outage_restoration.py
  test_load_shedding.py
  test_protection_selectivity.py
  test_startup_order.py
  test_loss_allocation.py
Dockerfile
docker-compose.yml
requirements.txt
```
