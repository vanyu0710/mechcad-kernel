"""
测试第 4 轮专家审查 P0 修复：

P0-1: renderer 缓存键（含 revision + level）
P0-2: manifold 三态（valid/invalid/unknown），不伪造 False
P0-3: kernel.geometry_revision + undo/redo/commit/rollback 自动清缓存
P0-4: renderer 异常隔离（坏几何/NaN/空/缺顶点）
"""
import math
import pytest
from mech_kernel import MechKernel
from mech_kernel.geometry_inspector import GeometryInspector
from mech_kernel.renderer import Renderer


# === Mock 几何 ===

class MockBox:
    def __init__(self, size=10):
        self._size = size
        self.vertices = [
            (0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
            (0, 0, size), (size, 0, size), (size, size, size), (0, size, size),
        ]
        self.faces = [
            [0, 1, 2], [0, 2, 3],
            [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4],
            [2, 3, 7], [2, 7, 6],
            [1, 2, 6], [1, 6, 5],
            [0, 3, 7], [0, 7, 4],
        ]
    
    def volume(self): return self._size ** 3
    def area(self): return 6 * self._size ** 2
    def face_count(self): return 12
    def edge_count(self): return 18
    def vertex_count(self): return 8
    
    def bounding_box(self):
        from types import SimpleNamespace
        bb = SimpleNamespace()
        bb.min = SimpleNamespace(X=0, Y=0, Z=0)
        bb.max = SimpleNamespace(X=self._size, Y=self._size, Z=self._size)
        return bb


class MockTorus:
    """圆环（g=1）—— 欧拉=0，V-E+F=0"""
    def __init__(self):
        # 简化：手算欧拉
        # 圆环：V=0, E=1, F=1 (粗略)
        self._v = 1
        self._e = 2
        self._f = 1
    
    def vertex_count(self): return self._v
    def edge_count(self): return self._e
    def face_count(self): return self._f
    def volume(self): return 100  # > 0
    def area(self): return 300


# === P0-1: 缓存键含 revision + level ===

def test_renderer_cache_key_includes_revision():
    """P0-1: 同一几何不同 revision 不应该命中同一缓存"""
    r = Renderer()
    geom = MockBox()
    
    # revision=1 渲染
    v1 = r.render(geom, "iso_only", geometry_revision=1)
    
    # revision=2 应该重新渲染（不命中）
    v2 = r.render(geom, "iso_only", geometry_revision=2)
    
    # 缓存里有 2 个 key
    assert len(r._cache) == 2


def test_renderer_cache_key_includes_level():
    """P0-1: 同一几何同一 revision 不同 level 不应该命中同一缓存"""
    r = Renderer()
    geom = MockBox()
    
    v1 = r.render(geom, "iso_only", geometry_revision=1)
    v2 = r.render(geom, "full", geometry_revision=1)
    
    # 缓存里应该有 2 个 key
    assert len(r._cache) == 2


def test_renderer_cache_hit_on_same_key():
    """P0-1: 同一 (id, revision, level) 命中缓存"""
    r = Renderer()
    geom = MockBox()
    
    v1 = r.render(geom, "iso_only", geometry_revision=1)
    v2 = r.render(geom, "iso_only", geometry_revision=1)
    
    # 缓存里只有 1 个 key
    assert len(r._cache) == 1


def test_renderer_lru_eviction():
    """P0-1: LRU 限制（默认 32）"""
    r = Renderer(cache_size=2)
    geom = MockBox()
    
    # 渲染 3 次（不同 revision）
    r.render(geom, "iso_only", geometry_revision=1)
    r.render(geom, "iso_only", geometry_revision=2)
    r.render(geom, "iso_only", geometry_revision=3)
    
    # 缓存只保留最近 2 个
    assert len(r._cache) == 2


def test_renderer_can_disable_cache():
    """P0-1: 可禁用缓存"""
    r = Renderer()
    r.disable_cache()
    geom = MockBox()
    
    r.render(geom, "iso_only", geometry_revision=1)
    r.render(geom, "iso_only", geometry_revision=1)
    
    # 禁用后不缓存
    assert len(r._cache) == 0


# === P0-2: manifold 三态 ===

def test_manifold_returns_three_states():
    """P0-2: 立方体（g=0）应该返回 'unknown'（缺底层 API）"""
    insp = GeometryInspector()
    geom = MockBox()
    s = insp.summary(geom)
    # 因为 MockBox 没有 is_manifold() 方法，应该返回 'unknown'
    assert s.is_manifold in ("valid", "invalid", "unknown")
    assert s.is_manifold == "unknown"  # 我们没 is_manifold() 方法


def test_manifold_uses_native_api_if_available():
    """P0-2: 有底层 is_manifold() 就用"""
    class HasIsManifold:
        def is_manifold(self): return True
        def bounding_box(self):
            from types import SimpleNamespace
            bb = SimpleNamespace()
            bb.min = SimpleNamespace(X=0, Y=0, Z=0)
            bb.max = SimpleNamespace(X=1, Y=1, Z=1)
            return bb
    insp = GeometryInspector()
    s = insp.summary(HasIsManifold())
    assert s.is_manifold == "valid"


def test_manifold_invalid_when_no_topology():
    """P0-2: 没有顶点/边/面 → 'invalid'"""
    class EmptyGeom:
        def bounding_box(self):
            from types import SimpleNamespace
            bb = SimpleNamespace()
            bb.min = SimpleNamespace(X=0, Y=0, Z=0)
            bb.max = SimpleNamespace(X=0, Y=0, Z=0)
            return bb
    insp = GeometryInspector()
    s = insp.summary(EmptyGeom())
    assert s.is_manifold == "invalid"


def test_manifold_does_not_use_euler_alone():
    """P0-2: 圆环（g=1，欧拉=0）不再被误判为非流形"""
    insp = GeometryInspector()
    geom = MockTorus()  # V=1, E=2, F=1, 欧拉=0
    s = insp.summary(geom)
    # 之前会返回 False（euler != 2），现在返回 'unknown'
    assert s.is_manifold == "unknown"


def test_watertight_returns_three_states():
    """P0-2: 水密也用三态"""
    insp = GeometryInspector()
    geom = MockBox()
    s = insp.summary(geom)
    assert s.is_watertight in ("valid", "invalid", "unknown")
    # volume > 0 但无底层 API → 'unknown'
    assert s.is_watertight == "unknown"


def test_connected_returns_three_states():
    """P0-2: 连通也用三态"""
    insp = GeometryInspector()
    geom = MockBox()
    s = insp.summary(geom)
    assert s.is_connected in ("valid", "invalid", "unknown")
    assert s.is_connected == "unknown"


def test_validate_handles_three_states():
    """P0-2: validate 接受三态（unknown 不算错）"""
    insp = GeometryInspector()
    geom = MockBox()  # 全部 unknown
    is_valid, issues = insp.validate(geom)
    # unknown 不算 issue
    assert is_valid is True
    assert issues == []


# === P0-3: geometry_revision + undo 自动清缓存 ===

def test_kernel_has_geometry_revision():
    """P0-3: kernel 持有 _geometry_revision"""
    k = MechKernel()
    assert hasattr(k, '_geometry_revision')
    assert k._geometry_revision == 0


def test_undo_bumps_geometry_revision():
    """P0-3: undo 后 _geometry_revision 递增"""
    k = MechKernel()
    initial = k._geometry_revision
    k.create_workplane("base", "XY")  # 触发事务 commit
    after_create = k._geometry_revision
    assert after_create > initial
    
    k.undo()
    after_undo = k._geometry_revision
    assert after_undo > after_create


def test_redo_bumps_geometry_revision():
    """P0-3: redo 后 _geometry_revision 递增"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.undo()
    rev_before_redo = k._geometry_revision
    
    k.redo()
    assert k._geometry_revision > rev_before_redo


def test_undo_clears_renderer_cache():
    """P0-3: undo 自动清 renderer 缓存"""
    k = MechKernel()
    k._current_geometry = MockBox()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    k.close_sketch("sk_1")
    r1 = k.extrude("sk_1", depth=20)  # 触发 iso 渲染
    
    # 缓存应该有内容
    assert len(k.renderer._cache) > 0
    
    # undo
    k.undo()
    
    # 缓存被清
    assert len(k.renderer._cache) == 0


def test_transaction_commit_bumps_revision():
    """P0-3: 事务 commit 后 _geometry_revision 递增"""
    k = MechKernel()
    initial = k._geometry_revision
    
    # 用 create_workplane 触发事务 commit
    k.create_workplane("base", "XY")
    
    assert k._geometry_revision > initial


def test_explicit_transaction_commit_bumps_revision():
    """P0-3: 显式事务 commit 后 _geometry_revision 递增"""
    from mech_kernel.transaction import Transaction
    k = MechKernel()
    initial = k._geometry_revision
    
    # 第一个事务：commit
    with Transaction(k, "test op 1") as txn:
        k.create_workplane("base", "XY")
        txn.commit()
    after_first = k._geometry_revision
    assert after_first > initial
    
    # 第二个事务：commit
    with Transaction(k, "test op 2") as txn:
        k.create_workplane("top", "XY")
        txn.commit()
    after_second = k._geometry_revision
    assert after_second > after_first


# === P0-4: 异常隔离 ===

def test_renderer_handles_none_gracefully():
    """P0-4: None 几何返回空 dict 不崩"""
    r = Renderer()
    result = r.render(None, "iso_only")
    assert all(v is None for v in result.values())


def test_renderer_handles_object_without_methods():
    """P0-4: 普通对象（无 vertices/faces）不崩"""
    r = Renderer()
    result = r.render(object(), "iso_only")
    assert all(v is None for v in result.values())


def test_renderer_handles_empty_mesh():
    """P0-4: 空 mesh 不崩"""
    r = Renderer()
    class EmptyMesh:
        vertices = []
        faces = []
    result = r.render(EmptyMesh(), "iso_only")
    assert all(v is None for v in result.values())


def test_renderer_handles_partial_vertices():
    """P0-4: vertices 元素不够 3 维不崩"""
    r = Renderer()
    class BadMesh:
        vertices = [(1, 2), (3, 4), (5, 6)]  # 只有 2 维
        faces = [[0, 1, 2]]
    result = r.render(BadMesh(), "iso_only")
    # 不崩
    assert isinstance(result, dict)


def test_renderer_handles_nan_in_bbox():
    """P0-4: bbox 含 NaN 不崩（核心目标：异常隔离）"""
    r = Renderer()
    class NanBBox:
        vertices = [(0, 0, 0), (1, 1, 1)]
        faces = [[0, 1, 0]]
        def bounding_box(self):
            from types import SimpleNamespace
            bb = SimpleNamespace()
            bb.min = SimpleNamespace(X=float('nan'), Y=0, Z=0)
            bb.max = SimpleNamespace(X=1, Y=1, Z=1)
            return bb
    # 目标：不崩
    result = r.render(NanBBox(), "iso_only")
    assert isinstance(result, dict)
    # renderer 用 vertices 算 bbox，绕过了 NaN，但容错依然重要
    # P0-4 真正关心的是不抛异常


def test_renderer_handles_inf_in_bbox():
    """P0-4: bbox 含 Inf 不崩"""
    r = Renderer()
    class InfBBox:
        vertices = [(0, 0, 0), (1, 1, 1)]
        faces = [[0, 1, 0]]
        def bounding_box(self):
            from types import SimpleNamespace
            bb = SimpleNamespace()
            bb.min = SimpleNamespace(X=0, Y=0, Z=0)
            bb.max = SimpleNamespace(X=float('inf'), Y=1, Z=1)
            return bb
    result = r.render(InfBBox(), "iso_only")
    assert isinstance(result, dict)


def test_inspector_rejects_nan_bbox():
    """P0-2/4: inspector 检测到 NaN bbox 时返回 None（不兜底为零盒）"""
    insp = GeometryInspector()
    
    class NanBBox:
        def bounding_box(self):
            from types import SimpleNamespace
            bb = SimpleNamespace()
            bb.min = SimpleNamespace(X=float('nan'), Y=0, Z=0)
            bb.max = SimpleNamespace(X=1, Y=1, Z=1)
            return bb
    
    # inspector._bounding_box 应该返回 None
    bbox = insp._bounding_box(NanBBox())
    assert bbox is None


def test_inspector_rejects_inf_bbox():
    """P0-2/4: inspector 检测到 Inf bbox 时返回 None"""
    insp = GeometryInspector()
    
    class InfBBox:
        def bounding_box(self):
            from types import SimpleNamespace
            bb = SimpleNamespace()
            bb.min = SimpleNamespace(X=0, Y=0, Z=0)
            bb.max = SimpleNamespace(X=float('inf'), Y=1, Z=1)
            return bb
    
    bbox = insp._bounding_box(InfBBox())
    assert bbox is None


def test_renderer_handles_broken_volume_method():
    """P0-4: volume() 抛异常不崩"""
    insp = GeometryInspector()
    class BrokenVol:
        def volume(self): raise RuntimeError("broken")
        def bounding_box(self):
            from types import SimpleNamespace
            bb = SimpleNamespace()
            bb.min = SimpleNamespace(X=0, Y=0, Z=0)
            bb.max = SimpleNamespace(X=1, Y=1, Z=1)
            return bb
    s = insp.summary(BrokenVol())
    assert s.volume == 0  # 兜底


def test_inspector_handles_broken_bbox():
    """P0-4: bounding_box() 抛异常不崩"""
    insp = GeometryInspector()
    class BrokenBB:
        def bounding_box(self): raise RuntimeError("broken")
    s = insp.summary(BrokenBB())
    assert s.bounding_box == (0, 0, 0, 0, 0, 0)  # 兜底


def test_kernel_undo_with_bad_geometry_does_not_crash():
    """P0-4: undo 时几何损坏不崩 kernel"""
    k = MechKernel()
    k._current_geometry = MockBox()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    k.close_sketch("sk_1")
    k.extrude("sk_1", depth=20)
    
    # 模拟几何损坏
    k._current_geometry = None
    
    # undo 不崩
    r = k.undo()
    assert r.success


def test_renderer_never_raises():
    """P0-4: renderer.render 永远不抛异常"""
    r = Renderer()
    
    # 各种异常输入
    inputs = [None, object(), "string", 42, [], {}, float('nan')]
    for inp in inputs:
        try:
            result = r.render(inp, "iso_only")
            assert isinstance(result, dict)
        except Exception as e:
            pytest.fail(f"renderer.render({inp!r}) raised {e}")
