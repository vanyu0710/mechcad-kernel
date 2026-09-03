"""
测试 mech_kernel/server.py — stdio JSON-lines RPC 薄壳

覆盖：协议编解码（坏行/空行/缺字段）、命令分派、圆柱冒烟序列（对应
examples/01_cylinder.py）、feature_tree/op_history、undo/redo、snapshot/restore、
select 引用、STEP/STL 导出、错误路径（未知 cmd / 未知 op / unknown_field /
RECOVERABLE 建议）、serve 主循环与 shutdown。

可独立运行：python mech_kernel/tests/test_server.py
也可经 _pytest_compat runner 与全量测试一起跑。
"""
import base64
import io
import json
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mech_kernel.server import KernelServer

EXPECTED_VOLUME = math.pi * 50.0 * 50.0 * 20.0  # Ø100 × 20 圆柱


def approx(value: float, target: float, rel: float = 1e-6) -> bool:
    return abs(value - target) <= rel * abs(target)


def make_server() -> KernelServer:
    return KernelServer()


def rpc(server: KernelServer, cmd: str, payload=None) -> dict:
    line = json.dumps({"id": "t-1", "cmd": cmd, "payload": payload or {}}, ensure_ascii=False)
    return server.handle_line(line)


def expect_ok(server: KernelServer, cmd: str, payload=None) -> dict:
    resp = rpc(server, cmd, payload)
    assert resp["ok"] is True, f"{cmd} RPC 失败: {resp}"
    return resp["data"]


def expect_err(server: KernelServer, cmd: str, payload=None, kind=None) -> dict:
    resp = rpc(server, cmd, payload)
    assert resp["ok"] is False, f"{cmd} 应该失败: {resp}"
    if kind is not None:
        assert resp["error"]["kind"] == kind, f"{cmd} 错误类型 {resp['error']['kind']} != {kind}"
    return resp["error"]


def run_cylinder(server: KernelServer):
    """examples/01_cylinder.py 的 execute 化序列。"""
    steps = [
        ("create_workplane", {"name": "base", "type": "XY"}),
        ("new_sketch", {"workplane_name": "base", "sketch_name": "sk_1"}),
        ("add_circle", {"sketch_name": "sk_1", "center": [0, 0], "radius": 50, "name": "outer_circle"}),
        ("close_sketch", {"sketch_name": "sk_1"}),
        ("extrude", {"sketch_name": "sk_1", "depth": 20, "mode": "new_body", "name": "main_body"}),
    ]
    results = []
    for op, args in steps:
        data = expect_ok(server, "execute", {"op": op, "args": args})
        assert data["success"] is True, f"{op} 执行失败: {data}"
        results.append(data)
    return results


# ----------------------------------------------------------------- 协议基础


def test_ping():
    data = expect_ok(make_server(), "ping")
    assert data["pong"] is True
    assert isinstance(data["kernel_version"], str) and data["kernel_version"]


def test_empty_and_comment_lines_ignored():
    server = make_server()
    assert server.handle_line("") is None
    assert server.handle_line("   \n") is None
    assert server.handle_line("# comment") is None


def test_malformed_line_bad_request():
    resp = make_server().handle_line("not json at all")
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "BAD_REQUEST"
    assert resp["id"] is None


def test_missing_cmd_bad_request():
    resp = make_server().handle_line(json.dumps({"id": "x"}))
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "BAD_REQUEST"
    assert resp["id"] == "x"


def test_payload_not_object_bad_request():
    resp = make_server().handle_line(json.dumps({"id": "y", "cmd": "ping", "payload": [1, 2]}))
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "BAD_REQUEST"


def test_unknown_cmd():
    expect_err(make_server(), "no_such_cmd", kind="UNKNOWN_CMD")


def test_missing_required_payload_field():
    server = make_server()
    expect_err(server, "execute", {"args": {}}, kind="BAD_REQUEST")  # 缺 op
    expect_err(server, "update_feature", {"new_params": {}}, kind="BAD_REQUEST")  # 缺 feature_id
    expect_err(server, "delete_feature", {}, kind="BAD_REQUEST")


# ----------------------------------------------------------------- 能力清单


