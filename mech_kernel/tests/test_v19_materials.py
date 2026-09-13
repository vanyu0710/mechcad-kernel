"""v2.19 材质系统 + 软件光栅化渲染器测试。

背景：matplotlib 后端是画家算法（无深度缓冲），封闭壳体总会盖住内部零件——
无法做剖切/装配遮挡。OCC 原生 AIS 离屏在无图形驱动的机器上不可用。故新增
numpy 软件渲染器（z-buffer + 逐面 Blinn-Phong 材质光照 + SSAA）。
"""
import io
import math

from PIL import Image

from mech_kernel.materials import MATERIALS, resolve_material, material_color, material_edge
from mech_kernel.software_renderer import SoftwareRenderer, look_at


def _cube(cx, cy, cz, s):
    h = s / 2.0
    v = [(cx - h, cy - h, cz - h), (cx + h, cy - h, cz - h),
         (cx + h, cy + h, cz - h), (cx - h, cy + h, cz - h),
         (cx - h, cy - h, cz + h), (cx + h, cy - h, cz + h),
         (cx + h, cy + h, cz + h), (cx - h, cy + h, cz + h)]
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
         [0, 1, 5], [0, 5, 4], [2, 3, 7], [2, 7, 6],
         [1, 2, 6], [1, 6, 5], [0, 4, 7], [0, 7, 3]]
    return {"vertices": v, "faces": f}


def _png_ok(data):
    return isinstance(data, bytes) and len(data) > 200 and data[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------- materials ----------------

def test_materials_have_required_fields():
    for name, mat in MATERIALS.items():
        assert len(mat) == 6, name
        base, amb, dif, spec, shin, edge = mat
        assert len(base) == 3 and all(0.0 <= c <= 1.0 for c in base)
        assert 0.0 <= amb <= 1.0 and 0.0 <= dif <= 1.0 and 0.0 <= spec <= 1.0
        assert shin > 0


def test_resolve_material_by_part_name():
    assert resolve_material("箱体") == "cast_iron"
    assert resolve_material("箱体盖") == "cast_iron"
    assert resolve_material("输入轴") == "steel"
    assert resolve_material("从动齿轮_1挡_z42") == "steel"
    assert resolve_material("同步器毂_1") == "dark_steel"
    # 拨叉优先于"轴"匹配
    assert resolve_material("拨叉轴_1") == "bronze"
    assert resolve_material("unknown_widget") == "steel"


def test_material_color_and_edge():
    assert material_color("bronze") == MATERIALS["bronze"][0]
    assert material_edge("cast_iron") == MATERIALS["cast_iron"][5]


# ---------------- look_at ----------------

def test_look_at_orthonormal_basis():
    eye, right, up, fwd = look_at((10, 0, 0), (0, 0, 0))
    for v in (right, up, fwd):
        assert abs(float(sum(c * c for c in v)) - 1.0) < 1e-9
    assert abs(float(sum(a * b for a, b in zip(right, up)))) < 1e-9
    assert abs(float(sum(a * b for a, b in zip(right, fwd)))) < 1e-9
    assert fwd[0] < -0.99  # 从 +X 看向原点 → forward ≈ -X


# ---------------- software renderer ----------------

def test_software_render_returns_png_and_autofit():
    r = SoftwareRenderer(width=240, height=180, ssaa=1)
    png = r.render([_cube(0, 0, 0, 10)])
    assert _png_ok(png)
    im = Image.open(io.BytesIO(png))
    assert im.size == (240, 180)


def test_zbuffer_hides_far_object_behind_near():
    """近块与远块投影重叠的区域必须显示近块（z-buffer 正确遮挡）。

    远块比近块小且同轴，其投影完全落在近块轮廓内 → 两图在这些像素上必须
    完全一致；只有近块之外的背景区才可能出现远块（本测试把远块放到完全被
    覆盖的位置，故重叠区零差异即为通过）。
    """
    import numpy as np
    near = _cube(0, 0, 0, 20)          # 大块（近）
    far = _cube(0, 0, -6, 14)          # 小块（更远，投影完全落在近块内）
    # 纯色背景，避免垂直渐变污染"近块像素"判定
    r = SoftwareRenderer(width=200, height=200, ssaa=1,
                         background=(0.96, 0.97, 0.98), bg_top=(0.96, 0.97, 0.98))
    eye = (0.0, -60.0, 0.0)            # 沿 +Y 看
    with_far = np.asarray(Image.open(io.BytesIO(
        r.render([near, far], eye=eye, target=(0, 0, 0)))).convert("RGB")).astype(int)
    without = np.asarray(Image.open(io.BytesIO(
        r.render([near], eye=eye, target=(0, 0, 0)))).convert("RGB")).astype(int)
    # 近块的投影像素集合（与背景不同处）
    bg = without[2, 2]
    near_mask = np.abs(without - bg).sum(axis=2) > 12
    overlap_diff = int(np.count_nonzero(np.abs(with_far - without).sum(axis=2)[near_mask] > 0))
    assert overlap_diff == 0, f"far object bled through the near block: {overlap_diff} px"


def test_material_changes_rendered_color():
    """不同材质应有不同的像素统计（材质真的参与着色）。"""
    import numpy as np
    r = SoftwareRenderer(width=180, height=180, ssaa=1)
    def mean_rgb(mat):
        m = _cube(0, 0, 0, 10)
        m["material"] = mat
        im = Image.open(io.BytesIO(r.render([m]))).convert("RGB")
        arr = np.asarray(im).reshape(-1, 3).astype(float)
        return arr.mean(axis=0)
    steel = mean_rgb("steel")
    bronze = mean_rgb("bronze")
    assert abs(steel[2] - bronze[2]) > 8.0, "钢材与青铜的蓝色通道应明显不同"


def test_face_color_override_wins():
    import numpy as np
    r = SoftwareRenderer(width=160, height=160, ssaa=1)
    a = _cube(0, 0, 0, 10); a["material"] = "steel"; a["color"] = (0.9, 0.1, 0.1)
    b = _cube(0, 0, 0, 10); b["material"] = "steel"; b["color"] = (0.1, 0.1, 0.9)
    ia = Image.open(io.BytesIO(r.render([a]))).convert("RGB")
    ib = Image.open(io.BytesIO(r.render([b]))).convert("RGB")
    ma = np.asarray(ia).reshape(-1, 3).astype(float).mean(axis=0)
    mb = np.asarray(ib).reshape(-1, 3).astype(float).mean(axis=0)
    assert ma[0] > mb[0]   # 红色版更红
    assert mb[2] > ma[2]   # 蓝色版更蓝


def test_bounding_sphere_fit_keeps_object_in_frame():
    """自动取景：模型应占据画面中心区域（不是被裁掉）。"""
    import numpy as np
    r = SoftwareRenderer(width=220, height=220, ssaa=1)
    png = r.render([_cube(500, -300, 120, 40)])  # 远离原点
    arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(int)
    bg = arr[2, 2]
    non_bg = np.count_nonzero(np.abs(arr - bg).sum(axis=2) > 12)
    assert non_bg > 1200, f"object not framed: only {non_bg} non-background pixels"


def test_empty_mesh_list_returns_background():
    r = SoftwareRenderer(width=120, height=90, ssaa=1)
    png = r.render([])
    assert _png_ok(png)
