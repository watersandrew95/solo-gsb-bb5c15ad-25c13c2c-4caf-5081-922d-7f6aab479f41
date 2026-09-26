"""POST /resistance-calibration 的端到端测试。

辨识用树（真实电阻 R1=0.1, R2=0.2, R3=0.05，输入电阻不参与拟合）：

source --s1--> a --s2--> b
               |
               +--s3--> c

工况一负载 a=2A, b=5A, c=8A → 段电流 s1=15, s2=5, s3=8，
真实节点电压 Va=48-15×0.1=46.5, Vb=46.5-5×0.2=45.5, Vc=46.5-8×0.05=46.1。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 输入线段电阻故意写成与真实值（0.1/0.2/0.05）无关的 9.9，验证其不参与拟合
SEGMENTS = [
    {"id": "s1", "parent": "source", "child": "a", "resistance": 9.9, "rated_current": 20.0},
    {"id": "s2", "parent": "a", "child": "b", "resistance": 9.9, "rated_current": 10.0},
    {"id": "s3", "parent": "a", "child": "c", "resistance": 9.9, "rated_current": 15.0},
]
LOADS = [
    {"node": "a", "current": 2.0},
    {"node": "b", "current": 5.0},
    {"node": "c", "current": 8.0},
]
MEASUREMENTS = [
    {"node": "a", "voltage": 46.5},
    {"node": "b", "voltage": 45.5},
    {"node": "c", "voltage": 46.1},
]


def base_payload(**overrides):
    payload = {
        "segments": [dict(seg) for seg in SEGMENTS],
        "scenarios": [
            {
                "source_voltage": 48.0,
                "loads": [dict(load) for load in LOADS],
                "measurements": [dict(m) for m in MEASUREMENTS],
            }
        ],
        "tolerance": 0.001,
    }
    payload.update(overrides)
    return payload


# ---------- calibrated ----------


def test_calibrated_single_scenario():
    resp = client.post("/resistance-calibration", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "calibrated"
    assert data["matrix_rank"] is None
    assert data["max_residual"] == pytest.approx(0.0, abs=1e-9)

    # 各段估计电阻按线段输入顺序给出，且与输入电阻（9.9）无关
    assert [s["segment_id"] for s in data["segments"]] == ["s1", "s2", "s3"]
    assert [s["parent"] for s in data["segments"]] == ["source", "a", "a"]
    assert [s["child"] for s in data["segments"]] == ["a", "b", "c"]
    resistances = {s["segment_id"]: s["resistance"] for s in data["segments"]}
    assert resistances["s1"] == pytest.approx(0.1)
    assert resistances["s2"] == pytest.approx(0.2)
    assert resistances["s3"] == pytest.approx(0.05)


def test_calibrated_joint_scenarios():
    """两工况联合辨识：不同负载下的测点方程联立求解。"""
    scenarios = [
        {  # 段电流 (15, 5, 8)
            "source_voltage": 48.0,
            "loads": LOADS,
            "measurements": MEASUREMENTS,
        },
        {  # 仅 b=10A：段电流 (10, 10, 0)，Va=47, Vb=45, Vc=47
            "source_voltage": 48.0,
            "loads": [{"node": "b", "current": 10.0}],
            "measurements": [
                {"node": "a", "voltage": 47.0},
                {"node": "b", "voltage": 45.0},
                {"node": "c", "voltage": 47.0},
            ],
        },
    ]
    resp = client.post("/resistance-calibration", json=base_payload(scenarios=scenarios))
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "calibrated"
    resistances = {s["segment_id"]: s["resistance"] for s in data["segments"]}
    assert resistances == {
        "s1": pytest.approx(0.1),
        "s2": pytest.approx(0.2),
        "s3": pytest.approx(0.05),
    }
    assert data["max_residual"] == pytest.approx(0.0, abs=1e-9)


def test_calibrated_deep_chain_requires_multiple_scenarios():
    """链形树单测点单工况只有 1 个方程，多工况联立后满秩。"""
    segments = [
        {"id": "s1", "parent": "source", "child": "a", "resistance": 0.0, "rated_current": 20.0},
        {"id": "s2", "parent": "a", "child": "b", "resistance": 0.0, "rated_current": 20.0},
        {"id": "s3", "parent": "b", "child": "c", "resistance": 0.0, "rated_current": 20.0},
    ]
    # 真实电阻 (0.1, 0.2, 0.3)；均只测末端 c
    scenarios = [
        {  # c=4A：段电流 (4,4,4)，Vc = 48 - 4×0.6 = 45.6
            "source_voltage": 48.0,
            "loads": [{"node": "c", "current": 4.0}],
            "measurements": [{"node": "c", "voltage": 45.6}],
        },
        {  # b=6A：段电流 (6,6,0)，Vc = Vb = 48 - 6×0.3 = 46.2
            "source_voltage": 48.0,
            "loads": [{"node": "b", "current": 6.0}],
            "measurements": [{"node": "c", "voltage": 46.2}],
        },
        {  # a=8A：段电流 (8,0,0)，Vc = Va = 48 - 8×0.1 = 47.2
            "source_voltage": 48.0,
            "loads": [{"node": "a", "current": 8.0}],
            "measurements": [{"node": "c", "voltage": 47.2}],
        },
    ]
    payload = {"segments": segments, "scenarios": scenarios, "tolerance": 0.0}
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "calibrated"
    resistances = {s["segment_id"]: s["resistance"] for s in data["segments"]}
    assert resistances == {
        "s1": pytest.approx(0.1),
        "s2": pytest.approx(0.2),
        "s3": pytest.approx(0.3),
    }


def test_nnls_clamps_negative_solution_to_zero():
    """无约束最小二乘给出负电阻时，非负解将其钳到 0 并重解其余段。"""
    segments = [
        {"id": "s1", "parent": "source", "child": "a", "resistance": 0.0, "rated_current": 20.0},
        {"id": "s2", "parent": "a", "child": "b", "resistance": 0.0, "rated_current": 20.0},
    ]
    # b=10A：段电流 (10,10)。方程 10R1=2.0、10R1+10R2=0.5 → 无约束解 R2=-0.15；
    # 非负解 R2=0、R1=(2.0+0.5)/20=0.125，两个测点残差均为 0.75
    scenarios = [
        {
            "source_voltage": 48.0,
            "loads": [{"node": "b", "current": 10.0}],
            "measurements": [
                {"node": "a", "voltage": 46.0},
                {"node": "b", "voltage": 47.5},
            ],
        }
    ]
    payload = {"segments": segments, "scenarios": scenarios, "tolerance": 1.0}
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "calibrated"
    resistances = {s["segment_id"]: s["resistance"] for s in data["segments"]}
    assert resistances["s1"] == pytest.approx(0.125)
    assert resistances["s2"] == pytest.approx(0.0)
    assert data["max_residual"] == pytest.approx(0.75)
    assert all(s["resistance"] >= 0.0 for s in data["segments"])


def test_zero_resistance_segment_identified():
    """真实电阻为 0 的线段可被辨识为 0（非负约束边界）。"""
    # 真实电阻 (0.1, 0.0, 0.05)：Va=46.5, Vb=Va=46.5, Vc=46.1
    measurements = [
        {"node": "a", "voltage": 46.5},
        {"node": "b", "voltage": 46.5},
        {"node": "c", "voltage": 46.1},
    ]
    scenarios = [{"source_voltage": 48.0, "loads": LOADS, "measurements": measurements}]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios)
    ).json()

    assert data["status"] == "calibrated"
    resistances = {s["segment_id"]: s["resistance"] for s in data["segments"]}
    assert resistances["s2"] == pytest.approx(0.0, abs=1e-9)


def test_residual_within_tolerance_calibrated():
    """满秩、残差未超容限：返回折中估计与最大残差（六位小数）。"""
    # 工况 2 的 a 测点电压被污染为 46.0（方程 15R1=2.0，与工况 1 的 15R1=1.5 矛盾）
    scenarios = [
        {"source_voltage": 48.0, "loads": LOADS, "measurements": MEASUREMENTS},
        {
            "source_voltage": 48.0,
            "loads": LOADS,
            "measurements": [
                {"node": "a", "voltage": 46.0},
                {"node": "b", "voltage": 45.5},
                {"node": "c", "voltage": 46.1},
            ],
        },
    ]
    # 最小二乘折中：R1=(1.5+2.0)/30≈0.116667, R2=0.15, R3=0.01875，最大残差 0.25
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios, tolerance=0.5)
    ).json()

    assert data["status"] == "calibrated"
    resistances = {s["segment_id"]: s["resistance"] for s in data["segments"]}
    assert resistances["s1"] == pytest.approx(0.116667)
    assert resistances["s2"] == pytest.approx(0.15)
    assert resistances["s3"] == pytest.approx(0.01875)
    assert data["max_residual"] == pytest.approx(0.25)


def test_residual_exactly_at_tolerance_calibrated():
    """最大残差恰好等于容限不算超过，仍 calibrated。"""
    scenarios = [
        {"source_voltage": 48.0, "loads": LOADS, "measurements": MEASUREMENTS},
        {
            "source_voltage": 48.0,
            "loads": LOADS,
            "measurements": [
                {"node": "a", "voltage": 46.0},
                {"node": "b", "voltage": 45.5},
                {"node": "c", "voltage": 46.1},
            ],
        },
    ]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios, tolerance=0.25)
    ).json()
    assert data["status"] == "calibrated"
    assert data["max_residual"] == pytest.approx(0.25)


def test_same_node_measured_in_multiple_scenarios_allowed():
    """同一节点在不同工况重复测量不算重复测点。"""
    scenarios = [
        {"source_voltage": 48.0, "loads": LOADS, "measurements": MEASUREMENTS},
        {
            "source_voltage": 48.0,
            "loads": [{"node": "b", "current": 10.0}],
            "measurements": [
                {"node": "a", "voltage": 47.0},
                {"node": "b", "voltage": 45.0},
                {"node": "c", "voltage": 47.0},
            ],
        },
    ]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios)
    ).json()
    assert data["status"] == "calibrated"


def test_segment_ids_auto_generated():
    payload = base_payload()
    for seg in payload["segments"]:
        del seg["id"]
    data = client.post("/resistance-calibration", json=payload).json()
    assert data["status"] == "calibrated"
    assert [s["segment_id"] for s in data["segments"]] == ["seg-1", "seg-2", "seg-3"]


# ---------- undetermined ----------


def test_undetermined_single_measurement():
    """仅测 a：1 个方程对 3 个未知量，列秩 1 < 3。"""
    scenarios = [
        {"source_voltage": 48.0, "loads": LOADS, "measurements": [{"node": "a", "voltage": 46.5}]}
    ]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios)
    ).json()

    assert data["status"] == "undetermined"
    assert data["matrix_rank"] == 1
    assert data["segments"] is None
    assert data["max_residual"] is None


def test_undetermined_zero_current_segment():
    """b 支路无负载 → s2 电流恒为 0，对应列全零，列秩 2 < 3。"""
    loads = [{"node": "a", "current": 2.0}, {"node": "c", "current": 8.0}]  # I=(10,0,8)
    measurements = [
        {"node": "a", "voltage": 47.0},  # 48 - 10×0.1
        {"node": "b", "voltage": 47.0},  # s2 无电流，Vb = Va
        {"node": "c", "voltage": 46.6},  # 47 - 8×0.05
    ]
    scenarios = [{"source_voltage": 48.0, "loads": loads, "measurements": measurements}]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios)
    ).json()

    assert data["status"] == "undetermined"
    assert data["matrix_rank"] == 2


def test_undetermined_no_measurements():
    scenarios = [{"source_voltage": 48.0, "loads": LOADS, "measurements": []}]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios)
    ).json()

    assert data["status"] == "undetermined"
    assert data["matrix_rank"] == 0


def test_undetermined_dependent_equations():
    """测点方程线性相关（链形树只测末端，工况电流成比例）→ 列秩不足。"""
    segments = [
        {"id": "s1", "parent": "source", "child": "a", "resistance": 0.0, "rated_current": 20.0},
        {"id": "s2", "parent": "a", "child": "b", "resistance": 0.0, "rated_current": 20.0},
    ]
    # 两工况电流分别为 (4,4) 与 (8,8)，方程行 [4,4] 与 [8,8] 线性相关
    scenarios = [
        {
            "source_voltage": 48.0,
            "loads": [{"node": "b", "current": 4.0}],
            "measurements": [{"node": "b", "voltage": 46.4}],  # 48 - 4×0.4
        },
        {
            "source_voltage": 48.0,
            "loads": [{"node": "b", "current": 8.0}],
            "measurements": [{"node": "b", "voltage": 44.8}],  # 48 - 8×0.4
        },
    ]
    payload = {"segments": segments, "scenarios": scenarios, "tolerance": 0.1}
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "undetermined"
    assert data["matrix_rank"] == 1


# ---------- inconsistent ----------


def test_inconsistent_residual_exceeds_tolerance():
    """满秩但最大测点电压残差 0.25 超过容限 0.1 → inconsistent。"""
    scenarios = [
        {"source_voltage": 48.0, "loads": LOADS, "measurements": MEASUREMENTS},
        {
            "source_voltage": 48.0,
            "loads": LOADS,
            "measurements": [
                {"node": "a", "voltage": 46.0},  # 与工况 1 的 46.5 矛盾
                {"node": "b", "voltage": 45.5},
                {"node": "c", "voltage": 46.1},
            ],
        },
    ]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios, tolerance=0.1)
    ).json()

    assert data["status"] == "inconsistent"
    assert data["max_residual"] == pytest.approx(0.25)
    assert data["segments"] is None
    assert data["matrix_rank"] is None


def test_inconsistent_impossible_voltage_rise():
    """单工况下测点电压高于电源（压降为负），非负电阻无法拟合。"""
    measurements = [
        {"node": "a", "voltage": 49.0},  # 高于电源 48V
        {"node": "b", "voltage": 49.0},
        {"node": "c", "voltage": 49.0},
    ]
    scenarios = [{"source_voltage": 48.0, "loads": LOADS, "measurements": measurements}]
    data = client.post(
        "/resistance-calibration", json=base_payload(scenarios=scenarios, tolerance=0.1)
    ).json()

    assert data["status"] == "inconsistent"
    # 非负最小二乘最优为全零电阻，残差 = 各测点压降 -1.0
    assert data["max_residual"] == pytest.approx(1.0)


# ---------- 测点校验（400 invalid_input） ----------


def test_duplicate_measurement_node_rejected():
    scenarios = [
        {
            "source_voltage": 48.0,
            "loads": LOADS,
            "measurements": [
                {"node": "a", "voltage": 46.5},
                {"node": "a", "voltage": 46.4},
            ],
        }
    ]
    resp = client.post("/resistance-calibration", json=base_payload(scenarios=scenarios))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("重复" in d["message"] for d in error["details"])


def test_unknown_measurement_node_rejected():
    scenarios = [
        {
            "source_voltage": 48.0,
            "loads": LOADS,
            "measurements": [{"node": "ghost", "voltage": 46.5}],
        }
    ]
    resp = client.post("/resistance-calibration", json=base_payload(scenarios=scenarios))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在" in d["message"] for d in error["details"])


def test_source_measurement_node_rejected():
    scenarios = [
        {
            "source_voltage": 48.0,
            "loads": LOADS,
            "measurements": [{"node": "source", "voltage": 48.0}],
        }
    ]
    resp = client.post("/resistance-calibration", json=base_payload(scenarios=scenarios))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("根节点" in d["message"] for d in error["details"])


def test_unknown_load_node_rejected():
    scenarios = [
        {
            "source_voltage": 48.0,
            "loads": [{"node": "ghost", "current": 1.0}],
            "measurements": MEASUREMENTS,
        }
    ]
    resp = client.post("/resistance-calibration", json=base_payload(scenarios=scenarios))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
    assert any("不存在" in d["message"] for d in resp.json()["error"]["details"])


def test_invalid_tree_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不连通" in d["message"] for d in error["details"])


# ---------- 请求校验（422 validation_error） ----------


def test_negative_tolerance_rejected():
    resp = client.post("/resistance-calibration", json=base_payload(tolerance=-0.1))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("残差容限不能为负" in d["message"] for d in error["details"])


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_tolerance_rejected(bad):
    payload = base_payload(tolerance=bad)
    # httpx 的 json= 不允许非有限浮点，改用原始请求体发送 Infinity / NaN 字面量
    resp = client.post(
        "/resistance-calibration",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("残差容限必须为有限数值" in d["message"] for d in error["details"])


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_measurement_voltage_rejected(bad):
    payload = base_payload()
    payload["scenarios"][0]["measurements"][0]["voltage"] = bad
    resp = client.post(
        "/resistance-calibration",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("实测电压必须为有限数值" in d["message"] for d in error["details"])


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_non_finite_source_voltage_rejected(bad):
    payload = base_payload()
    payload["scenarios"][0]["source_voltage"] = bad
    resp = client.post(
        "/resistance-calibration",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_non_positive_source_voltage_rejected():
    payload = base_payload()
    payload["scenarios"][0]["source_voltage"] = 0.0
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert any("电源电压必须为正数" in d["message"] for d in error["details"])


def test_empty_scenarios_rejected():
    resp = client.post("/resistance-calibration", json=base_payload(scenarios=[]))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("工况列表不能为空" in d["message"] for d in error["details"])


def test_negative_load_current_rejected():
    payload = base_payload()
    payload["scenarios"][0]["loads"][0]["current"] = -1.0
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert any("负载电流不能为负" in d["message"] for d in error["details"])


def test_missing_tolerance_rejected():
    payload = base_payload()
    del payload["tolerance"]
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 422
    assert any("残差容限" in d["message"] for d in resp.json()["error"]["details"])
