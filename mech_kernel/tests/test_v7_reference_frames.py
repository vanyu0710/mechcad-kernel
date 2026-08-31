"""
v2.7 Reference Coordinate Frame focused tests.

覆盖:
- CoordinateFrame 正交右手规范化
- FrameRegistry 增删查 + parent 链
- resolve_point / resolve_placement 数值正确
- create_reference_plane / query_reference / resolve_point / resolve_placement 公开 API
- validate_assembly 多种 relation kind
- 集成: snapshot round-trip + assemble 后 mount_frame 关系
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mech_kernel.reference_frames import (
    CoordinateFrame, FrameRegistry, resolve_point, resolve_placement
)
from mech_kernel import MechKernel


# ---------- CoordinateFrame / FrameRegistry ----------

def test_frame_basic():
    f = CoordinateFrame(name="world", origin=(0, 0, 0), normal=(0, 0, 1), x_axis=(1, 0, 0))
    assert f.is_orthonormal()
    y = f.y_axis
    assert abs(y[0]) < 1e-6 and abs(y[1] - 1.0) < 1e-6 and abs(y[2]) < 1e-6
    print("  ✓ test_frame_basic")


def test_frame_x_axis_auto_orthogonalize():
    # x_axis 沿 (2, 0, 0) 跟 normal (0,0,1) 自然垂直，不变
    f = CoordinateFrame(name="a", origin=(0, 0, 0), normal=(0, 0, 1), x_axis=(2, 0, 0))
    assert abs(f.x_axis[0] - 1.0) < 1e-6
    # x_axis 不垂直 normal: x_axis = (1, 1, 0), normal = (0, 0, 1) → 应正交化为 (1/√2, 1/√2, 0)
    f2 = CoordinateFrame(name="b", origin=(0, 0, 0), normal=(0, 0, 1), x_axis=(1, 1, 0))
    assert abs(f2.x_axis[0] - 0.7071067811865475) < 1e-6
    assert abs(f2.x_axis[1] - 0.7071067811865475) < 1e-6
    assert abs(f2.x_axis[2]) < 1e-6
    print("  ✓ test_frame_x_axis_auto_orthogonalize")


def test_frame_to_from_world():
    f = CoordinateFrame(name="g", origin=(10, 20, 5), normal=(0, 0, 1), x_axis=(1, 0, 0))
    # local (3, 4, 6) → world (10+3, 20+4, 5+6) = (13, 24, 11)
    w = f.to_world((3, 4, 6))
    assert abs(w[0] - 13) < 1e-9 and abs(w[1] - 24) < 1e-9 and abs(w[2] - 11) < 1e-9
    # 反向
    l = f.to_local(w)
    assert abs(l[0] - 3) < 1e-9 and abs(l[1] - 4) < 1e-9 and abs(l[2] - 6) < 1e-9
    print("  ✓ test_frame_to_from_world")


def test_registry_add_and_dup():
    reg = FrameRegistry()
    reg.add(CoordinateFrame("a"))
    reg.add(CoordinateFrame("b", parent="a"))
    assert reg.names() == ["a", "b"]
    try:
        reg.add(CoordinateFrame("a"))
        assert False, "should raise"
    except ValueError:
        pass
    try:
        reg.add(CoordinateFrame("c", parent="missing"))
        assert False, "should raise"
    except ValueError:
        pass
    print("  ✓ test_registry_add_and_dup")


def test_registry_parent_chain():
    reg = FrameRegistry()
    reg.add(CoordinateFrame("root"))
    reg.add(CoordinateFrame("mid", origin=(1, 0, 0), parent="root"))
    reg.add(CoordinateFrame("leaf", origin=(2, 0, 0), parent="mid"))
    chain = reg.parent_chain("leaf")
    assert [f.name for f in chain] == ["root", "mid", "leaf"]
    print("  ✓ test_registry_parent_chain")


def test_registry_remove_with_child():
    reg = FrameRegistry()
    reg.add(CoordinateFrame("root"))
    reg.add(CoordinateFrame("child", parent="root"))
    try:
        reg.remove("root")
        assert False, "should raise"
    except ValueError:
        pass
    reg.remove("child")
    reg.remove("root")
    assert reg.names() == []
    print("  ✓ test_registry_remove_with_child")


def test_registry_serialization_roundtrip():
    reg = FrameRegistry()
    reg.add(CoordinateFrame("a", origin=(1, 2, 3), normal=(0, 0, 1)))
    reg.add(CoordinateFrame("b", origin=(4, 5, 6), normal=(1, 0, 0), parent="a", metadata={"role": "x_axis"}))
    data = reg.to_dict()
    reg2 = FrameRegistry()
    reg2.from_dict(data)
    assert reg2.names() == ["a", "b"]
    assert reg2.get("b").parent == "a"
    assert reg2.get("b").metadata == {"role": "x_axis"}
    print("  ✓ test_registry_serialization_roundtrip")


def test_resolve_point_basic():
    reg = FrameRegistry()
    reg.add(CoordinateFrame("datum", origin=(10, 20, 5), normal=(0, 0, 1), x_axis=(1, 0, 0)))
    w = resolve_point(reg, "datum", uv=(35, 20), normal_offset=6)
    assert w == (45.0, 40.0, 11.0)
    print("  ✓ test_resolve_point_basic")


def test_resolve_placement_rotation():
    reg = FrameRegistry()
    reg.add(CoordinateFrame("d", origin=(0, 0, 0), normal=(0, 0, 1), x_axis=(1, 0, 0)))
    # 绕 (0,1,0) 旋转 90° → 原 (1,0,0) 变成 (0,0,-1)
    _, mat = resolve_placement(reg, "d", rotation=(90, (0, 1, 0)))
    # 验证 (1,0,0) 被映射到接近 (0,0,-1)
    v = [1.0, 0.0, 0.0]
    out = [sum(mat[i][j] * v[j] for j in range(3)) for i in range(3)]
    assert abs(out[0]) < 1e-6 and abs(out[1]) < 1e-6 and abs(out[2] + 1) < 1e-6
    print("  ✓ test_resolve_placement_rotation")


# ---------- Kernel 公开 API ----------

def test_kernel_create_reference_plane():
    k = MechKernel()
    r = k.create_reference_plane("datum", origin=(0, 0, 0), normal=(0, 0, 1))
    assert r.success
    assert k._frame_registry.has("datum")
    # 重名应抛异常（不是 StepResult failure，因为是直接调用方法）
    raised = False
    try:
        k.create_reference_plane("datum")
    except Exception as e:
        raised = True
        assert "已存在" in str(e)
    assert raised, "重名应该抛异常"
    print("  ✓ test_kernel_create_reference_plane")


def test_kernel_query_reference():
    k = MechKernel()
    k.create_reference_plane("a")
    k.create_reference_plane("b", origin=(1, 0, 0), normal=(0, 0, 1))
    r = k.query_reference()
    assert r.success
    assert r.value["count"] == 2
    r1 = k.query_reference("a")
    assert r1.success
    assert r1.value["count"] == 1
    assert r1.value["frames"][0]["name"] == "a"
    print("  ✓ test_kernel_query_reference")


def test_kernel_resolve_point():
    k = MechKernel()
    k.create_reference_plane("datum", origin=(10, 20, 5))
    r = k.resolve_point("datum", uv=(35, 20), normal_offset=6)
    assert r.success
    assert r.value["world"] == [45.0, 40.0, 11.0]
    raised = False
    try:
        k.resolve_point("missing")
    except Exception:
        raised = True
    assert raised
    print("  ✓ test_kernel_resolve_point")


def test_kernel_resolve_placement():
    k = MechKernel()
    k.create_reference_plane("d", origin=(10, 20, 5), normal=(0, 0, 1), x_axis=(1, 0, 0))
    r = k.resolve_placement("d", uv=(0, 0), normal_offset=0, rotation=(90, (0, 1, 0)))
    assert r.success
    assert r.value["origin"] == [10.0, 20.0, 5.0]
    # 3x3 matrix 应当是 rotation * frame.basis
    mat = r.value["matrix"]
    assert len(mat) == 3 and all(len(row) == 3 for row in mat)
    print("  ✓ test_kernel_resolve_placement")


def test_kernel_validate_assembly_basic():
    k = MechKernel()
    k.create_reference_plane("a")
    k.create_reference_plane("b", origin=(50, 0, 0))
    r = k.validate_assembly(level="basic")
    assert r.success
    assert r.value["ok"] is True
    print("  ✓ test_kernel_validate_assembly_basic")


def test_kernel_validate_assembly_coaxial():
    k = MechKernel()
    k.create_reference_plane("input_axis", origin=(0, 0, 0), normal=(0, 0, 1))
    k.create_reference_plane("output_axis", origin=(50, 0, 0), normal=(0, 0, 1))
    k.create_reference_plane("misaligned", origin=(0, 0, 0), normal=(1, 0, 0))
    r = k.validate_assembly(
        level="standard",
        relations=[
            {"kind": "coaxial", "source": "input_axis", "target": "output_axis"},
            {"kind": "coaxial", "source": "input_axis", "target": "misaligned"},
        ],
    )
    assert r.success
    assert r.value["ok"] is False
    codes = [i["code"] for i in r.value["issues"]]
    assert "coaxial_misaligned" in codes
    print("  ✓ test_kernel_validate_assembly_coaxial")


def test_kernel_validate_assembly_perpendicular_parallel():
    k = MechKernel()
    k.create_reference_plane("z", normal=(0, 0, 1))
    k.create_reference_plane("x", normal=(1, 0, 0))
    k.create_reference_plane("y", normal=(0, 1, 0))
    k.create_reference_plane("z2", origin=(50, 0, 0), normal=(0, 0, 1))  # parallel to z
    r = k.validate_assembly(relations=[
        {"kind": "perpendicular", "source": "z", "target": "x"},
        {"kind": "perpendicular", "source": "z", "target": "y"},
        {"kind": "parallel", "source": "z", "target": "z2"},
    ])
    assert r.value["ok"] is True, r.value
    print("  ✓ test_kernel_validate_assembly_perpendicular_parallel")


def test_kernel_validate_assembly_gear_mesh():
    k = MechKernel()
    # 两齿轮中心距 = (40 + 80) / 2 = 60
    k.create_reference_plane("shaft_a", origin=(0, 0, 0), normal=(0, 0, 1))
    k.create_reference_plane("shaft_b", origin=(60, 0, 0), normal=(0, 0, 1))
    r = k.validate_assembly(relations=[{
        "kind": "gear_mesh",
        "source": "shaft_a",
        "target": "shaft_b",
        "parameters": {"source_pitch_diameter": 40, "target_pitch_diameter": 80, "tolerance": 0.5},
    }])
    assert r.value["ok"] is True
    # 中心距不对
    k2 = MechKernel()
    k2.create_reference_plane("sa", origin=(0, 0, 0), normal=(0, 0, 1))
    k2.create_reference_plane("sb", origin=(50, 0, 0), normal=(0, 0, 1))
    r2 = k2.validate_assembly(relations=[{
        "kind": "gear_mesh", "source": "sa", "target": "sb",
        "parameters": {"source_pitch_diameter": 40, "target_pitch_diameter": 80, "tolerance": 0.5},
    }])
    assert r2.value["ok"] is False
    assert any(i["code"] == "gear_mesh_center_distance_mismatch" for i in r2.value["issues"])
    print("  ✓ test_kernel_validate_assembly_gear_mesh")


def test_kernel_validate_assembly_unknown_kind():
    k = MechKernel()
    k.create_reference_plane("a")
    r = k.validate_assembly(relations=[{"kind": "weird", "source": "a", "target": "a"}])
    assert any(i["code"] == "rel_unknown_kind" for i in r.value["issues"])
    print("  ✓ test_kernel_validate_assembly_unknown_kind")


def test_frame_to_dict_roundtrip_via_dict():
    f1 = CoordinateFrame("g", origin=(1, 2, 3), normal=(0, 0, 1), x_axis=(1, 0, 0), metadata={"k": "v"})
    d = f1.to_dict()
    f2 = CoordinateFrame.from_dict(d)
    assert f1.name == f2.name
    assert f1.origin == f2.origin
    assert f1.normal == f2.normal
    assert f1.x_axis == f2.x_axis
    assert f1.metadata == f2.metadata
    print("  ✓ test_frame_to_dict_roundtrip_via_dict")


def test_assembly_instance_v27_fields():
    from mech_kernel.assembly import AssemblyInstance
    inst = AssemblyInstance(
        id="A_0001", name="test", path="/tmp/x.step",
        mount_frame="housing_datum", mount_uv=[10, 20], mount_normal_offset=5.0,
        local_origin=[0, 0, 0],
    )
    d = inst.to_dict()
    assert d["mount_frame"] == "housing_datum"
    assert d["mount_uv"] == [10, 20]
    assert d["mount_normal_offset"] == 5.0
    inst2 = AssemblyInstance.from_dict(d)
    assert inst2.mount_frame == "housing_datum"
    assert inst2.mount_uv == [10, 20]
    print("  ✓ test_assembly_instance_v27_fields")


# ---------- entrypoint ----------

def main():
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    print(f"找到 {len(tests)} 个 v2.7 测试\n")
    passed = 0
    failed = 0
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
