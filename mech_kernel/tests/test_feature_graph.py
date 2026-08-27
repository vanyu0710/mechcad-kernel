"""
测试 FeatureGraph（DAG）
"""
import pytest
from mech_kernel.feature_graph import FeatureGraph
from mech_kernel.features import FeatureNode, FeatureType, FeatureState
from mech_kernel.errors import StateCorruptionError


def make_node(fid: str, name: str = "") -> FeatureNode:
    return FeatureNode(id=fid, type=FeatureType.EXTRUDE, name=name)


def test_empty_graph():
    g = FeatureGraph()
    assert len(g) == 0
    assert not g.has_cycle()
    assert g.topological_sort() == []


def test_add_node():
    g = FeatureGraph()
    n = make_node("F_001", "main_body")
    g.add_node(n)
    assert g.has_node("F_001")
    assert g.get_node("F_001").name == "main_body"
    assert len(g) == 1


def test_add_node_duplicate_raises():
    g = FeatureGraph()
    g.add_node(make_node("F_001"))
    with pytest.raises(StateCorruptionError):
        g.add_node(make_node("F_001"))


def test_add_edge():
    g = FeatureGraph()
    g.add_node(make_node("A"))
    g.add_node(make_node("B"))
    g.add_edge("A", "B")
    assert g.get_dependencies("B") and g.get_dependencies("B")[0].id == "A"
    assert g.get_dependents("A") and g.get_dependents("A")[0].id == "B"


def test_self_loop_rejected():
    g = FeatureGraph()
    g.add_node(make_node("A"))
    with pytest.raises(StateCorruptionError):
        g.add_edge("A", "A")


def test_cycle_detection():
    g = FeatureGraph()
    g.add_node(make_node("A"))
    g.add_node(make_node("B"))
    g.add_node(make_node("C"))
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    assert not g.has_cycle()
    # 加 C -> A 会形成环
    with pytest.raises(StateCorruptionError):
        g.add_edge("C", "A")


def test_topological_sort():
    g = FeatureGraph()
    for fid in ["A", "B", "C", "D"]:
        g.add_node(make_node(fid))
    g.add_edge("A", "C")
    g.add_edge("B", "C")
    g.add_edge("C", "D")
    order = g.topological_sort()
    # A 和 B 应在 C 之前，C 在 D 之前
    assert order.index("A") < order.index("C")
    assert order.index("B") < order.index("C")
    assert order.index("C") < order.index("D")


def test_get_descendants():
    g = FeatureGraph()
    for fid in ["A", "B", "C", "D", "E"]:
        g.add_node(make_node(fid))
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("C", "D")
    desc = g.get_descendants("A")
    assert set(desc) == {"B", "C", "D"}


def test_remove_node_cleans_edges():
    g = FeatureGraph()
    for fid in ["A", "B", "C"]:
        g.add_node(make_node(fid))
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.remove_node("B")
    assert not g.has_node("B")
    assert g.get_dependents("A") == []
    assert g.get_dependencies("C") == []


def test_snapshot_and_restore():
    g = FeatureGraph()
    for fid in ["A", "B", "C"]:
        g.add_node(make_node(fid))
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    
    snap = g.snapshot()
    
    # 修改原图
    g.add_node(make_node("D"))
    g.add_edge("C", "D")
    assert len(g) == 4
    
    # 恢复
    g.restore(snap)
    assert len(g) == 3
    assert not g.has_node("D")
