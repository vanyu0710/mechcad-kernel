"""v2.15 renderer quality tests.

Guards the fix for the reported defect: blue models showed messy triangles
and stray lines on faces. Root causes: every tessellation triangle edge was
drawn when edges were on, flat per-triangle shading banded on curved faces,
and build123d tessellate only capped linear deflection (coarse facets on
small fillets). The fix: welded OCP tessellation with an angular cap,
dihedral-based feature edges drawn as camera-facing ribbons inside the face
collection, and crease-aware smoothed vertex normals.
"""
import inspect
import math

from build123d import Box, Cylinder

from mech_kernel.renderer import Renderer


def _png_ok(data):
    return isinstance(data, bytes) and len(data) > 200 and data[:8] == b"\x89PNG\r\n\x1a\n"


# ---------- welded OCP tessellation ----------

def test_ocp_tessellation_welds_box_to_eight_vertices():
    r = Renderer()
    vertices, faces = r._extract_mesh(Box(20, 30, 10))
    # 6 quads -> 12 triangles, exactly 8 unique welded corners
    assert len(vertices) == 8
    assert len(faces) == 12


def test_box_feature_edges_are_exactly_the_twelve_cube_edges():
    r = Renderer()
    vertices, faces = r._extract_mesh(Box(20, 30, 10))
    style = r._analyze_group(vertices, faces)
    assert style is not None
    assert len(style["edges"]) == 12
    assert len(style["normals"]) == len(style["triangles"])


def test_cylinder_band_has_no_facet_lines_only_rims():
    """The old renderer drew a line per facet; v2.15 keeps only the rims.

    Every feature edge on a plain cylinder is a horizontal rim segment: a
    vertical band seam or facet-row line would prove triangulation leaked.
    """
    r = Renderer()
    vertices, faces = r._extract_mesh(Cylinder(8, 25))
    style = r._analyze_group(vertices, faces)
    assert style is not None
    assert len(style["triangles"]) > 40
    assert style["edges"], "rims should still be drawn"
    for pa, pb, _n1, _n2 in style["edges"]:
        assert abs(pa[2] - pb[2]) < 1e-9, f"facet line leaked: {pa} -> {pb}"
    # ~53 facets per rim (linear cap governs here), definitely not thousands
    assert len(style["edges"]) < 400


# ---------- feature edge extraction ----------

def test_planar_grid_keeps_only_boundary_edges():
    """The exact user complaint: fan diagonals on a flat face."""
    r = Renderer()
    n = 6
    vertices = [(float(i), float(j), 0.0) for j in range(n) for i in range(n)]
    faces = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            b = a + 1
            c = a + n
            d = c + 1
            faces.append([a, b, d])
            faces.append([a, d, c])
    style = r._analyze_group(vertices, faces)
    assert style is not None
    expected_boundary = 4 * (n - 1)
    assert len(style["edges"]) == expected_boundary
    limit = float(n - 1)
    for pa, pb, _n1, _n2 in style["edges"]:
        on_boundary = (pa[0] in (0.0, limit) or pa[1] in (0.0, limit) or
                       pb[0] in (0.0, limit) or pb[1] in (0.0, limit))
        assert on_boundary, "interior diagonal leaked into feature edges"
        # open boundary edges carry the face normal as first adjacent normal
        assert _n2 is None and abs(_n1[2]) > 0.99


def test_crease_normals_do_not_blur_box_corners():
    r = Renderer()
    vertices, faces = r._extract_mesh(Box(10, 10, 10))
    style = r._analyze_group(vertices, faces)
    # 90-deg creases out-smooth each other at shared corners; every smoothed
    # normal must stay axis-aligned or the box would read as a blob
    for nx, ny, nz in style["normals"]:
        assert sum(abs(v) > 0.99 for v in (nx, ny, nz)) == 1, f"corner blurred: {(nx, ny, nz)}"


def test_edge_ribbons_orient_and_skip_parallel():
    r = Renderer()
    edge = ((0, 0, 0), (10, 0, 0), (0, -1, 0), None)  # boundary normal faces camera
    ribbons = r._edge_ribbons([edge], (0, -100, 0), 0.5)
    assert len(ribbons) == 2  # one edge -> one ribbon -> two triangles
    # ribbon sits at the requested width around the edge line (edge along x,
    # view along -y -> the width axis is z)
    zs = [p[2] for tri in ribbons for p in tri]
    assert max(zs) - min(zs) == 1.0
    parallel = ((0, 0, 0), (0, -10, 0), (0, -1, 0), None)
    assert r._edge_ribbons([parallel], (0, -100, 0), 0.5) == []


def test_backfacing_edges_are_culled():
    """Far-side rims must not bleed through front plates (ghost circles)."""
    r = Renderer()
    edge = ((0, 0, 0), (10, 0, 0), (0, 1, 0), None)  # boundary normal points away
    assert r._edge_ribbons([edge], (0, -100, 0), 0.5) == []
    # same edge seen from the other side is drawn
    assert len(r._edge_ribbons([edge], (0, 100, 0), 0.5)) == 2


# ---------- end-to-end render ----------

def test_render_with_and_without_edges_returns_png():
    r = Renderer(image_size=(240, 180), dpi=60)
    with_edges = r.render(Box(20, 20, 20), level="iso_only", show_edges=True)
    plain = r.render(Box(20, 20, 20), level="iso_only", show_edges=False)
    assert _png_ok(with_edges.get("iso"))
    assert _png_ok(plain.get("iso"))


