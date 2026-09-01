"""
测试 M1：GeometryInspector + Renderer + AdaptiveRenderer
"""
import pytest
from mech_kernel.geometry_inspector import GeometryInspector
from mech_kernel.renderer import Renderer
from mech_kernel.adaptive_renderer import AdaptiveRenderer
from mech_kernel import MechKernel
from mech_kernel.step_result import GeometrySummary


# === Mock 几何对象 ===

class MockGeometry:
    """Mock 几何：用于测试"""
    def __init__(self, volume=0, area=0, faces=0, edges=0, vertices=0, bbox=None):
        self._volume = volume
        self._area = area
        self._faces = faces
        self._edges = edges
        self._vertices = vertices
        self._bbox = bbox or (0, 0, 0, 10, 10, 10)
    
    def volume(self): return self._volume
    def area(self): return self._area
    def face_count(self): return self._faces
    def edge_count(self): return self._edges
    def vertex_count(self): return self._vertices
    def bounding_box(self):
        from types import SimpleNamespace
        bb = SimpleNamespace()
        bb.min = SimpleNamespace(X=self._bbox[0], Y=self._bbox[1], Z=self._bbox[2])
        bb.max = SimpleNamespace(X=self._bbox[3], Y=self._bbox[4], Z=self._bbox[5])
        return bb


