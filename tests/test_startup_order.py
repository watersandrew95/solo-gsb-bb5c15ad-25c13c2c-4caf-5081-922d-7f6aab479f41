"""POST /startup-order 的端到端测试。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """与 test_evaluate 相同的三层小树：

    source --s1(0.1Ω, 21A)--> a --s2(0.2Ω, 10A)--> b
                               |
                               +--s3(0.05Ω, 15A)--> c

    既有负载：a=2A, b=5A, c=8A
    安全基线：段电流 s1=15, s2=5, s3=8；节点电压 a=46.5, b=45.5, c=46.1

    待投运负载：
      0 号挂 b，稳态 3A / 启动 4A（启动使 s1=19、s2=9，s1 余量 2A）
      1 号挂 c，稳态 2A / 启动 6A（启动使 s1=21，恰触 s1 额定）
    """
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 21.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
        ],
        "loads": [
            {"node": "a", "current": 2.0},
            {"node": "b", "current": 5.0},
            {"node": "c", "current": 8.0},
        ],
        "startup_loads": [
            {"node": "b", "steady_current": 3.0, "startup_current": 4.0},
            {"node": "c", "steady_current": 2.0, "startup_current": 6.0},
        ],
    }
    payload.update(overrides)
    return payload


# ---------- feasible ----------


def test_feasible_order_big_startup_first():
    # 0 号先投：第一步 s1=19；第二步 1 号启动使 s1=19+6=22 > 21 -> [0,1] 失败
    # 1 号先投：第一步 s1=21 恰触额定；第二步 0 号启动使 s1=21-6+2+4=21 恰触 -> [1,0] 可行
    resp = client.post("/startup-order", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "feasible"
    assert data["order"] == [1, 0]
    assert len(data["steps"]) == 2

    first, second = data["steps"]
    assert first["step"] == 1
    assert first["load_index"] == 1
    assert first["node"] == "c"
    assert first["startup_current"] == pytest.approx(6.0)
    # 第一步：s1=21(a=45.9)、s2=5(b=44.9)、s3=14(c=45.2)，最低点 b，s1 负载率 1.0
    assert first["min_voltage"] == pytest.approx(44.9)
    assert first["min_voltage_node"] == "b"
    assert first["max_loading_ratio"] == pytest.approx(1.0)
    assert first["max_loading_segment_id"] == "s1"

    assert second["step"] == 2
    assert second["load_index"] == 0
    assert second["node"] == "b"
    assert second["startup_current"] == pytest.approx(4.0)
    # 第二步：1 号按稳态 2A、0 号按启动 4A：s1=21、s2=9、s3=10
    # a=45.9、b=44.1、c=45.4，最低点 b，s1/s2 负载率均为 1.0，平局取输入顺序最先的 s1
    assert second["min_voltage"] == pytest.approx(44.1)
    assert second["min_voltage_node"] == "b"
    assert second["max_loading_ratio"] == pytest.approx(1.0)
    assert second["max_loading_segment_id"] == "s1"


def test_only_first_load_needs_big_startup_picks_identity_order():
    # 两条负载均无启动冲击（启动=稳态）时，[0,1] 即字典序最小可行顺序
    payload = base_payload(
        startup_loads=[
            {"node": "b", "steady_current": 1.0, "startup_current": 1.0},
            {"node": "c", "steady_current": 1.0, "startup_current": 1.0},
        ]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["order"] == [0, 1]
    assert [s["load_index"] for s in data["steps"]] == [0, 1]


def test_single_load():
    payload = base_payload(
        startup_loads=[{"node": "a", "steady_current": 2.0, "startup_current": 5.0}]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["order"] == [0]
    assert len(data["steps"]) == 1
    step = data["steps"][0]
    assert step["min_voltage_node"] == "b"  # 基线即 b 最低，source 点负载只抬升 s1
    assert step["max_loading_segment_id"] == "s1"


def test_lexicographically_smallest_with_three_loads():
    # 三条负载均安全时，字典序最小顺序为 [0,1,2]
    payload = base_payload(
        startup_loads=[
            {"node": "a", "steady_current": 1.0, "startup_current": 1.0},
            {"node": "b", "steady_current": 1.0, "startup_current": 1.0},
            {"node": "c", "steady_current": 1.0, "startup_current": 1.0},
        ]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["order"] == [0, 1, 2]
    assert [s["step"] for s in data["steps"]] == [1, 2, 3]


def test_previous_loads_counted_at_steady_current():
    # 0 号启动 4A / 稳态 3A：第一步 s1=19；第二步 1 号启动 1A 时 s1 应为
    # 15 + 3（0 号稳态）+ 1 = 19，而非 15 + 4 + 1 = 20
    payload = base_payload(
        startup_loads=[
            {"node": "b", "steady_current": 3.0, "startup_current": 4.0},
            {"node": "a", "steady_current": 1.0, "startup_current": 1.0},
        ]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["order"] == [0, 1]
    second = data["steps"][1]
    assert second["max_loading_ratio"] == pytest.approx(19.0 / 21.0)


def test_source_node_startup_load_allowed():
    # 挂在 source 的待投运负载不流经任何线段、不产生压降，不影响校核
    payload = base_payload(
        startup_loads=[{"node": "source", "steady_current": 5.0, "startup_current": 9.0}]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["order"] == [0]
    step = data["steps"][0]
    assert step["min_voltage"] == pytest.approx(45.5)  # 与基线最低点 b 相同
    assert step["min_voltage_node"] == "b"
    assert step["max_loading_ratio"] == pytest.approx(15.0 / 21.0)


def test_exact_limit_boundary_is_allowed():
    # 0 号启动恰好使 s2 达额定 10A（5+5）、b 点恰好 44V，均不算越限
    payload = base_payload(
        startup_loads=[{"node": "b", "steady_current": 5.0, "startup_current": 5.0}]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    step = data["steps"][0]
    assert step["max_loading_ratio"] == pytest.approx(1.0)
    assert step["max_loading_segment_id"] == "s2"


def test_node_level_min_voltage_override_applies():
    # s3 提高到 0.12Ω 并把 c 阈值抬到 45.3V：基线 c=45.54 仍合规；
    # c 点启动 1A：s1=16、s3=9，c=48-1.6-1.08=45.32 ≥ 45.3，
    # 且低于 b 的 45.4，故全树最低点为 c，可行
    payload = base_payload()
    payload["segments"][2]["resistance"] = 0.12
    payload["loads"][2]["min_voltage"] = 45.3
    payload["startup_loads"] = [
        {"node": "c", "steady_current": 1.0, "startup_current": 1.0},
    ]
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["steps"][0]["min_voltage_node"] == "c"
    assert data["steps"][0]["min_voltage"] == pytest.approx(45.32)


def test_node_level_override_causes_infeasible():
    # c 阈值抬到 45.3V（基线 c=45.54 合规），c 点启动 2A 后 c=45.2 < 45.3 -> 无解
    payload = base_payload()
    payload["segments"][2]["resistance"] = 0.12
    payload["loads"][2]["min_voltage"] = 45.3
    payload["startup_loads"] = [
        {"node": "c", "steady_current": 1.0, "startup_current": 2.0},
    ]
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "infeasible"


def test_zero_current_loads_feasible():
    payload = base_payload(
        startup_loads=[
            {"node": "a", "steady_current": 0.0, "startup_current": 0.0},
            {"node": "b", "steady_current": 0.0, "startup_current": 0.0},
        ]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["order"] == [0, 1]
    assert data["steps"][0]["max_loading_ratio"] == pytest.approx(15.0 / 21.0)


# ---------- infeasible ----------


def test_infeasible_when_branch_segment_too_small_regardless_of_order():
    # 0 号在 b 启动 6A：s2 基线 5A + 6A = 11A > 额定 10A；
    # b 支路线段不受投运顺序影响，任何顺序该步都越限 -> infeasible
    payload = base_payload(
        startup_loads=[
            {"node": "b", "steady_current": 1.0, "startup_current": 6.0},
            {"node": "c", "steady_current": 1.0, "startup_current": 1.0},
        ]
    )
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "infeasible"
    assert data["order"] is None
    assert data["steps"] is None


def test_infeasible_single_over_current_load():
    payload = base_payload(
        startup_loads=[{"node": "b", "steady_current": 1.0, "startup_current": 6.0}]
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "infeasible"


def test_infeasible_due_to_under_voltage():
    # s1=0.2Ω/s2=0.5Ω：基线 b=38V？否——基线 b=48-15*0.2-5*0.5=42.5V 合规；
    # b 点启动 4A：s1=19、s2=9，b=48-19*0.2-9*0.5=39.7V < 40V 欠压
    payload = base_payload(
        segments=[
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.2, "rated_current": 30.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.5, "rated_current": 30.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 30.0},
        ],
        startup_loads=[{"node": "b", "steady_current": 1.0, "startup_current": 4.0}],
    )
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "infeasible"


# ---------- 基线越限（400 baseline_violation） ----------


def test_baseline_over_current_returns_400():
    payload = base_payload()
    payload["segments"][0]["rated_current"] = 14.0  # s1 基线 15A
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "baseline_violation"
    assert any("s1" in d["message"] for d in error["details"])


def test_baseline_under_voltage_returns_400():
    payload = base_payload()
    payload["segments"][0]["resistance"] = 1.0  # 全树欠压
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "baseline_violation"


# ---------- 输入校验 ----------


def test_startup_current_below_steady_rejected():
    payload = base_payload()
    payload["startup_loads"][0]["startup_current"] = 2.0  # < steady 3.0
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert any("启动电流不能小于稳态电流" in d["message"] for d in details)


def test_negative_startup_current_rejected():
    payload = base_payload()
    payload["startup_loads"][0]["startup_current"] = -1.0
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 422
    assert any("启动电流不能为负" in d["message"] for d in resp.json()["error"]["details"])


def test_negative_steady_current_rejected():
    payload = base_payload()
    payload["startup_loads"][0]["steady_current"] = -0.5
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 422
    assert any("稳态电流不能为负" in d["message"] for d in resp.json()["error"]["details"])


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_startup_current_rejected(bad):
    # httpx 的 json= 不允许非有限浮点，改用原始请求体发送 NaN / Infinity 字面量
    payload = base_payload()
    body = json.dumps(payload).replace('"startup_current": 4.0', f'"startup_current": {bad}', 1)
    resp = client.post(
        "/startup-order",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert any("启动电流" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_startup_loads_rejected():
    payload = base_payload(startup_loads=[])
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 422


def test_missing_startup_loads_rejected():
    payload = base_payload()
    del payload["startup_loads"]
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 422


def test_eleven_startup_loads_rejected():
    payload = base_payload(
        startup_loads=[
            {"node": "a", "steady_current": 0.0, "startup_current": 0.0} for _ in range(11)
        ]
    )
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 422
    assert any("10" in d["message"] for d in resp.json()["error"]["details"])


def test_ten_startup_loads_accepted():
    payload = base_payload(
        startup_loads=[
            {"node": "a", "steady_current": 0.0, "startup_current": 0.0} for _ in range(10)
        ]
    )
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "feasible"


def test_big_startup_must_be_first_with_ten_loads():
    # s1 基线 15A、额定 25A（恰余 10A）：9 号启动电流 10A 必须第 1 个投
    # （此前只要有任一 1A 稳态负载在运，s1 即达 26A 越限）；其后再投 9 条小负载
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.01, "rated_current": 25.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.01, "rated_current": 100.0},
        ],
        "loads": [{"node": "a", "current": 15.0}],
        "startup_loads": [
            {"node": "b", "steady_current": 1.0, "startup_current": 1.0} for _ in range(9)
        ]
        + [{"node": "b", "steady_current": 0.0, "startup_current": 10.0}],
    }
    data = client.post("/startup-order", json=payload).json()
    assert data["status"] == "feasible"
    assert data["order"][0] == 9
    assert sorted(data["order"]) == list(range(10))
    assert len(data["steps"]) == 10
    assert data["steps"][0]["max_loading_ratio"] == pytest.approx(1.0)


def test_unknown_startup_node_rejected():
    payload = base_payload()
    payload["startup_loads"][0]["node"] = "ghost"
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在于树中" in d["message"] for d in error["details"])


def test_existing_tree_validation_still_applies():
    payload = base_payload()
    payload["loads"].append({"node": "ghost", "current": 1.0})
    resp = client.post("/startup-order", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


def test_index_lists_startup_endpoint():
    info = client.get("/").json()
    assert info["startup_order"] == "POST /startup-order"
