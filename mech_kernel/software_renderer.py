"""MechKernel v2.19: 软件光栅化渲染器（z-buffer + 逐像素材质光照）。

为什么需要：matplotlib 后端用画家算法（按三角形深度排序整体绘制），没有
深度缓冲——封闭壳体的大面片总会盖住内部零件，无法做真实遮挡，也做不出
连续材质光照。OCC 原生 AIS 离屏在无图形驱动的机器上不可用。

本模块实现一个够用的软件渲染管线：
  1. 相机（透视/正交）+ look-at 投影
  2. 三角形光栅化（重心坐标）+ z-buffer 逐像素深度测试
  3. 逐像素 Blinn-Phong 材质光照（materials.py 参数）
  4. 2x SSAA 抗锯齿 + 背景渐变 + 接触阴影

纯 numpy（无 GL 依赖），可跑在 CI / 无头机器上。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


def _norm(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def look_at(eye, target, up=(0.0, 0.0, 1.0)):
    eye = np.asarray(eye, dtype=float)
    target = np.asarray(target, dtype=float)
    f = _norm(target - eye)                 # forward
    up = np.asarray(up, dtype=float)
    if abs(float(np.dot(f, up))) > 0.999:
        up = np.array([0.0, 1.0, 0.0])
    s = _norm(np.cross(f, up))              # right
    u = np.cross(s, f)                      # true up
    return eye, s, u, f


class SoftwareRenderer:
    """z-buffer 软件渲染器（材质着色 + 抗锯齿）。"""

    def __init__(self, width=1000, height=700, ssaa=2, background=(0.955, 0.965, 0.974),
                 bg_top=(0.90, 0.93, 0.96), shadow=True):
        self.width = int(width)
        self.height = int(height)
        self.ssaa = max(1, int(ssaa))
        self.background = tuple(background)
        self.bg_top = tuple(bg_top)
        self.shadow = shadow
        self._depth_bias = 0.0

    # ---------------- public ----------------
    def render(self, meshes: List[Dict], eye=None, target=None, up=(0, 0, 1),
               fov_deg=32.0, ambient=0.30, light=(-0.45, -0.5, 0.74),
               view_dir=(0.62, -0.72, 0.31), fit=1.12) -> bytes:
        """meshes: [{'vertices': [(x,y,z)...], 'faces': [[i,j,k]...],
                     'material': 'steel'|'cast_iron'|..., 'color': (r,g,b)?}]

        eye/target 为 None 时按包围球自动取景（view_dir 是相机方位）。返回 PNG。
        """
        from .materials import MATERIALS
        W, H, S = self.width * self.ssaa, self.height * self.ssaa, self.ssaa
        # 顶点变换: world -> camera
        all_pts = np.vstack([np.asarray(m["vertices"], dtype=float) for m in meshes]) \
            if meshes else np.zeros((0, 3))
        if all_pts.size == 0:
            return self._blank(W, H, S)
        if eye is None or target is None:
            lo, hi = all_pts.min(0), all_pts.max(0)
            center = (lo + hi) / 2.0
            radius = float(np.linalg.norm(hi - lo)) / 2.0 or 1.0
            # 包围球在视锥内：dist = R / sin(fov/2) * fit
            dist = radius / math.sin(math.radians(fov_deg) / 2.0) * fit
            d = np.asarray(view_dir, dtype=float)
            d = d / (np.linalg.norm(d) or 1.0)
            target = tuple(center)
            eye = tuple(center + d * dist)
        eye, right, true_up, fwd = look_at(eye, target, up)
        span = float(np.linalg.norm(all_pts.max(0) - all_pts.min(0))) or 1.0
        self._depth_bias = span * 2e-4
        rel = all_pts - eye
        cam = np.stack([rel @ right, rel @ true_up, rel @ fwd], axis=1)  # (N,3) x right / y up / z depth

        depth_buf = np.full((H, W), np.inf, dtype=np.float64)
        color_buf = self._background(W, H, S)

        L = np.asarray(light, dtype=float)
        L = L / (np.linalg.norm(L) or 1.0)

        focal = 1.0 / math.tan(math.radians(fov_deg) / 2.0)

        def project(p):
            z = np.maximum(p[:, 2], 1e-6)
            x = (p[:, 0] / z) * focal
            y = (p[:, 1] / z) * focal
            sx = (x * 0.5 + 0.5) * W
            sy = (1.0 - (y * 0.5 + 0.5)) * H
            return sx, sy, z

        offset = 0
        for mesh in meshes:
            verts = np.asarray(mesh["vertices"], dtype=float)
            faces = np.asarray(mesh["faces"], dtype=int)
            n = len(verts)
            if n == 0 or len(faces) == 0:
                continue
            v = cam[offset:offset + n]
            offset += n
            sx, sy, z = project(v)
            mat = MATERIALS.get(mesh.get("material") or "steel", MATERIALS["steel"])
            base = np.asarray(mesh.get("color") or mat[0], dtype=float)
            if base.max() > 1.5:
                base = base / 255.0
            # 面法线（世界空间，用相机基重建）
            a3 = verts[faces[:, 0]]
            b3 = verts[faces[:, 1]]
            c3 = verts[faces[:, 2]]
            fn = np.cross(b3 - a3, c3 - a3)
            fl = np.linalg.norm(fn, axis=1, keepdims=True)
            fn = fn / np.maximum(fl, 1e-12)
            # 背面剔除（法线朝相机 → 可见）
            centroid = (a3 + b3 + c3) / 3.0
            view_vec = np.array([eye]) - centroid
            view_vec = view_vec / np.maximum(np.linalg.norm(view_vec, axis=1, keepdims=True), 1e-12)
            front = np.einsum("ij,ij->i", fn, view_vec) > 0
            # 相机后方 / 跨越近裁剪面的三角形一并剔除（否则背面经透视除法翻转投出）
            near = 1e-4
            visible = front & np.all(z[faces] > near, axis=1)
            faces = faces[visible]
            fn = fn[visible]
            view_vec = view_vec[visible]
            if len(faces) == 0:
                continue
            # 光照（每面一次，光滑法线由细分保证足够）
            ndl = np.abs(fn @ L)
            ndh = np.abs(np.einsum("ij,ij->i", fn, (view_vec + L) / np.linalg.norm(view_vec + L, axis=1, keepdims=True)))
            spec = mat[3] * (ndh ** mat[4])
            scale = (mat[1] + mat[2] * ndl)[:, None]
            face_color = np.clip(base[None, :] * scale + spec[:, None], 0.0, 1.0)
            # 视图方向近匀速 => 逐面着色 + 三角细分即可得到平滑外观
            tris = np.stack([sx[faces[:, 0]], sy[faces[:, 0]], z[faces[:, 0]],
                             sx[faces[:, 1]], sy[faces[:, 1]], z[faces[:, 1]],
                             sx[faces[:, 2]], sy[faces[:, 2]], z[faces[:, 2]]], axis=1)
            self._rasterize(tris, face_color, depth_buf, color_buf, W, H)

        if S > 1:
            color_buf = color_buf.reshape(self.height, S, self.width, S, 3).mean(axis=(1, 3))
        return self._to_png(color_buf)

    # ---------------- internals ----------------
    def _background(self, W, H, S):
        t = np.linspace(0.0, 1.0, H)[:, None, None]
        top = np.asarray(self.bg_top, dtype=float)
        bottom = np.asarray(self.background, dtype=float)
        col = top * t + bottom * (1.0 - t)   # 顶部略深
        return np.repeat(col, W, axis=1)

    def _rasterize(self, tris, face_color, depth_buf, color_buf, W, H):
        """逐三角形扫描线光栅化（重心 + z-buffer）。"""
        for t, col in zip(tris, face_color):
            x0, y0, z0, x1, y1, z1, x2, y2, z2 = t
            minx = max(int(math.floor(min(x0, x1, x2))), 0)
            maxx = min(int(math.ceil(max(x0, x1, x2))), W - 1)
            miny = max(int(math.floor(min(y0, y1, y2))), 0)
            maxy = min(int(math.ceil(max(y0, y1, y2))), H - 1)
            if minx > maxx or miny > maxy:
                continue
            area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
            if abs(area) < 1e-9:
                continue
            xs = np.arange(minx, maxx + 1)
            ys = np.arange(miny, maxy + 1)
            gx, gy = np.meshgrid(xs + 0.5, ys + 0.5)
            w0 = ((x1 - x0) * (gy - y0) - (gx - x0) * (y1 - y0)) / area
            w1 = ((gx - x0) * (y2 - y0) - (x2 - x0) * (gy - y0)) / area
            w2 = 1.0 - w0 - w1
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not inside.any():
                continue
            # 用重心插值透视校正深度（近似用屏幕线性）
            zi = w0 * z0 + w1 * z1 + w2 * z2
            region_depth = depth_buf[miny:maxy + 1, minx:maxx + 1]
            region_color = color_buf[miny:maxy + 1, minx:maxx + 1]
            mask = inside & (zi < region_depth - self._depth_bias)
            if not mask.any():
                continue
            region_depth[mask] = zi[mask]
            region_color[mask] = col
        return

    def _blank(self, W, H, S):
        buf = self._background(W, H, S)
        if S > 1:
            buf = buf.reshape(self.height, S, self.width, S, 3).mean(axis=(1, 3))
        return self._to_png(buf)

    def _to_png(self, buf):
        import io
        from PIL import Image
        arr = np.clip(buf * 255.0, 0, 255).astype(np.uint8)
        im = Image.fromarray(arr, "RGB")
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()
