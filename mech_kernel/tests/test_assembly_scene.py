"""v2.14 装配场景命令 tests（export_assembly / assembly_interference / render_assembly）.

覆盖:
- XCAF 装配导出：多具名产品节点（STEPCAFControl_Reader 回读断言）、体积可加、bbox 覆盖
- 干涉：全对求交 + bbox 预过滤 + 预期重叠豁免 + 坏输入拒绝
- render_assembly：分色四视角 PNG
- 上限与输入校验
"""
from __future__ import annotations
import math
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mech_kernel.server import KernelServer


def _make_parts(tmp: str) -> list[dict]:
    """kernel 造两件：100×60×10 底板 + Ø16×30 凸台，各自导出 STEP。"""
    srv = KernelServer()
    paths = {}
    for name, sketch_op in (("plate", "rectangle"), ("peg", "circle")):
        srv.dispatch("execute", {"op": "create_workplane", "args": {"name": "b", "type": "XY"}})
        srv.dispatch("execute", {"op": "new_sketch", "args": {"workplane_name": "b", "sketch_name": "s"}})
        if sketch_op == "rectangle":
            srv.dispatch("execute", {"op": "add_rectangle", "args": {"sketch_name": "s", "width": 100, "height": 60}})
        else:
            srv.dispatch("execute", {"op": "add_circle", "args": {"sketch_name": "s", "center": [0, 0], "radius": 8}})
        srv.dispatch("execute", {"op": "close_sketch", "args": {"sketch_name": "s"}})
        srv.dispatch("execute", {"op": "extrude", "args": {"sketch_name": "s", "depth": 10 if name == "plate" else 30}})
        path = os.path.join(tmp, f"{name}.step")
        srv.dispatch("export", {"path": path, "format": "step"})
        srv.dispatch("reset", {})
        paths[name] = path
    return [
        {"path": paths["plate"], "name": "底板", "pose": {"position": [0, 0, 0]}},
        {"path": paths["peg"], "name": "凸台", "pose": {"position": [20, 10, 5]}},   # 与板重叠
        {"path": paths["peg"], "name": "远端凸台", "pose": {"position": [500, 0, 0]}},  # 不相交
    ]


def _read_xcaf_names(step_path: str) -> list[str]:
    """回读 STEP 的 XCAF 具名产品节点（build123d importers 同款 API）。"""
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDocStd import TDocStd_Document

    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    assert reader.ReadFile(step_path), "STEP 读取失败"
    assert reader.Transfer(doc), "XCAF transfer 失败"
    def label_name(label):
        target = label
        if shape_tool.IsReference_s(label):
            referred = TDF_Label()
            if shape_tool.GetReferredShape_s(label, referred):
                target = referred
        std_name = TDataStd_Name()
        if target.FindAttribute(TDataStd_Name.GetID_s(), std_name):
            return str(std_name.Get().ToWideString())
        return ""

    def resolve(label):
        if shape_tool.IsReference_s(label):
            referred = TDF_Label()
            if shape_tool.GetReferredShape_s(label, referred):
                return referred
        return label

    def walk(label):
        names.append(label_name(label))
        subs = TDF_LabelSequence()
        shape_tool.GetComponents_s(resolve(label), subs)  # 组件查询用解析后的标签
        for k in range(1, subs.Length() + 1):
            walk(subs.Value(k))

    roots = TDF_LabelSequence()
    shape_tool.GetFreeShapes(roots)
    names: list[str] = []
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i))
    return names


def test_export_assembly_named_products_and_volume():
    with tempfile.TemporaryDirectory() as tmp:
        parts = _make_parts(tmp)
        srv = KernelServer()
        out = os.path.join(tmp, "asm.step")
        r = srv.dispatch("export_assembly", {"parts": parts, "out_step": out})
        assert r["ok"] is True, r
        assert r["parts"] == 3 and r["solids"] == 3
        # 体积可加：板 60000 + 凸台×2 (π·64·30≈6031.9)
        expect = 100 * 60 * 10 + 2 * math.pi * 8 ** 2 * 30
        assert abs(r["volume"] - expect) / expect < 0.01, (r["volume"], expect)
        # bbox 覆盖远端件（x 到 508）
        assert r["bounding_box"][3] > 507
        assert os.path.getsize(out) > 1000
        # XCAF 回读：三个具名产品节点
        names = _read_xcaf_names(out)
        for wanted in ("底板", "凸台", "远端凸台"):
            assert any(wanted in n for n in names), (wanted, names)
    print("  ✓ test_export_assembly_named_products_and_volume")


