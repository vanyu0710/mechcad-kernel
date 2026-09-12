"""v2.17 P1-4: 拓扑引用重绑定防护测试。

审查场景：第一次 select 得 E00 → 改几何 → 再 select，新 E00 指代不同边；
模型记忆里"E00=那条边"静默换代。防护两层：
1. select 发放时指纹比对 → rebindings 报告（was/now 可见）；
2. 消费端 expected 锚点 → 不符即 TOPOLOGY_REFERENCE_REBOUND。
"""
import sys

from mech_kernel import MechKernel


def _box(k):
    k.create_workplane("base", "XY")
    k.new_sketch("base", "s")
    k.add_rectangle("s", 40, 40)
    k.close_sketch("s")
    k.extrude("s", 10)


def test_select_reports_rebindings_when_ref_changes_meaning():
    k = MechKernel()
    _box(k)
    first = k.select(element_type="edge", filter_type="line")
    assert first.success
    assert "rebindings" not in first.value  # 首次发放无历史可比
    k.fillet(radius=2, edges=["E00"])
    second = k.select(element_type="edge")
    assert second.success
    rebindings = second.value.get("rebindings") or []
    assert rebindings, "几何换代后必须报告 rebindings"
    entry = next(r for r in rebindings if r["ref"] == "E00")
    assert entry["was"]["length_mm"] == 10.0
    assert entry["now"]["length_mm"] != 10.0


def test_expected_anchor_rejects_stale_memory():
    k = MechKernel()
    _box(k)
    k.select(element_type="edge", filter_type="line")
    k.fillet(radius=2, edges=["E00"])
    k.select(element_type="edge")
    r = k.execute("fillet", radius=1, edges=["E00"],
                  expected={"type": "line", "length_mm": 10})
    assert not r.success
    assert (r.suggestion or {}).get("reason_code") == "topology_reference_rebound"


def test_expected_anchor_passes_on_match():
    k = MechKernel()
    _box(k)
    sel = k.select(element_type="edge", filter_type="line")
    edge = sel.value["selected"][0]
    r = k.execute("fillet", radius=2, edges=[edge["ref"]],
                  expected={"type": "line", "length_mm": edge["length_mm"]})
    assert r.success, r.error


def test_replay_skips_anchor_verification():
    """历史里带 expected 的 op 重放时不得因锚点失败阻断 rebuild。"""
    k = MechKernel()
    _box(k)
    sel = k.select(element_type="edge", filter_type="line")
    edge = sel.value["selected"][0]
    assert k.execute("fillet", radius=2, edges=[edge["ref"]],
                     expected={"type": "line", "length_mm": edge["length_mm"]}).success
    assert k.rebuild().success


def test_workplane_face_expected_anchor():
    k = MechKernel()
    _box(k)
    sel = k.select(element_type="face", filter_type="plane")
    face = sel.value["selected"][0]
    r = k.execute("create_workplane", name="on_top", type="face",
                  face_ref=face["ref"],
                  expected={"type": "plane", "center": face["center"]})
    assert r.success, r.error
    # create_workplane 也 bump revision（保守失效）→ 重 select 拿新鲜引用再验锚点
    sel2 = k.select(element_type="face", filter_type="plane")
    face2 = sel2.value["selected"][0]
    r = k.execute("create_workplane", name="bad", type="face",
                  face_ref=face2["ref"],
                  expected={"type": "plane", "center": [999, 999, 999]})
    assert not r.success
    assert (r.suggestion or {}).get("reason_code") == "topology_reference_rebound"


def test_fingerprint_stable_within_revision():
    """同 revision 重复 select 不得误报 rebind（指纹确定性）。"""
    k = MechKernel()
    _box(k)
    a = k.select(element_type="edge")
    b = k.select(element_type="edge")
    assert not b.value.get("rebindings"), b.value.get("rebindings")
