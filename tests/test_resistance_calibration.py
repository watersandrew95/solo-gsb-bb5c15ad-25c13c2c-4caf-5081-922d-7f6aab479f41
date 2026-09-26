"""POST /resistance-calibration 的端到端测试。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 真实电阻（输入 segments 的 resistance 不参与拟合，仅定义树结构）：
#   R1 = 0.1, R2 = 0.2, R3 = 0.05
#
# source --s1--> a --s2--> b
#                |
#                +--s3--> c
#
# 工况 1：48V，负载 a=2, b=5, c=8 → 段电流 (15, 5, 8)
#   Va = 48 - 15×0.1 = 46.5，Vb = 46.5 - 5×0.2 = 45.5，Vc = 46.5 - 8×0.05 = 46.1
# 工况 2：50V，负载 a=1, b=3, c=4 → 段电流 (8, 3, 4)
#   Va = 50 - 8×0.1 = 49.2，Vb = 49.2 - 3×0.2 = 48.6，Vc = 49.2 - 4×0.05 = 49.0


def base_payload(**overrides):
    """两工况、每工况测 a/b/c 三点的满秩辨识输入（输入电阻故意写成占位值）。"""
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 9.9, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 9.9, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 9.9, "rated_current": 15.0},
        ],
        "scenarios": [
            {
                "source_voltage": 48.0,
                "loads": [
                    {"node": "a", "current": 2.0},
                    {"node": "b", "current": 5.0},
                    {"node": "c", "current": 8.0},
                ],
                "measurements": [
                    {"node": "a", "voltage": 46.5},
                    {"node": "b", "voltage": 45.5},
                    {"node": "c", "voltage": 46.1},
                ],
            },
            {
                "source_voltage": 50.0,
                "loads": [
                    {"node": "a", "current": 1.0},
                    {"node": "b", "current": 3.0},
                    {"node": "c", "current": 4.0},
                ],
                "measurements": [
                    {"node": "a", "voltage": 49.2},
                    {"node": "b", "voltage": 48.6},
                    {"node": "c", "voltage": 49.0},
                ],
            },
        ],
        "tolerance": 0.000001,
    }
    payload.update(overrides)
    return payload


def single_segment_payload(measurements, **overrides):
    """单段树 source --s1--> a，两工况各测 a 点一次。"""
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.0, "rated_current": 100.0},
        ],
        "scenarios": [
            {
                "source_voltage": 48.0,
                "loads": [{"node": "a", "current": m["current"]}],
                "measurements": [{"node": "a", "voltage": m["voltage"]}],
            }
            for m in measurements
        ],
        "tolerance": 0.01,
    }
    payload.update(overrides)
    return payload


# ---------- calibrated ----------


def test_calibrated_two_scenarios():
    resp = client.post("/resistance-calibration", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "calibrated"
    assert data["rank"] is None
    # 按线段输入顺序返回估计电阻
    assert [s["segment_id"] for s in data["segments"]] == ["s1", "s2", "s3"]
    assert data["segments"][0]["resistance"] == pytest.approx(0.1)
    assert data["segments"][1]["resistance"] == pytest.approx(0.2)
    assert data["segments"][2]["resistance"] == pytest.approx(0.05)
    assert data["max_residual"] == pytest.approx(0.0, abs=1e-9)


def test_input_resistance_not_used_in_fit():
    """输入线段电阻不参与拟合：占位值 9.9 与真实值输入结果一致。"""
    payload = base_payload()
    for seg, r in zip(payload["segments"], [0.1, 0.2, 0.05]):
        seg["resistance"] = r
    reference = client.post("/resistance-calibration", json=payload).json()

    data = client.post("/resistance-calibration", json=base_payload()).json()
    assert data == reference


def test_single_scenario_full_rank():
    """单工况测满 a/b/c：3×3 系数矩阵满秩，单工况即可辨识。"""
    payload = base_payload(scenarios=base_payload()["scenarios"][:1])
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "calibrated"
    assert [s["resistance"] for s in data["segments"]] == [
        pytest.approx(0.1),
        pytest.approx(0.2),
        pytest.approx(0.05),
    ]
    assert data["max_residual"] == pytest.approx(0.0, abs=1e-9)


def test_zero_tolerance_passes_on_exact_data():
    """精确数据残差约为 0：tolerance=0 仍 calibrated（EPS 内视为触限）。"""
    data = client.post("/resistance-calibration", json=base_payload(tolerance=0.0)).json()
    assert data["status"] == "calibrated"


def test_resistance_rounded_to_six_decimals():
    """估计电阻保留六位小数：真实 R = 1/3 Ω。"""
    payload = single_segment_payload([{"current": 3.0, "voltage": 47.0}], tolerance=0.001)
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "calibrated"
    assert data["segments"][0]["resistance"] == 0.333333
    assert data["max_residual"] == pytest.approx(0.0, abs=1e-9)


def test_nnls_clamps_negative_solution():
    """无约束最小二乘解为负时，非负约束把电阻钳到 0。

    工况 1：I=1A，压降 0.1V；工况 2：I=10A，实测电压高于电源（压降 -0.5V）。
    无约束解 R = (1×0.1 + 10×(-0.5)) / (1 + 100) ≈ -0.0485 < 0，NNLS 解为 R = 0，
    残差 |0-0.1| 与 |0+0.5|，最大 0.5。
    """
    payload = single_segment_payload(
        [{"current": 1.0, "voltage": 47.9}, {"current": 10.0, "voltage": 48.5}],
        tolerance=0.6,
    )
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "calibrated"
    assert data["segments"][0]["resistance"] == 0.0
    assert data["max_residual"] == pytest.approx(0.5)


# ---------- undetermined ----------


def test_rank_deficient_measurements_undetermined():
    """单工况只测 a、c：s2 列全零，列秩 2 < 3。"""
    scenario = base_payload()["scenarios"][0]
    scenario["measurements"] = [m for m in scenario["measurements"] if m["node"] != "b"]
    payload = base_payload(scenarios=[scenario])
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "undetermined"
    assert data["rank"] == 2
    assert data["segments"] is None
    assert data["max_residual"] is None


def test_zero_current_column_undetermined():
    """c 支路无负载：s3 在各工况电流均为 0，该列无法辨识。"""
    scenario = base_payload()["scenarios"][0]
    scenario["loads"] = [load for load in scenario["loads"] if load["node"] != "c"]
    # 段电流 (7, 5, 0)：Va = 48 - 7×0.1 = 47.3，Vb = 46.3，Vc = 47.3（s3 无电流）
    scenario["measurements"] = [
        {"node": "a", "voltage": 47.3},
        {"node": "b", "voltage": 46.3},
        {"node": "c", "voltage": 47.3},
    ]
    payload = base_payload(scenarios=[scenario])
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "undetermined"
    assert data["rank"] == 2


def test_no_measurements_undetermined():
    """所有工况均无测点：系数矩阵为空，秩为 0。"""
    scenarios = base_payload()["scenarios"]
    for scenario in scenarios:
        scenario["measurements"] = []
    data = client.post("/resistance-calibration", json=base_payload(scenarios=scenarios)).json()

    assert data["status"] == "undetermined"
    assert data["rank"] == 0


# ---------- inconsistent ----------


def test_inconsistent_measurements():
    """两工况单段：真实 R=0.1，工况 2 测点压降偏出 0.5V。

    方程 10R = 1.0 与 20R = 2.5，最小二乘解 R = 0.12，
    残差 |10×0.12 − 1.0| = 0.2 与 |20×0.12 − 2.5| = 0.1，最大 0.2 > 容限 0.01。
    """
    payload = single_segment_payload(
        [{"current": 10.0, "voltage": 47.0}, {"current": 20.0, "voltage": 45.5}]
    )
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "inconsistent"
    assert data["max_residual"] == pytest.approx(0.2)
    assert data["segments"] is None
    assert data["rank"] is None


def test_residual_exactly_at_tolerance_calibrated():
    """最大残差恰好等于容限：视为触限通过（与现有 EPS 规则一致）。"""
    payload = single_segment_payload(
        [{"current": 10.0, "voltage": 47.0}, {"current": 20.0, "voltage": 45.5}],
        tolerance=0.2,
    )
    data = client.post("/resistance-calibration", json=payload).json()

    assert data["status"] == "calibrated"
    assert data["segments"][0]["resistance"] == pytest.approx(0.12)
    assert data["max_residual"] == pytest.approx(0.2)


# ---------- 测点校验（400 invalid_input） ----------


def test_duplicate_measurement_node_rejected():
    payload = base_payload()
    payload["scenarios"][0]["measurements"].append({"node": "a", "voltage": 46.4})
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("重复" in d["message"] for d in error["details"])


def test_same_node_in_different_scenarios_allowed():
    """同一节点在不同工况重复测量是正常用法（base_payload 即如此）。"""
    resp = client.post("/resistance-calibration", json=base_payload())
    assert resp.status_code == 200
    assert resp.json()["status"] == "calibrated"


def test_unknown_measurement_node_rejected():
    payload = base_payload()
    payload["scenarios"][1]["measurements"].append({"node": "ghost", "voltage": 1.0})
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在" in d["message"] for d in error["details"])


def test_source_measurement_node_rejected():
    payload = base_payload()
    payload["scenarios"][0]["measurements"].append({"node": "source", "voltage": 48.0})
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("source" in d["message"] for d in error["details"])


# ---------- 读数与容限校验（422 validation_error） ----------


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_voltage_rejected(bad):
    payload = base_payload()
    payload["scenarios"][0]["measurements"][0]["voltage"] = bad
    # httpx 的 json= 不允许非有限浮点，改用原始请求体发送 Infinity / NaN 字面量
    resp = client.post(
        "/resistance-calibration",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("实测电压" in d["message"] for d in error["details"])


def test_negative_tolerance_rejected():
    resp = client.post("/resistance-calibration", json=base_payload(tolerance=-0.1))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("残差容限不能为负" in d["message"] for d in error["details"])


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_tolerance_rejected(bad):
    payload = base_payload(tolerance=bad)
    resp = client.post(
        "/resistance-calibration",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("残差容限" in d["message"] for d in error["details"])


def test_missing_tolerance_rejected():
    payload = base_payload()
    del payload["tolerance"]
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 422
    assert any("残差容限" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_scenarios_rejected():
    resp = client.post("/resistance-calibration", json=base_payload(scenarios=[]))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("工况列表" in d["message"] for d in error["details"])


def test_missing_measurement_node_rejected():
    payload = base_payload()
    payload["scenarios"][0]["measurements"] = [{"voltage": 46.5}]
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 422
    assert any("测点节点" in d["message"] for d in resp.json()["error"]["details"])


# ---------- 沿用树与负载校验 ----------


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


def test_unknown_load_node_rejected_with_scenario_prefix():
    payload = base_payload()
    payload["scenarios"][1]["loads"].append({"node": "ghost", "current": 1.0})
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("工况 2" in d["message"] and "不存在" in d["message"] for d in error["details"])


def test_negative_scenario_load_rejected():
    payload = base_payload()
    payload["scenarios"][0]["loads"][0]["current"] = -1.0
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 422
    assert any("负载电流不能为负" in d["message"] for d in resp.json()["error"]["details"])


def test_non_positive_scenario_source_voltage_rejected():
    payload = base_payload()
    payload["scenarios"][0]["source_voltage"] = 0.0
    resp = client.post("/resistance-calibration", json=payload)
    assert resp.status_code == 422
    assert any("电源电压" in d["message"] for d in resp.json()["error"]["details"])
