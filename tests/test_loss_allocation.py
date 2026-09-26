"""POST /loss-allocation 的端到端测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """与 test_evaluate 相同的三层小树：

    source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                              |
                              +--s3(0.05Ω, 15A)--> c

    负载：a=2A, b=5A, c=8A
    段电流：s1=15, s2=5, s3=8
    段线损：s1=15²×0.1=22.5, s2=5²×0.2=5.0, s3=8²×0.05=3.2，总线损 30.7
    """
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
    }
    payload.update(overrides)
    return payload


# ---------- 逐段线损与总线损 ----------


def test_segment_losses_and_total():
    resp = client.post("/loss-allocation", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    segments = {s["segment_id"]: s for s in data["segments"]}
    assert segments["s1"]["current"] == pytest.approx(15.0)
    assert segments["s1"]["resistance"] == pytest.approx(0.1)
    assert segments["s1"]["loss"] == pytest.approx(22.5)
    assert segments["s2"]["loss"] == pytest.approx(5.0)
    assert segments["s3"]["loss"] == pytest.approx(3.2)

    # 总线损 = 各段线损之和
    assert data["total_loss"] == pytest.approx(30.7)
    assert data["total_loss"] == pytest.approx(sum(s["loss"] for s in data["segments"]))


def test_per_load_allocation():
    resp = client.post("/loss-allocation", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    loads = data["loads"]
    assert [load["index"] for load in loads] == [0, 1, 2]

    # 负载 0（a，2A）：路径仅 s1，承担 2×15×0.1 = 3.0
    assert loads[0]["node"] == "a"
    assert loads[0]["current"] == pytest.approx(2.0)
    assert loads[0]["loss"] == pytest.approx(3.0)
    assert [s["segment_id"] for s in loads[0]["segments"]] == ["s1"]
    assert loads[0]["segments"][0]["loss"] == pytest.approx(3.0)

    # 负载 1（b，5A）：s1 承担 5×15×0.1=7.5，s2 承担 5×5×0.2=5.0，合计 12.5
    assert loads[1]["node"] == "b"
    assert loads[1]["loss"] == pytest.approx(12.5)
    shares1 = {s["segment_id"]: s["loss"] for s in loads[1]["segments"]}
    assert shares1 == {"s1": pytest.approx(7.5), "s2": pytest.approx(5.0)}

    # 负载 2（c，8A）：s1 承担 8×15×0.1=12.0，s3 承担 8×8×0.05=3.2，合计 15.2
    assert loads[2]["node"] == "c"
    assert loads[2]["loss"] == pytest.approx(15.2)
    shares2 = {s["segment_id"]: s["loss"] for s in loads[2]["segments"]}
    assert shares2 == {"s1": pytest.approx(12.0), "s3": pytest.approx(3.2)}

    # 逐负载归属之和与总线损一致
    assert sum(load["loss"] for load in loads) == pytest.approx(data["total_loss"])


def test_source_node_load_zero_allocation():
    payload = base_payload()
    payload["loads"].insert(0, {"node": "source", "current": 4.0})
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    # source 节点负载路径为空，归属为零
    source_load = data["loads"][0]
    assert source_load["index"] == 0
    assert source_load["node"] == "source"
    assert source_load["loss"] == pytest.approx(0.0)
    assert source_load["segments"] == []

    # source 节点负载不流经任何线段，各段线损与总线损不变
    assert data["total_loss"] == pytest.approx(30.7)
    assert sum(load["loss"] for load in data["loads"]) == pytest.approx(data["total_loss"])


def test_same_node_loads_listed_separately():
    payload = base_payload()
    payload["loads"].append({"node": "b", "current": 1.0})  # 序号 3，b 点第二条负载
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    # b 点两条负载分别列出；s2 电流变为 6A，s1 变为 16A
    assert [load["index"] for load in data["loads"]] == [0, 1, 2, 3]
    first_b, second_b = data["loads"][1], data["loads"][3]
    assert first_b["node"] == second_b["node"] == "b"

    # 序号 1（b，5A）：s1 承担 5×16×0.1=8.0，s2 承担 5×6×0.2=6.0，合计 14.0
    assert first_b["loss"] == pytest.approx(14.0)
    shares1 = {s["segment_id"]: s["loss"] for s in first_b["segments"]}
    assert shares1 == {"s1": pytest.approx(8.0), "s2": pytest.approx(6.0)}

    # 序号 3（b，1A）：s1 承担 1×16×0.1=1.6，s2 承担 1×6×0.2=1.2，合计 2.8
    assert second_b["loss"] == pytest.approx(2.8)
    shares3 = {s["segment_id"]: s["loss"] for s in second_b["segments"]}
    assert shares3 == {"s1": pytest.approx(1.6), "s2": pytest.approx(1.2)}

    # 总线损 = 16²×0.1 + 6²×0.2 + 8²×0.05 = 25.6 + 7.2 + 3.2 = 36.0
    assert data["total_loss"] == pytest.approx(36.0)
    assert sum(load["loss"] for load in data["loads"]) == pytest.approx(data["total_loss"])


def test_zero_current_load_zero_share():
    payload = base_payload()
    payload["loads"].append({"node": "b", "current": 0.0})
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    zero_load = data["loads"][3]
    assert zero_load["loss"] == pytest.approx(0.0)
    # 路径各段仍列出（自负载节点向 source 排序），归属均为零
    assert [s["segment_id"] for s in zero_load["segments"]] == ["s2", "s1"]
    assert all(s["loss"] == pytest.approx(0.0) for s in zero_load["segments"])
    assert data["total_loss"] == pytest.approx(30.7)


def test_empty_loads_all_losses_zero():
    resp = client.post("/loss-allocation", json=base_payload(loads=[]))
    assert resp.status_code == 200
    data = resp.json()

    assert data["loads"] == []
    assert data["total_loss"] == pytest.approx(0.0)
    assert all(s["current"] == pytest.approx(0.0) for s in data["segments"])
    assert all(s["loss"] == pytest.approx(0.0) for s in data["segments"])


# ---------- 未舍入计算与六位小数展示 ----------


def test_display_values_rounded_to_six_decimals():
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
        ],
        "loads": [{"node": "a", "current": 0.123456789}],
    }
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    # 未舍入线损 = 0.123456789² × 0.1 = 0.001524157...，展示值舍入到 6 位小数
    assert data["total_loss"] == pytest.approx(0.001524, abs=1e-9)
    assert data["segments"][0]["loss"] == pytest.approx(0.001524, abs=1e-9)
    assert data["loads"][0]["loss"] == pytest.approx(0.001524, abs=1e-9)
    assert data["loads"][0]["segments"][0]["loss"] == pytest.approx(0.001524, abs=1e-9)


def test_allocation_sum_matches_total_with_fractional_currents():
    """非有限小数电流下，逐负载归属之和仍与总线损一致（展示舍入差除外）。"""
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
        ],
        "loads": [
            {"node": "a", "current": 0.1},
            {"node": "b", "current": 0.2},
        ],
    }
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    # 未舍入：s1 电流 0.3、线损 0.009；s2 电流 0.2、线损 0.008；总线损 0.017
    assert data["total_loss"] == pytest.approx(0.017)
    # 负载 0（a，0.1A）：s1 承担 0.1×0.3×0.1 = 0.003
    assert data["loads"][0]["loss"] == pytest.approx(0.003)
    # 负载 1（b，0.2A）：s1 承担 0.2×0.3×0.1=0.006，s2 承担 0.2×0.2×0.2=0.008
    assert data["loads"][1]["loss"] == pytest.approx(0.014)
    assert sum(load["loss"] for load in data["loads"]) == pytest.approx(data["total_loss"])


def test_min_voltage_does_not_affect_losses():
    low = client.post("/loss-allocation", json=base_payload(min_voltage=0.0)).json()
    high = client.post("/loss-allocation", json=base_payload(min_voltage=47.0)).json()
    assert low == high


def test_over_limit_baseline_still_computes_losses():
    """基线超额定 / 欠压不拦截线损归属（与 /capacity 的 baseline_violation 不同）。"""
    payload = base_payload()
    payload["segments"][0]["rated_current"] = 10.0  # s1 承载 15A，已超额定
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 200
    assert resp.json()["total_loss"] == pytest.approx(30.7)


# ---------- 复用树校验与错误响应 ----------


def test_invalid_tree_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不连通" in d["message"] for d in error["details"])


def test_unknown_load_node_rejected():
    payload = base_payload()
    payload["loads"].append({"node": "ghost", "current": 1.0})
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在" in d["message"] for d in error["details"])


def test_negative_load_rejected():
    payload = base_payload()
    payload["loads"][0]["current"] = -1.0
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("负载电流不能为负" in d["message"] for d in error["details"])


def test_segment_ids_auto_generated():
    payload = base_payload()
    for seg in payload["segments"]:
        del seg["id"]
    resp = client.post("/loss-allocation", json=payload)
    assert resp.status_code == 200
    ids = [s["segment_id"] for s in resp.json()["segments"]]
    assert ids == ["seg-1", "seg-2", "seg-3"]