def test_capabilities_public_and_experimental():
    data = expect_ok(make_server(), "capabilities")
    assert data["public_count"] == 33
    names = [c["name"] for c in data["public"]]
    for expected in ("create_workplane", "extrude", "hole", "select", "undo", "set_parameter"):
        assert expected in names
    for cap in data["public"]:
        assert {"name", "category", "description", "inputs", "examples"} <= set(cap.keys())
        for field in cap["inputs"].values():
            assert {"type", "required"} <= set(field.keys())
    assert len(data["experimental"]) == 10
    assert all(e["name"] not in names for e in data["experimental"])


def test_capabilities_inputs_have_schema_details():
    data = expect_ok(make_server(), "capabilities")
    by_name = {c["name"]: c for c in data["public"]}
    depth = by_name["extrude"]["inputs"]["depth"]
    assert depth["required"] is True and depth["min"] == 0.001
    mode = by_name["extrude"]["inputs"]["mode"]
    assert "new_body" in mode["enum"] and mode["default"] == "new_body"


# ----------------------------------------------------------------- 冒烟序列


def test_cylinder_sequence_volume():
    server = make_server()
    results = run_cylinder(server)
    summary = results[-1]["geometry_summary"]
    # 体积/包围盒是 StepResult 契约；face/edge 等拓扑计数在 summary 里是惰性字段
    assert approx(summary["volume"], EXPECTED_VOLUME)
    assert summary["bounding_box"] == [-50.0, -50.0, 0.0, 50.0, 50.0, 20.0]
    faces = expect_ok(server, "query", {"target": "_current_geometry", "what": "face_count"})
    assert faces["success"] is True and faces["value"] >= 3


def test_feature_tree_matches_history():
    server = make_server()
    run_cylinder(server)
    data = expect_ok(server, "feature_tree")
    assert data["node_count"] >= 1
    assert len(data["op_history"]) == 5
    graph = data["graph"]
    assert set(graph.keys()) >= {"nodes", "edges"}
    assert len(graph["nodes"]) == data["node_count"]


def test_state():
    server = make_server()
    state = expect_ok(server, "state")
    assert state["feature_count"] == 0
    run_cylinder(server)
    state = expect_ok(server, "state")
    assert state["feature_count"] >= 1


def test_undo_redo_roundtrip():
    server = make_server()
    run_cylinder(server)
    before = expect_ok(server, "feature_tree")["node_count"]

    undo = expect_ok(server, "undo", {"steps": 1})
    assert undo["success"] is True
    assert expect_ok(server, "feature_tree")["node_count"] < before

    redo = expect_ok(server, "redo", {"steps": 1})
    assert redo["success"] is True
    assert expect_ok(server, "feature_tree")["node_count"] == before

    vol = expect_ok(server, "query", {"target": "_current_geometry", "what": "volume"})
    assert vol["success"] is True
    assert approx(float(vol["value"]), EXPECTED_VOLUME)


def test_snapshot_restore():
    server = make_server()
    run_cylinder(server)
    assert expect_ok(server, "snapshot")["saved"] is True

    undo = expect_ok(server, "undo", {"steps": 5})
    assert undo["success"] is True
    tree = expect_ok(server, "feature_tree")
    assert tree["node_count"] == 0 and len(tree["op_history"]) == 0

    restored = expect_ok(server, "restore")
    assert restored["restored"] is True and restored["op_history_len"] == 5
    vol = expect_ok(server, "query", {"target": "_current_geometry", "what": "volume"})
    assert vol["success"] is True
    assert approx(float(vol["value"]), EXPECTED_VOLUME)


def test_select_refs_value():
    server = make_server()
    run_cylinder(server)
    data = expect_ok(server, "select_refs", {"filter_type": "all", "element_type": "face"})
    assert data["success"] is True
    selected = data["value"]["selected"]
    assert len(selected) >= 3
    assert all(str(s["ref"]).startswith("F") for s in selected)


def test_validate_geometry_and_measure():
    server = make_server()
    run_cylinder(server)
    v = expect_ok(server, "validate_geometry", {"level": "standard"})
    assert v["success"] is True
    m = expect_ok(server, "measure", {"target1": "_current_geometry", "metric": "volume"})
    assert m["success"] is True


# ----------------------------------------------------------------- 导出