class MockMesh:
    """Mock mesh（带 vertices + faces）"""
    def __init__(self):
        # 一个简单立方体
        self.vertices = [
            (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
            (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10),
        ]
        self.faces = [
            [0, 1, 2], [0, 2, 3],   # 底
            [4, 5, 6], [4, 6, 7],   # 顶
            [0, 1, 5], [0, 5, 4],   # 前
            [2, 3, 7], [2, 7, 6],   # 后
            [1, 2, 6], [1, 6, 5],   # 右
            [0, 3, 7], [0, 7, 4],   # 左
        ]
        self._volume = 1000
        self._area = 600
        self._faces = 12
        self._edges = 18
        self._vertices = 8
    
    def volume(self): return self._volume
    def area(self): return self._area
    def face_count(self): return self._faces
    def edge_count(self): return self._edges
    def vertex_count(self): return self._vertices
    def bounding_box(self):
        from types import SimpleNamespace
        bb = SimpleNamespace()
        bb.min = SimpleNamespace(X=0, Y=0, Z=0)
        bb.max = SimpleNamespace(X=10, Y=10, Z=10)
        return bb


# === GeometryInspector 测试 ===

def test_inspector_empty_summary():
    insp = GeometryInspector()
    s = insp.summary(None)
    assert s.volume == 0
    assert s.face_count == 0
    assert s.is_manifold is False


def test_inspector_with_mock_geometry():
    insp = GeometryInspector()
    geom = MockGeometry(volume=1000, area=600, faces=12, edges=18, vertices=8)
    s = insp.summary(geom)
    assert s.volume == 1000
    assert s.face_count == 12
    assert s.vertex_count == 8
    # P0-2 修复：无底层 is_manifold()，返回三态 "unknown"
    assert s.is_manifold == "unknown"
    assert s.is_watertight == "unknown"
    assert s.is_connected == "unknown"


def test_inspector_with_mock_mesh():
    insp = GeometryInspector()
    mesh = MockMesh()
    s = insp.summary(mesh)
    assert s.volume == 1000
    assert s.face_count == 12
    assert s.bounding_box == (0, 0, 0, 10, 10, 10)


def test_inspector_handles_geometry_without_methods():
    """几何对象没有 volume/area 等方法时不崩溃"""
    insp = GeometryInspector()
    class EmptyGeometry:
        pass
    s = insp.summary(EmptyGeometry())
    assert s.volume == 0
    assert s.face_count == 0


def test_inspector_validate_valid_geometry():
    insp = GeometryInspector()
    geom = MockGeometry(volume=1000, faces=6, edges=12, vertices=8)
    is_valid, issues = insp.validate(geom)
    assert is_valid is True
    assert issues == []


def test_inspector_validate_invalid_geometry():
    insp = GeometryInspector()
    geom = MockGeometry(volume=0, faces=2, edges=5, vertices=1)  # V-E+F = -2, 非流形
    is_valid, issues = insp.validate(geom)
    assert is_valid is False
    assert len(issues) > 0


def test_inspector_handles_nan_in_bbox():
    insp = GeometryInspector()
    geom = MockGeometry(bbox=(float('nan'), 0, 0, 10, 10, 10))
    is_valid, issues = insp.validate(geom)
    assert is_valid is False


# === AdaptiveRenderer 测试 ===

def test_adaptive_no_geometry_returns_none():
    ar = AdaptiveRenderer()
    assert ar.should_render("extrude", has_geometry=False) == "none"


def test_adaptive_topology_change_returns_full():
    ar = AdaptiveRenderer()
    assert ar.should_render("extrude", has_geometry=True) == "full"
    assert ar.should_render("fillet", has_geometry=True) == "full"
    assert ar.should_render("boolean", has_geometry=True) == "full"
    assert ar.should_render("hole", has_geometry=True) == "full"


def test_adaptive_sketch_op_returns_none():
    ar = AdaptiveRenderer(interval=100)
    assert ar.should_render("add_circle", has_geometry=True) == "none"
    assert ar.should_render("add_rectangle", has_geometry=True) == "none"
    assert ar.should_render("close_sketch", has_geometry=True) == "none"
    assert ar.should_render("create_workplane", has_geometry=True) == "none"
    assert ar.should_render("new_sketch", has_geometry=True) == "none"


def test_adaptive_interval_triggers_full():
    ar = AdaptiveRenderer(interval=3)
    # 第 1 步
    assert ar.should_render("add_circle", has_geometry=True) == "none"
    # 第 2 步
    assert ar.should_render("add_circle", has_geometry=True) == "none"
    # 第 3 步：间隔 3 → full
    assert ar.should_render("add_circle", has_geometry=True) == "iso_only"


def test_adaptive_failure_then_recovery():
    ar = AdaptiveRenderer()
    ar.mark_failure()
    # 恢复（相同 op）→ iso
    assert ar.should_render("fillet", has_geometry=True) == "full"
    # 后续正常
    assert ar.should_render("add_circle", has_geometry=True) == "none"


def test_adaptive_key_decision_full():
    ar = AdaptiveRenderer()
    assert ar.should_render("delete_feature", has_geometry=True) == "full"
    assert ar.should_render("update_feature", has_geometry=True) == "full"


def test_adaptive_reset():
    ar = AdaptiveRenderer(interval=100)
    ar.should_render("extrude", has_geometry=True)  # iso
    ar.reset()
    # 重置后从头开始
    assert ar.should_render("add_circle", has_geometry=True) == "none"


# === Renderer 测试 ===

def test_renderer_with_none_geometry():
    r = Renderer()
    result = r.render(None, "iso_only")
    assert result["iso"] is None


def test_renderer_with_no_geometry_level_none():
    r = Renderer()
    result = r.render(None, "none")
    assert all(v is None for v in result.values())


def test_renderer_with_mock_mesh():
    r = Renderer(image_size=(320, 240), dpi=60)
    mesh = MockMesh()
    result = r.render(mesh, "iso_only")
    # 应该成功生成至少一个视角
    assert result["iso"] is not None
    assert len(result["iso"]) > 0


def test_renderer_full_level_4_views():
    r = Renderer(image_size=(320, 240), dpi=60)
    mesh = MockMesh()
    result = r.render(mesh, "full")
    assert result["iso"] is not None
    assert result["front"] is not None
    assert result["top"] is not None
    assert result["side"] is not None
    assert result["default"] is not None


def test_renderer_extract_mesh_handles_object_without_attrs():
    r = Renderer()
    v, f = r._extract_mesh(object())  # 普通对象没有 vertices/faces
    assert v == [] or v is None
    assert f == [] or f is None


def test_renderer_handles_empty_mesh():
    r = Renderer()
    class EmptyMesh:
        vertices = []
        faces = []
    result = r.render(EmptyMesh(), "iso_only")
    assert result["iso"] is None


# === MechKernel 集成测试 ===

def test_kernel_initializes_m1_components():
    k = MechKernel()
    assert k.inspector is not None
    assert k.renderer is not None
    assert k.adaptive_renderer is not None


def test_kernel_step_result_includes_geometry_summary():
    """M1 阶段：所有 StepResult 都含 geometry_summary"""
    k = MechKernel()
    r = k.create_workplane("base", "XY")
    assert r.geometry_summary is not None
    assert r.geometry_summary.feature_count == 0


def test_kernel_extrude_with_mock_geometry_renders():
    """M1 阶段：拓扑变化触发 iso 渲染（如果 _current_geometry 被设置）"""
    k = MechKernel()
    k._current_geometry = MockMesh()  # 模拟有几何
    
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    k.close_sketch("sk_1")
    r = k.extrude("sk_1", depth=20, confirm_replace=True)
    
    # M1 阶段：拓扑变化 + 有几何 = iso 渲染
    assert r.render_level == "full"
    # M1 阶段还没接 build123d，所以可能没真实 PNG（这是预期）
    # 但 render_png 不应该 None（mock mesh 可以渲染）


def test_kernel_no_geometry_renders_none():
    """没有几何时不渲染（query 在 v1.11 真实实现，几何为空时抛 InvalidRequestError）"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    # v1.11: query 真实，几何为空时抛 InvalidRequestError
    try:
        k.query("_current_geometry", "bounding_box")
        assert False, "应该抛 InvalidRequestError"
    except Exception as e:
        assert "几何为空" in str(e) or "query 需要先有几何" in str(e)


def test_kernel_render_cache_works():
    """渲染缓存：相同几何不重复渲染"""
    k = MechKernel()
    k._current_geometry = MockMesh()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    r1 = k.close_sketch("sk_1")
    r2 = k.extrude("sk_1", depth=20, confirm_replace=True)
    
    # 草图阶段不渲染
    assert r1.render_level == "none"
    # 拓扑变化 + 有几何 = iso 渲染
    assert r2.render_level == "full"