def test_per_facet_edgecolor_path_is_gone():
    """Regression lock: Poly3DCollection must never carry triangle outlines."""
    text = inspect.getsource(Renderer._render_view)
    assert 'edgecolor="none"' in text or "edgecolor='none'" in text
    assert "linewidth=0.18" not in text


def test_over_budget_group_falls_back_without_crash():
    r = Renderer(image_size=(200, 150), dpi=50)
    r.STYLE_BUDGET = 1  # instance override: every group skips analysis
    views = r.render(Box(20, 20, 20), level="iso_only", show_edges=True)
    assert _png_ok(views.get("iso"))


def test_degenerate_mesh_analyse_returns_none():
    r = Renderer()
    assert r._analyze_group([(0, 0, 0), (1, 0, 0)], [[0, 1, 0]]) is None
    assert r._analyze_group([], []) is None


# ---------- v2.16.1 audit regressions (review-2026-09-11/rendering-review.md) ----------

def test_cube_silhouette_edges_survive_culling():
    """P1-1: an edge with one adjacent face toward the camera is a silhouette
    candidate and must survive culling. Cube seen from (1,0.2,0.1): three
    faces visible, so 9 of its 12 edges are candidates (only the 3 edges of
    the far corner have both faces away)."""
    import io

    from PIL import Image

    r = Renderer()
    v, f = r._extract_mesh(Box(20, 20, 20))
    st = r._analyze_group(v, f)
    view = (1.0, 0.2, 0.1)
    ribbons = r._edge_ribbons(st["edges"], view, 0.5)
    # 9 candidate edges x 2 triangles per ribbon
    assert len(ribbons) == 18, f"{len(ribbons)} ribbons, expected 18 (9 silhouette edges)"


def test_hidden_edges_do_not_bleed_through_front_plate():
    """P1-2: a closed cube fully behind a thin front plate must contribute no
    edge pixels. Stroke width must not be used as a depth bias: the old
    offset(half_width*2) lifted hidden rims in front of the 0.05 mm plate."""
    import io

    import numpy as np
    from PIL import Image

    r = Renderer()

    def style_of(shape):
        vs, fs = r._extract_mesh(shape)
        return (vs, fs, r.body_color, False), r._analyze_group(vs, fs)

    g_plate, s_plate = style_of(Box(0.05, 20, 20).translate((0.075, 0, 0)))
    g_cube, s_cube = style_of(Box(2, 10, 10).translate((-1, 0, 0)))
    common = dict(
        vertices=[], faces=[], bbox=(-2, -10, -10, 0.1, 10, 10),
        camera_pos=(-0.95, -40, 0), view_name="front",
        cx=-0.95, cy=0, cz=0, lim=20, size=(2.1, 20, 20),
        show_edges=True, image_size=(480, 480),
    )
    both = r._render_view(**common, mesh_groups=[g_plate, g_cube], mesh_styles=[s_plate, s_cube])
    front = r._render_view(**common, mesh_groups=[g_plate], mesh_styles=[s_plate])
    a = np.asarray(Image.open(io.BytesIO(both)).convert("RGB")).astype(int)
    b = np.asarray(Image.open(io.BytesIO(front)).convert("RGB")).astype(int)
    diff = int(np.count_nonzero(np.max(np.abs(a - b), axis=2) > 5))
    # Pre-HLE the cube's full edge box bled through the 0.05 mm plate (~1375 px
    # at 480). Hidden-line elimination removes every hidden ribbon; what
    # remains (~150 px) is a 1 px anti-aliased seam along the plate's own
    # triangulation diagonal, where the two coplanar front-face triangles blend
    # with whatever lies behind them (plate back face alone vs the cube face).
    # The review's acceptance allows small AA differences but forbids the
    # hidden geometry box — that box is gone. A per-fragment depth pass
    # (review requirements 4-5, future face/edge channel) would erase the seam.
    assert diff < 400, f"hidden cube bleeds through plate: {diff} px differ"


def test_hidden_line_elimination_scales_to_assembly_sizes():
    """v2.16.2 性能回归：8 件变速箱规模的边×三角形不得再退化成分钟级。

    旧实现对每条边逐三角形做 Python 级裁剪；7.3k 边 × 10k 面实测 185s，
    超过 worker 120s 超时导致装配导出失败。宽相位网格 + 向量化后应秒级。
    """
    import time

    import numpy as np

    r = Renderer()
    rng = np.random.default_rng(7)
    n_tris = 9000
    # 面向 -Z 相机的密集三角形网格（相机在 +Z 方向）
    base = rng.uniform(-50, 50, size=(n_tris, 2))
    tris = np.stack([base, base + rng.uniform(0.5, 2.0, size=(n_tris, 2)),
                     base + rng.uniform(0.5, 2.0, size=(n_tris, 2))], axis=1)
    z = rng.uniform(-5, 5, size=(n_tris, 3))
    tris = np.concatenate([tris, z[:, :, None]], axis=2)
    # 法向朝 +Z（面向相机）
    segments = [((float(rng.uniform(-50, 50)), float(rng.uniform(-50, 50)), 0.0),
                 (float(rng.uniform(-50, 50)), float(rng.uniform(-50, 50)), 0.0))
                for _ in range(7000)]
    t0 = time.time()
    out = r._visible_edge_segments(segments, tris, (0.0, 0.0, 1.0))
    dt = time.time() - t0
    assert dt < 20.0, f"hidden-line elimination too slow: {dt:.1f}s for 7k segments"
    assert isinstance(out, list)