def test_export_step_and_mesh():
    server = make_server()
    run_cylinder(server)
    with tempfile.TemporaryDirectory() as td:
        step_path = os.path.join(td, "part.step")
        data = expect_ok(server, "export", {"path": step_path, "format": "step"})
        assert data["success"] is True
        assert os.path.getsize(step_path) > 0

        stl_path = os.path.join(td, "part.stl")
        mesh = expect_ok(server, "export_mesh", {"path": stl_path, "format": "stl"})
        assert mesh["size"] > 0 and os.path.getsize(stl_path) > 0
        # 二进制 STL（默认）头部是 80 字节自由字段，只校验非空

        expect_err(
            server,
            "export_mesh",
            {"path": os.path.join(td, "x.obj"), "format": "obj"},
            kind="BAD_REQUEST",
        )


def test_export_without_geometry():
    server = make_server()
    with tempfile.TemporaryDirectory() as td:
        expect_err(server, "export_mesh", {"path": os.path.join(td, "a.stl")}, kind="BAD_REQUEST")


def test_save_and_load_project():
    server = make_server()
    run_cylinder(server)
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "proj")
        saved = expect_ok(server, "save_project", {"base_path": base})
        assert saved["step_path"] if "step_path" in saved else True
        assert os.path.exists(base + ".step")

        fresh = make_server()
        loaded = expect_ok(fresh, "load_project", {"base_path": base})
        assert loaded["success"] is True
        vol = expect_ok(fresh, "query", {"target": "_current_geometry", "what": "volume"})
        assert vol["success"] is True
        assert approx(float(vol["value"]), EXPECTED_VOLUME, rel=1e-3)


# ----------------------------------------------------------------- 渲染


def test_render_returns_base64():
    server = make_server()
    run_cylinder(server)
    data = expect_ok(server, "render", {"views": ["iso"], "size": 64, "backend": "occ"})
    assert data["success"] is True
    if data.get("has_render"):
        raw = base64.b64decode(data["render_base64"])
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"


# ----------------------------------------------------------------- 错误路径


def test_execute_unknown_op_is_failed_step_not_rpc_error():
    data = expect_ok(make_server(), "execute", {"op": "not_an_op", "args": {}})
    assert data["success"] is False
    assert data["error_kind"] == "NOT_IMPLEMENTED"


def test_execute_unknown_field_suggestion():
    data = expect_ok(
        make_server(),
        "execute",
        {"op": "new_sketch", "args": {"workplane_name": "XY", "sketch_name": "s", "bogus": 1}},
    )
    assert data["success"] is False
    assert data["suggestion"]["reason_code"] == "unknown_field"


def test_execute_new_body_conflict_is_recoverable():
    server = make_server()
    run_cylinder(server)
    data = expect_ok(
        server,
        "execute",
        {"op": "extrude", "args": {"sketch_name": "sk_1", "depth": 5, "mode": "new_body"}},
    )
    assert data["success"] is False
    assert data["error_kind"] == "RECOVERABLE"
    assert isinstance(data["suggestion"], dict) and "action" in data["suggestion"]
    assert "confirm_replace" in json.dumps(data["suggestion"], ensure_ascii=False)


def test_execute_allow_experimental_gate():
    server = make_server()
    denied = expect_ok(server, "execute", {"op": "query_reference", "args": {}})
    assert denied["success"] is False  # 默认拒绝实验 op


def test_serve_loop_and_shutdown():
    server = make_server()
    requests = "\n".join(
        [
            json.dumps({"id": 1, "cmd": "ping"}),
            json.dumps({"id": 2, "cmd": "state"}),
            json.dumps({"id": 3, "cmd": "shutdown"}),
            json.dumps({"id": 4, "cmd": "ping"}),  # shutdown 之后不应再有响应
        ]
    ) + "\n"
    out, err = io.StringIO(), io.StringIO()
    code = server.serve(io.StringIO(requests), out, err)
    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().strip().splitlines()]
    assert [line["id"] for line in lines] == [1, 2, 3]
    assert lines[0]["data"]["pong"] is True
    assert lines[1]["data"]["feature_count"] == 0
    assert lines[2]["data"]["bye"] is True


def test_serve_eof_exits_cleanly():
    server = make_server()
    out, err = io.StringIO(), io.StringIO()
    code = server.serve(io.StringIO(""), out, err)
    assert code == 0 and out.getvalue() == ""


if __name__ == "__main__":
    # 直接运行：python mech_kernel/tests/test_server.py
    mod = sys.modules[__name__]
    failed = 0
    names = [n for n in dir(mod) if n.startswith("test_") and callable(getattr(mod, n))]
    for name in names:
        try:
            getattr(mod, name)()
            print(f"  ok {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
