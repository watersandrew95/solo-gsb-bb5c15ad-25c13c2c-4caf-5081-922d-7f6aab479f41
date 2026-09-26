"""POST /protection-selectivity 的端到端测试。

测试用树（与 test_evaluate 相同的三层小树）：

source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                          |
                          +--s3(0.05Ω, 15A)--> c

负载：a=2A, b=5A, c=8A → 基线线段电流 s1=15, s2=5, s3=8

基线曲线（启动电流均高于基线电流，故无误动；反时限曲线，电流越大动作越快）：
  s1: (20A, 1.0s) -- (100A, 0.4s)
  s2: (10A, 0.6s) -- (100A, 0.2s)
  s3: (15A, 0.6s) -- (100A, 0.2s)
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
        ],
        "loads": [
            {"node": "a", "current": 2.0},
            {"node": "b", "current": 5.0},
            {"node": "c", "current": 8.0},
        ],
        "curves": [
            {"segment_id": "s1", "points": [{"current": 20.0, "time": 1.0}, {"current": 100.0, "time": 0.4}]},
            {"segment_id": "s2", "points": [{"current": 10.0, "time": 0.6}, {"current": 100.0, "time": 0.2}]},
            {"segment_id": "s3", "points": [{"current": 15.0, "time": 0.6}, {"current": 100.0, "time": 0.2}]},
        ],
        "faults": [{"node": "b", "current": 50.0}],
        "min_grading_margin": 0.2,
    }
    payload.update(overrides)
    return payload


def by_id(items, key):
    return {item[key]: item for item in items}


# ---------- 选择性满足 ----------


def test_selective_fault_returns_operating_order():
    """b 点 50A：s2 插值 0.422222s（最近），s1 插值 0.775s；要求 ≥ 0.622222s，满足。"""
    resp = client.post("/protection-selectivity", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert data["min_grading_margin"] == pytest.approx(0.2)
    assert data["misoperation_segments"] == []

    assert len(data["faults"]) == 1
    fault = data["faults"][0]
    assert fault["node"] == "b"
    assert fault["fault_current"] == pytest.approx(50.0)
    assert fault["selective"] is True
    assert fault["miscoordinated_segments"] == []

    devices = by_id(fault["devices"], "segment_id")
    assert [d["segment_id"] for d in fault["devices"]] == ["s2", "s1"]  # 自故障点向 source
    s2, s1 = devices["s2"], devices["s1"]
    assert s2["level"] == 0 and s2["operates"] is True
    assert s2["operating_time"] == pytest.approx(0.422222)  # 0.6 - 0.4*40/90
    assert s1["level"] == 1 and s1["operates"] is True
    assert s1["operating_time"] == pytest.approx(0.775)  # 1.0 - 0.6*30/80

    assert fault["operating_sequence"] == ["s2", "s1"]


def test_fault_at_branch_root_has_single_device():
    """a 点 50A：路径上只有 s1，最近装置动作即满足。"""
    payload = base_payload(faults=[{"node": "a", "current": 50.0}])
    data = client.post("/protection-selectivity", json=payload).json()
    fault = data["faults"][0]
    assert [d["segment_id"] for d in fault["devices"]] == ["s1"]
    assert fault["devices"][0]["operating_time"] == pytest.approx(0.775)
    assert fault["operating_sequence"] == ["s1"]
    assert fault["selective"] is True
    assert data["ok"] is True


def test_multiple_faults_evaluated_independently():
    """c 点 30A：s3=0.529412s，s1=0.925s，要求 0.729412s，同样满足。"""
    payload = base_payload(
        faults=[
            {"node": "b", "current": 50.0},
            {"node": "c", "current": 30.0},
        ]
    )
    data = client.post("/protection-selectivity", json=payload).json()
    assert [f["node"] for f in data["faults"]] == ["b", "c"]
    fc = data["faults"][1]
    devices = by_id(fc["devices"], "segment_id")
    assert devices["s3"]["operating_time"] == pytest.approx(0.529412)  # 0.6 - 0.4*15/85
    assert devices["s1"]["operating_time"] == pytest.approx(0.925)  # 1.0 - 0.6*10/80
    assert fc["operating_sequence"] == ["s3", "s1"]
    assert fc["selective"] is True


# ---------- 曲线求值规则 ----------


def test_curve_first_point_last_point_and_below_pickup():
    """恰为首点用首点时间；超过末点沿用末点时间；低于首点不动作。"""
    payload = base_payload(
        faults=[
            {"node": "b", "current": 10.0},    # 恰为 s2 首点
            {"node": "b", "current": 150.0},   # 超过 s2/s1 末点
            {"node": "b", "current": 5.0},     # 低于 s2 首点
        ],
        min_grading_margin=0.2,
    )
    data = client.post("/protection-selectivity", json=payload).json()
    f_first, f_last, f_below = data["faults"]

    devices_first = by_id(f_first["devices"], "segment_id")
    assert devices_first["s2"]["operates"] is True
    assert devices_first["s2"]["operating_time"] == pytest.approx(0.6)
    assert devices_first["s1"]["operates"] is False
    assert devices_first["s1"]["operating_time"] is None
    assert f_first["operating_sequence"] == ["s2"]
    assert f_first["selective"] is True  # 上级不动作不构成失配

    devices_last = by_id(f_last["devices"], "segment_id")
    assert devices_last["s2"]["operating_time"] == pytest.approx(0.2)  # 沿用末点
    assert devices_last["s1"]["operating_time"] == pytest.approx(0.4)
    # 上级 0.4 恰为最近 0.2 + 级差 0.2，恰好满足不算失配
    assert f_last["selective"] is True

    devices_below = by_id(f_below["devices"], "segment_id")
    assert devices_below["s2"]["operates"] is False
    assert devices_below["s1"]["operates"] is False
    assert f_below["operating_sequence"] == []


def test_single_point_curve_is_step_definite_time():
    """单点曲线：低于该电流不动作，达到即该时间（等于首点也是末点）。"""
    payload = base_payload(
        curves=[
            {"segment_id": "s1", "points": [{"current": 20.0, "time": 1.0}]},
            {"segment_id": "s2", "points": [{"current": 10.0, "time": 0.5}]},
            {"segment_id": "s3", "points": [{"current": 15.0, "time": 0.5}]},
        ],
    )
    data = client.post("/protection-selectivity", json=payload).json()
    devices = by_id(data["faults"][0]["devices"], "segment_id")
    assert devices["s2"]["operating_time"] == pytest.approx(0.5)
    assert devices["s1"]["operating_time"] == pytest.approx(1.0)
    assert data["faults"][0]["selective"] is True
    assert data["ok"] is True


# ---------- 失配 ----------


def test_insufficient_grading_margin_is_miscoordination():
    """级差 0.4s：要求 s1 ≥ 0.822222s，实际 0.775s，s1 失配。"""
    payload = base_payload(min_grading_margin=0.4)
    data = client.post("/protection-selectivity", json=payload).json()
    fault = data["faults"][0]
    assert fault["selective"] is False
    assert data["ok"] is False
    mis = fault["miscoordinated_segments"]
    assert len(mis) == 1
    assert mis[0]["segment_id"] == "s1"
    assert mis[0]["reason"] == "insufficient_margin"
    assert mis[0]["operating_time"] == pytest.approx(0.775)
    assert mis[0]["required_time"] == pytest.approx(0.822222)


def test_zero_margin_requires_upstream_not_faster():
    """级差为 0：上级不早于最近装置即可。"""
    curves = [
        {"segment_id": "s1", "points": [{"current": 20.0, "time": 0.5}]},
        {"segment_id": "s2", "points": [{"current": 10.0, "time": 0.5}]},
        {"segment_id": "s3", "points": [{"current": 15.0, "time": 0.5}]},
    ]
    ok = client.post(
        "/protection-selectivity", json=base_payload(curves=curves, min_grading_margin=0.0)
    ).json()
    fault = ok["faults"][0]
    assert fault["selective"] is True  # 0.5 ≥ 0.5 + 0
    # 时间相同：动作顺序中距故障近者在前
    assert fault["operating_sequence"] == ["s2", "s1"]

    bad = client.post(
        "/protection-selectivity", json=base_payload(curves=curves, min_grading_margin=0.1)
    ).json()
    fault = bad["faults"][0]
    assert fault["selective"] is False
    assert fault["miscoordinated_segments"][0]["segment_id"] == "s1"
    assert fault["miscoordinated_segments"][0]["reason"] == "insufficient_margin"


def test_nearest_device_not_operating_is_miscoordination():
    """b 点 5A 低于 s2 首点：最近装置不动作，记 nearest_not_operating，全故障不满足。"""
    payload = base_payload(faults=[{"node": "b", "current": 5.0}])
    data = client.post("/protection-selectivity", json=payload).json()
    fault = data["faults"][0]
    assert fault["selective"] is False
    assert data["ok"] is False
    mis = fault["miscoordinated_segments"]
    assert len(mis) == 1
    assert mis[0]["segment_id"] == "s2"
    assert mis[0]["reason"] == "nearest_not_operating"
    assert mis[0]["operating_time"] is None
    assert mis[0]["required_time"] is None


# ---------- 基线误动 ----------


def test_baseline_current_at_or_above_pickup_marks_misoperation():
    """s2 启动电流降为 5A（= 基线电流 5A）：正常负荷下即动作，标出误动。"""
    payload = base_payload()
    payload["curves"][1]["points"] = [
        {"current": 5.0, "time": 0.6},
        {"current": 100.0, "time": 0.2},
    ]
    data = client.post("/protection-selectivity", json=payload).json()
    misop = data["misoperation_segments"]
    assert len(misop) == 1
    assert misop[0]["segment_id"] == "s2"
    assert misop[0]["baseline_current"] == pytest.approx(5.0)
    assert misop[0]["pickup_current"] == pytest.approx(5.0)
    assert misop[0]["operating_time"] == pytest.approx(0.6)
    assert data["ok"] is False  # 即使故障选择性满足，存在误动装置总体结论仍为 false
    assert data["faults"][0]["selective"] is True


def test_baseline_below_pickup_not_marked():
    data = client.post("/protection-selectivity", json=base_payload()).json()
    assert data["misoperation_segments"] == []


# ---------- 请求校验（400） ----------


def test_missing_curve_rejected():
    payload = base_payload()
    del payload["curves"][2]  # 缺 s3
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("s3" in d["message"] and "缺少保护曲线" in d["message"] for d in error["details"])


def test_duplicate_curve_rejected():
    payload = base_payload()
    payload["curves"].append(dict(payload["curves"][1]))  # 重复 s2
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 400
    assert any("s2" in d["message"] and "重复" in d["message"] for d in resp.json()["error"]["details"])


def test_curve_for_unknown_segment_rejected():
    payload = base_payload()
    payload["curves"][0]["segment_id"] = "ghost"
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 400
    assert any("ghost" in d["message"] for d in resp.json()["error"]["details"])


def test_fault_node_unknown_rejected():
    payload = base_payload(faults=[{"node": "ghost", "current": 50.0}])
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
    assert any("ghost" in d["message"] and "故障节点" in d["message"] for d in resp.json()["error"]["details"])


def test_fault_node_source_rejected():
    payload = base_payload(faults=[{"node": "source", "current": 50.0}])
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 400
    assert any("根节点" in d["message"] for d in resp.json()["error"]["details"])


def test_existing_tree_validation_still_applies():
    payload = base_payload()
    payload["loads"].append({"node": "ghost", "current": 1.0})
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


# ---------- 请求校验（422） ----------


def test_non_increasing_curve_points_rejected():
    # 相等：不满足严格递增
    payload = base_payload()
    payload["curves"][0]["points"] = [
        {"current": 20.0, "time": 1.0},
        {"current": 20.0, "time": 0.5},
    ]
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422
    assert any("严格递增" in d["message"] for d in resp.json()["error"]["details"])

    # 递减
    payload["curves"][0]["points"][1]["current"] = 15.0
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422
    assert any("严格递增" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_points_curves_faults_rejected():
    payload = base_payload()
    payload["curves"][0]["points"] = []
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422

    payload = base_payload(curves=[])
    assert client.post("/protection-selectivity", json=payload).status_code == 422

    payload = base_payload(faults=[])
    assert client.post("/protection-selectivity", json=payload).status_code == 422


def test_negative_and_non_positive_values_rejected():
    # 故障电流必须为正
    payload = base_payload(faults=[{"node": "b", "current": 0.0}])
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422
    assert any("故障电流" in d["message"] for d in resp.json()["error"]["details"])

    payload = base_payload(faults=[{"node": "b", "current": -1.0}])
    assert client.post("/protection-selectivity", json=payload).status_code == 422

    # 最小级差不能为负
    payload = base_payload(min_grading_margin=-0.1)
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422
    assert any("最小级差" in d["message"] for d in resp.json()["error"]["details"])

    # 曲线电流 / 动作时间不能为负
    payload = base_payload()
    payload["curves"][0]["points"][0]["current"] = -1.0
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422
    assert any("曲线电流" in d["message"] for d in resp.json()["error"]["details"])

    payload = base_payload()
    payload["curves"][0]["points"][0]["time"] = -0.01
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422
    assert any("动作时间" in d["message"] for d in resp.json()["error"]["details"])


def test_non_finite_numbers_rejected():
    payload = base_payload()
    payload["curves"][0]["points"][0]["time"] = "Infinity"
    assert client.post("/protection-selectivity", json=payload).status_code == 422

    payload = base_payload(faults=[{"node": "b", "current": "NaN"}])
    assert client.post("/protection-selectivity", json=payload).status_code == 422


def test_missing_required_fields_rejected():
    payload = base_payload()
    del payload["min_grading_margin"]
    resp = client.post("/protection-selectivity", json=payload)
    assert resp.status_code == 422
    assert any("最小级差" in d["message"] for d in resp.json()["error"]["details"])