def test_export_assembly_input_guards():
    srv = KernelServer()
    def bad(payload):
        resp = srv.handle_line(__import__("json").dumps(
            {"id": "t", "cmd": "export_assembly", "payload": payload}))
        assert resp["ok"] is False and resp["error"]["kind"] == "BAD_REQUEST", resp
        return resp["error"]["message"]
    assert "非空" in bad({"parts": [], "out_step": "x.step"})
    assert "超上限" in bad({"parts": [{"path": "p", "name": f"n{i}"} for i in range(33)], "out_step": "x.step"})
    with tempfile.TemporaryDirectory() as tmp:
        parts = _make_parts(tmp)
        real = parts[0]["path"]
        assert "重复" in bad({"parts": [{"path": real, "name": "a"}, {"path": real, "name": "a"}],
                              "out_step": os.path.join(tmp, "x.step")})
        assert "导入失败" in bad({"parts": [{"path": "no_such_file.step", "name": "a"}],
                                  "out_step": os.path.join(tmp, "x.step")})
    print("  ✓ test_export_assembly_input_guards")


def test_assembly_interference_with_prefilter_and_exemption():
    with tempfile.TemporaryDirectory() as tmp:
        parts = _make_parts(tmp)
        srv = KernelServer()
        r = srv.dispatch("assembly_interference", {"parts": parts})
        assert r["ok"] is True
        assert r["total_pairs"] == 3
        # 远端凸台与两件 bbox 都不相交 → 至少 2 对被预过滤
        assert r["prefiltered_pairs"] >= 2, r
        assert r["interfering_count"] == 1, r  # 板×凸台
        pair = r["pairs"][0]
        assert pair["name_a"] == "底板" and pair["name_b"] == "凸台"
        # 重叠体积 = π·8²·5 = 1005.3
        assert abs(pair["volume_mm3"] - math.pi * 64 * 5) < 50, pair
        # 豁免：给足上限 → 降为 exempted，interfering 报告清零（物理重叠仍计数）
        r2 = srv.dispatch("assembly_interference", {"parts": parts, "expected_overlaps": [
            {"a": "底板", "b": "凸台", "max_volume_mm3": 2000, "reason": "压配设计"}]})
        assert r2["exempted_count"] == 1 and r2["pairs"] == [], r2
        assert r2["interfering_count"] == 1  # 仍如实计数物理重叠
        # 上限过小 → 不豁免
        r3 = srv.dispatch("assembly_interference", {"parts": parts, "expected_overlaps": [
            {"a": "底板", "b": "凸台", "max_volume_mm3": 10, "reason": "too small"}]})
        assert r3["exempted_count"] == 0 and len(r3["pairs"]) == 1, r3
    print("  ✓ test_assembly_interference_with_prefilter_and_exemption")


def test_render_assembly_png():
    import base64
    with tempfile.TemporaryDirectory() as tmp:
        parts = _make_parts(tmp)
        srv = KernelServer()
        r = srv.dispatch("render_assembly", {"parts": parts, "size": 240})
        assert r["ok"] is True and r.get("render_base64")
        raw = base64.b64decode(r["render_base64"])
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    print("  ✓ test_render_assembly_png")


def test_assembly_commands_are_stateless():
    """三个命令不得读写 kernel 会话状态（F2a 核心不变量）。"""
    with tempfile.TemporaryDirectory() as tmp:
        parts = _make_parts(tmp)
        srv = KernelServer()
        before = srv.dispatch("feature_tree", {})
        srv.dispatch("export_assembly", {"parts": parts, "out_step": os.path.join(tmp, "a.step")})
        srv.dispatch("assembly_interference", {"parts": parts})
        srv.dispatch("render_assembly", {"parts": parts})
        after = srv.dispatch("feature_tree", {})
        assert after["node_count"] == before["node_count"] == 0
        assert after["op_history"] == []
        state = srv.dispatch("state", {})
        assert state.get("feature_count", 0) == 0
    print("  ✓ test_assembly_commands_are_stateless")


# ---------- entrypoint ----------

def main():
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    print(f"找到 {len(tests)} 个 v2.14 装配场景测试\n")
    passed = failed = 0
    failures = []
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))
    print(f"\n通过 {passed}/{len(tests)}, 失败 {failed}")
    if failures:
        for n, e in failures:
            print(f"  - {n}: {e}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
