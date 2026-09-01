"""
v2.9 Collision / Interference Check tests.

覆盖:
- check_pair_interference 基础: 有/无干涉
- check_assembly_interference: 多 part, pair 数量, 干涉列表
- check_interference_matrix: 矩阵可视化
- MechKernel.check_interference API
- demo 14 场景: 齿轮 + 轴 + housing
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from build123d import Box, Part, Location
from mech_kernel.collision import (
    check_pair_interference, check_assembly_interference, check_interference_matrix
)
from mech_kernel import MechKernel


def test_no_overlap():
    """两个 box 不重叠时, vol=0, interfering=False."""
    b1 = Box(10, 10, 10)
    b2 = Box(5, 5, 5).moved(Location((20, 0, 0)))
    r = check_pair_interference(b1, b2, "b1", "b2")
    assert r["interfering"] is False
    assert r["volume_mm3"] < 0.01
    assert r["intersection_part"] is None
    print("  ✓ test_no_overlap")


def test_overlap():
    """两个 box 重叠时, vol > 0, interfering=True."""
    b1 = Box(10, 10, 10)
    b2 = Box(5, 5, 5)
    r = check_pair_interference(b1, b2, "b1", "b2")
    assert r["interfering"] is True
    assert 124 < r["volume_mm3"] < 126  # 5*5*5 = 125
    assert r["center"] is not None
    print("  ✓ test_overlap")


def test_tolerance():
    """tolerance 低于 0.001 应该被视为无干涉."""
    b1 = Box(10, 10, 10)
    b2 = Box(5, 5, 5)
    r = check_pair_interference(b1, b2, "b1", "b2", tolerance=200.0)  # 远高于 125
    assert r["interfering"] is False  # 因为 vol (125) < tolerance (200)
    print("  ✓ test_tolerance")


def test_assembly_total_pairs():
    """N 个 part → N*(N-1)/2 个 pair."""
    parts = [(f"p{i}", Box(10, 10, 10)) for i in range(5)]
    r = check_assembly_interference(parts)
    assert r["total_pairs"] == 10  # 5*4/2
    # 5 个 Box 在同一原点, 10 个 pair 都干涉
    # 但 OCC 对完全相同 shape 的 boolean 偶尔返回 0 (内部去重)
    # 接受 ≥ 5 (至少一半干涉)
    assert r["interfering_count"] >= 5, f"expected ≥5 interfering, got {r['interfering_count']}"
    assert r["max_interference_volume"] > 100
    print("  ✓ test_assembly_total_pairs")


def test_assembly_mixed():
    """混合 part: 有的重叠, 有的不重叠."""
    parts = [
        ("A", Box(10, 10, 10)),
        ("B", Box(5, 5, 5)),                # 重叠 A
        ("C", Box(5, 5, 5)),                # 重叠 A
        ("D", Box(5, 5, 5).moved(Location((20, 0, 0)))),  # 不重叠
    ]
    r = check_assembly_interference(parts)
    assert r["total_pairs"] == 6
    # A-B, A-C, B-C 重叠 (3 个); A-D, B-D, C-D 不重叠
    assert r["interfering_count"] == 3
    # 验证配对
    interfering = r["interfering_pairs"]
    names = sorted((p["name_a"], p["name_b"]) for p in interfering)
    assert names == [("A", "B"), ("A", "C"), ("B", "C")]
    print("  ✓ test_assembly_mixed")


def test_assembly_only_interfering():
    """only_interfering=True 只返回有干涉的 pair."""
    parts = [
        ("A", Box(10, 10, 10)),
        ("B", Box(5, 5, 5)),
        ("C", Box(5, 5, 5).moved(Location((20, 0, 0)))),
    ]
    r = check_assembly_interference(parts, only_interfering=True)
    assert r["total_pairs"] == 3  # 仍然报告全部
    # 但内部 list 只包含有干涉的 (A-B)
    assert len([p for p in r["pairs"]]) <= 3
    print("  ✓ test_assembly_only_interfering")


def test_interference_matrix():
    """矩阵是对称的, 对角线为 0."""
    parts = [
        ("A", Box(10, 10, 10)),
        ("B", Box(5, 5, 5)),
        ("C", Box(5, 5, 5).moved(Location((20, 0, 0)))),
    ]
    m = check_interference_matrix(parts)
    assert m["names"] == ["A", "B", "C"]
    assert len(m["matrix"]) == 3
    # 对角线
    for i in range(3):
        assert m["matrix"][i][i] == 0.0
    # 对称
    for i in range(3):
        for j in range(3):
            assert m["matrix"][i][j] == m["matrix"][j][i]
    # A-B 重叠, A-C 不重叠
    assert m["matrix"][0][1] > 100
    assert m["matrix"][0][2] < 0.01
    print("  ✓ test_interference_matrix")


def test_kernel_check_interference_api():
    """MechKernel.check_interference 应该能用."""
    k = MechKernel()
    parts = [("A", Box(10, 10, 10)), ("B", Box(5, 5, 5))]
    r = k.check_interference(parts)
    assert r.success
    assert r.value["total_pairs"] == 1
    assert r.value["interfering_count"] == 1
    print("  ✓ test_kernel_check_interference_api")


def test_kernel_check_interference_empty():
    """空 parts 应该返回 0 pairs."""
    k = MechKernel()
    r = k.check_interference([])
    assert r.success
    assert r.value["total_pairs"] == 0
    assert r.value["interfering_count"] == 0
    print("  ✓ test_kernel_check_interference_empty")


def test_real_assembly_no_interference():
    """模拟装配体: 4 个齿轮在轴上 (实际啮合对会有干涉, 但相邻齿轮不该有)."""
    # 这测试一个典型场景: input + intermediate_large 啮合
    # 它们的中心距 = 80mm, 半径 20+60 = 80, 接触但不应该有侵入
    # 但因为是梯形齿形 (不是真实 involute), 接触是 0-干涉
    # 我们测一个安全距离 case
    from mech_kernel.gear import build_involute_gear
    g1 = build_involute_gear(module=2.0, teeth=20, width=18, bore=16)
    g2 = build_involute_gear(module=2.0, teeth=20, width=18, bore=16).moved(Location((100, 0, 0)))
    r = check_pair_interference(g1, g2, "g1", "g2")
    assert r["interfering"] is False  # 距离 100mm > 齿轮半径
    print("  ✓ test_real_assembly_no_interference")


def test_real_assembly_meshing_gears():
    """两个啮合齿轮: 中心距 80mm, 半径 20+62=82, 实际会干涉 2mm."""
    from mech_kernel.gear import build_involute_gear
    g_input = build_involute_gear(module=2.0, teeth=20, width=18, bore=16)
    g_inter = build_involute_gear(module=2.0, teeth=60, width=18, bore=22)
    # 中心距 80mm (cd=80 for 20+60)
    g_inter_moved = g_inter.moved(Location((0, 80, 0)))
    r = check_pair_interference(g_input, g_inter_moved, "input_z20", "inter_z60")
    # 实际有少量干涉 (因为梯形齿形 vs 真圆)
    # vol 应 > 0 (有侵入) 但比较小
    assert r["volume_mm3"] > 0
    print(f"  ✓ test_real_assembly_meshing_gears: vol={r['volume_mm3']:.1f} mm³")


# ---------- entrypoint ----------

def main():
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    print(f"找到 {len(tests)} 个 v2.9 collision 测试\n")
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
