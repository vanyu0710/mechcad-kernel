"""
MechKernel Renderer（v2.5 工程视觉版）

第 4 轮专家审查修复：
- P0-1 缓存键：用 (id, geometry_revision, level, config) 替代裸 id
- P0-4 异常隔离：坏几何/NaN/空/缺顶点不崩
- LRU 限制（默认 32 个）
- 任何异常都隔离，**绝不**让 kernel 崩溃
- 面向视觉模型的干净正交/ISO 工程证据图，不输出调试坐标轴
- 默认使用无网格线实体着色；需要检查拓扑时可打开 show_edges

v2.15 渲染质量修复（"面上乱三角和线条"）：
- OCP BRepMesh 角向细分：曲面相邻三角形法线差被硬性封顶，粗圆角不再成阶梯
- show_edges 改为只画特征边（二面角 > SHARP_EDGE_DEG），平面扇形三角化的
  对角线噪声全部消失；边以朝视点的细 quad 并入面片集合，靠 mplot3d 的
  per-patch 深度排序被正面正确遮挡（Line3DCollection 整体绘制会穿面浮线）
- crease-aware 平滑法线：曲面色带抹平，棱角处法线不跨 crease 聚类混合
"""
from typing import Any, Optional, Dict, List, Tuple
from collections import OrderedDict
import io
import math

try:
    import matplotlib
    matplotlib.use("Agg")
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class Renderer:
    """
    离屏 CAD 证据图渲染器（duck-typed）。
    
    缓存策略：
    - 缓存键 = (geometry_id, geometry_revision, render_level, config_signature)
    - LRU 限制（默认 32 个）
    - 任何异常都隔离，**绝不**让 kernel 崩溃
    """

    # Feature-edge dihedral threshold: edges whose adjacent faces differ by
    # more than this are real CAD edges and get drawn (v2.15).
    SHARP_EDGE_DEG = 30.0
    # Vertex-normal smoothing must not cross a crease this wide, otherwise
    # box corners would blur into fillet-like gradients.
    CREASE_DEG = 30.0
    # OCP tessellation angular cap (~20 deg max per facet step).
    ANG_DEFLECT_RAD = 0.35
    # Chord tolerance as a share of the part diagonal, clamped.
    LIN_DEFLECT_RATIO = 0.0004
    LIN_DEFLECT_MIN = 0.005
    LIN_DEFLECT_MAX = 0.1
    # Groups above this triangle count skip the O(n) style analysis and fall
    # back to flat shading without edge lines (keeps huge meshes renderable).
    STYLE_BUDGET = 400_000
    EDGE_RGB = (0.16, 0.22, 0.30, 0.55)

    def __init__(
        self,
        image_size: tuple = (640, 480),
        dpi: int = 80,
        cache_size: int = 32,
        use_cache: bool = True,
        backend: str = "auto",
        show_edges: bool = False,
        background: tuple = (0.965, 0.972, 0.980),
        body_color: tuple = (0.36, 0.56, 0.76),
    ):
        self.image_size = image_size
        self.dpi = dpi
        self._cache_size = cache_size
        self._use_cache = use_cache
        if backend not in ("auto", "occ", "matplotlib"):
            raise ValueError(f"不支持的渲染后端: {backend}")
        self.backend = backend
        self._occ_probe_failed = False
        self._occ_failure_warning = ""
        self.show_edges = show_edges
        self.background = background
        self.body_color = body_color
        self._cache: "OrderedDict[Tuple, Dict[str, bytes]]" = OrderedDict()
        self._config_signature = (image_size, dpi, show_edges, background, body_color)
    
    def render(
        self, 
        geometry: Any, 
        level: str = "iso_only",
        geometry_revision: int = 0,
        views: Optional[List[str]] = None,
        annotate: bool = True,
        turntable: bool = False,
        image_size: Optional[Tuple[int, int]] = None,
        quality: str = "evidence",
        backend: str = "auto",
        show_edges: bool = False,
        highlight: Optional[List[str]] = None,
        scene: Any = None,
    ) -> Dict[str, bytes]:
        """
        渲染几何到 PNG bytes（v2.2：多视图 / 标注 / 转台 / 自定义尺寸）。
        
        Args:
            geometry: 几何对象（duck-typed）
            level: "none" | "iso_only" | "full"
            geometry_revision: kernel 维护的版本号（用于缓存键）
            views: 需要渲染的视角名列表（None = 全部 4 视角）
            annotate: 是否叠加视图名 + 包围盒尺寸标注
            turntable: 是否追加 4 个 45° 转台视角（rot0/rot90/rot180/rot270）
            image_size: 覆盖默认分辨率 (w, h)
        
        Returns:
            {"iso": bytes, "front": bytes, ...}
            任何失败都返回空 dict，绝不抛异常
        """
        backend = self.backend if backend == "auto" and self.backend != "auto" else backend
        self.last_backend_requested = backend
        self.last_backend_used = "none"
        self.last_warnings = []
        self.last_quality = quality
        # P0-4: 异常隔离 - 在函数入口就 try
        try:
            if backend not in ("auto", "occ", "matplotlib"):
                raise ValueError(f"不支持的渲染后端: {backend}")
            if quality not in ("evidence", "presentation"):
                raise ValueError(f"不支持的渲染质量: {quality}")
            if backend in ("auto", "occ") and not (backend == "auto" and self._occ_probe_failed):
                try:
                    from .occ_renderer import OCCRenderer
                    native = OCCRenderer().render(
                        geometry, views or [], max(image_size or self.image_size),
                        annotate=annotate, highlight=highlight, scene=scene,
                    )
                    self.last_backend_used = "occ"
                    return native
                except Exception as exc:
                    self._occ_probe_failed = True
                    self._occ_failure_warning = str(exc)
                    self.last_warnings.append(self._occ_failure_warning)
            elif backend == "auto" and self._occ_failure_warning:
                self.last_warnings.append(self._occ_failure_warning)
            result = self._render_safe(
                geometry, level, geometry_revision,
                views=views, annotate=annotate, turntable=turntable,
                image_size=image_size, quality=quality, show_edges=show_edges,
                highlight=highlight, scene=scene,
            )
            self.last_backend_used = "matplotlib" if any(result.values()) else "none"
            return result
        except Exception as exc:
            self.last_warnings.append(str(exc))
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
    
    def _render_safe(
        self,
        geometry: Any,
        level: str,
        geometry_revision: int,
        views: Optional[List[str]] = None,
        annotate: bool = True,
        turntable: bool = False,
        image_size: Optional[Tuple[int, int]] = None,
        quality: str = "evidence",
        show_edges: bool = False,
        highlight: Optional[List[str]] = None,
        scene: Any = None,
    ) -> Dict[str, bytes]:
        if level == "none" or geometry is None:
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
        
        # P0-1: 缓存键包含 revision、level 与渲染配置（视图/标注/转台/尺寸）
        render_config = (
            annotate, tuple(sorted(views or [])), turntable, image_size, quality,
            show_edges, tuple(sorted(highlight or [])), self._scene_signature(scene),
        )
        cache_key = self._make_cache_key(geometry, level, geometry_revision, tolerance=0.1,
                                         render_config=render_config)
        if self._use_cache and cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]
        
        # 提取 mesh（P0-4: 异常隔离）
        try:
            mesh_groups = self._extract_scene_meshes(geometry, scene, highlight or [])
            vertices = [vertex for group in mesh_groups for vertex in group[0]]
            faces = []
            offset = 0
            for group_vertices, group_faces, _, _ in mesh_groups:
                faces.extend([[index + offset for index in face] for face in group_faces])
                offset += len(group_vertices)
        except Exception:
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}

        # v2.15: per-group feature edges + smooth normals, computed once for
        # all views. A failure (or over-budget group) yields None and that
        # group renders as a plain flat-shaded triangle soup.
        try:
            mesh_styles = [
                self._analyze_group(group_vertices, group_faces)
                if len(group_faces) <= self.STYLE_BUDGET else None
                for group_vertices, group_faces, _, _ in mesh_groups
            ]
        except Exception:
            mesh_styles = [None] * len(mesh_groups)
        
        if not vertices or not faces:
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
        
        # P0-4: bbox 计算异常隔离
        try:
            bbox = self._compute_bbox(vertices)
        except Exception:
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
        
        if bbox is None:
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
        
        # P0-4: 检查 NaN/Inf
        if any(not math.isfinite(x) for x in bbox):
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
        
        if not MATPLOTLIB_AVAILABLE or not PIL_AVAILABLE:
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
        
        # 计算视图参数
        try:
            cx, cy, cz = self._bbox_center(bbox)
            size = self._bbox_size(bbox)
            max_dim = max(size) if size else 1.0
            padding = max_dim * 0.3
            lim = max_dim / 2 + padding
        except Exception:
            return {"iso": None, "front": None, "top": None, "side": None, "default": None}
        
        view_configs = [
            ("iso", (cx + lim, cy + lim, cz + lim)),
            ("front", (cx, cy - lim * 2, cz)),
            ("top", (cx, cy, cz + lim * 2)),
            ("side", (cx + lim * 2, cy, cz)),
        ]
        if turntable:
            view_configs += [
                ("rot0", (cx + lim, cy, cz + lim)),
                ("rot90", (cx, cy + lim, cz + lim)),
                ("rot180", (cx - lim, cy, cz + lim)),
                ("rot270", (cx, cy - lim, cz + lim)),
            ]
        if views is not None:
            wanted = set(views)
            if turntable:
                wanted.update({"rot0", "rot90", "rot180", "rot270"})
            view_configs = [(n, p) for n, p in view_configs if n in wanted]
        if level == "iso_only":
            view_configs = [(n, p) for n, p in view_configs if n == "iso"]
        
        dims_label = f"{bbox[3]-bbox[0]:.0f} x {bbox[4]-bbox[1]:.0f} x {bbox[5]-bbox[2]:.0f} mm"
        views = {}
        for view_name, camera_pos in view_configs:
            try:
                png_bytes = self._render_view(
                    vertices, faces, bbox, camera_pos, view_name,
                    cx, cy, cz, lim, size, image_size=image_size,
                    mesh_groups=mesh_groups, quality=quality, show_edges=show_edges,
                    mesh_styles=mesh_styles,
                )
                if png_bytes and annotate:
                    png_bytes = self._annotate_png(png_bytes, f"{view_name.upper()}  |  {dims_label}")
                views[view_name] = png_bytes if png_bytes else None
            except Exception:
                # P0-4: 单个视图失败不影响其他
                views[view_name] = None
        
        views["default"] = views.get("iso")
        
        # P0-1: 缓存（含 LRU 限制）
        if self._use_cache and any(views.values()):
            self._cache[cache_key] = views
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        
        return views

    @staticmethod
    def _scene_signature(scene: Any) -> tuple:
        if not scene:
            return ()
        values = scene.values() if isinstance(scene, dict) else scene
        result = []
        for item in values:
            get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
            result.append((get("id"), get("visible", True), tuple(get("color", []))))
        return tuple(result)

    def _extract_scene_meshes(self, geometry: Any, scene: Any, highlight: List[str]) -> list:
        if not scene:
            vertices, faces = self._extract_mesh(geometry)
            return [(vertices, faces, self.body_color, False)]
        groups = []
        values = scene.values() if isinstance(scene, dict) else scene
        for item in values:
            get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
            if not get("visible", True):
                continue
            item_geometry = get("geometry")
            if item_geometry is None:
                continue
            vertices, faces = self._extract_mesh(item_geometry)
            groups.append((vertices, faces, tuple(get("color", self.body_color)), get("id") in highlight))
        # Keep an all-hidden assembly visually empty; falling back to the fused
        # solid would make instance visibility controls misleading.
        return groups
    
    def _annotate_png(self, png_bytes: bytes, label: str) -> bytes:
        """在 PNG 左上角叠加工程视图标签（失败则原图返回）。"""
        from PIL import Image, ImageDraw, ImageFont
        try:
            img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            d = ImageDraw.Draw(img)
            font = ImageFont.load_default()
            x, y = 12, 10
            tb = d.textbbox((x, y), label, font=font)
            d.rounded_rectangle(
                [tb[0] - 7, tb[1] - 5, tb[2] + 7, tb[3] + 5],
                radius=3,
                fill=(255, 255, 255, 232),
                outline=(190, 199, 210, 220),
                width=1,
            )
            d.text((x, y), label, font=font, fill=(30, 40, 52, 255))
            out = io.BytesIO()
            img.convert("RGB").save(out, format="PNG")
            return out.getvalue()
        except Exception:
            return png_bytes

    @staticmethod
    def compose_grid(
        views: Dict[str, bytes], cols: int = 2, include_titles: bool = False,
        max_size: Optional[int] = None,
    ) -> Optional[bytes]:
        """把多张视图拼成紧凑证据包，避免重复标题占用视觉 token。"""
        from PIL import Image, ImageDraw
        try:
            items = [(k, v) for k, v in views.items() if v]
            if not items:
                return None
            imgs = []
            for k, v in items:
                im = Image.open(io.BytesIO(v)).convert("RGB")
                imgs.append((k, im))
            w = max(im.width for _, im in imgs)
            h = max(im.height for _, im in imgs)
            rows = math.ceil(len(imgs) / cols)
            pad, title_h = 4, 16 if include_titles else 0
            grid = Image.new("RGB", (cols*w + (cols+1)*pad, rows*(h+title_h) + (rows+1)*pad), (246, 248, 251))
            d = ImageDraw.Draw(grid)
            for idx, (k, im) in enumerate(imgs):
                r, c = divmod(idx, cols)
                x = pad + c*(w+pad)
                y = pad + r*(h+title_h+pad)
                grid.paste(im, (x, y + title_h))
                if include_titles:
                    d.text((x + 4, y + 1), k.upper(), fill=(0, 0, 0))
            if max_size is not None:
                grid.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            grid.save(out, format="PNG")
            return out.getvalue()
        except Exception:
            return None

    def _make_cache_key(
        self, 
        geometry: Any, 
        level: str, 
        geometry_revision: int,
        tolerance: float = 0.1,
        render_config: tuple = (),
    ) -> Tuple:
        """P0-1 修复：缓存键包含 revision + level + config
        P1-3（v8）：+ tolerance；v2.2：+ render_config（视图/标注/转台/尺寸）
        """
        return (
            id(geometry),
            geometry_revision,
            tolerance,
            level,
            self._config_signature,
            render_config,
        )
    
    def _lin_deflection(self, geometry: Any, fallback: float) -> float:
        """Chord tolerance scaled to the part size, clamped to sane bounds."""
        try:
            diagonal = geometry.bounding_box().diagonal
            if diagonal and math.isfinite(diagonal) and diagonal > 0:
                return min(self.LIN_DEFLECT_MAX,
                           max(self.LIN_DEFLECT_MIN, diagonal * self.LIN_DEFLECT_RATIO))
        except Exception:
            pass
        return fallback

    def _tessellate_ocp(self, geometry: Any, lin_deflection: float) -> Optional[Tuple[List, List]]:
        """Tessellate through OCP with both linear and angular deflection caps.

        Walks every face's Poly_Triangulation, applies the face location, and
        welds coincident node positions across faces so shared edges land on
        the same vertex indices (the renderer's edge/normal analysis needs a
        watertight index structure). Face orientation is applied to keep all
        windings outward-facing. Any failure returns None and the caller falls
        back to the duck-typed paths below.
        """
        shape = getattr(geometry, "wrapped", None)
        if shape is None:
            return None
        try:
            from OCP.BRep import BRep_Tool
            from OCP.BRepMesh import BRepMesh_IncrementalMesh
            from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
            from OCP.TopExp import TopExp_Explorer
            from OCP.TopLoc import TopLoc_Location
            from OCP.TopoDS import TopoDS

            BRepMesh_IncrementalMesh(
                shape, float(lin_deflection), False, self.ANG_DEFLECT_RAD, True
            )
            verts: List = []
            tris: List = []
            vmap: Dict = {}
            explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
            while explorer.More():
                face = TopoDS.Face_s(explorer.Current())
                face_reversed = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
                location = TopLoc_Location()
                triangulation = BRep_Tool.Triangulation_s(face, location)
                if triangulation is not None:
                    transform = location.Transformation()
                    index_map: List[int] = []
                    for i in range(1, triangulation.NbNodes() + 1):
                        point = triangulation.Node(i).Transformed(transform)
                        key = (round(point.X(), 5), round(point.Y(), 5), round(point.Z(), 5))
                        idx = vmap.get(key)
                        if idx is None:
                            idx = len(verts)
                            vmap[key] = idx
                            verts.append((point.X(), point.Y(), point.Z()))
                        index_map.append(idx)
                    for i in range(1, triangulation.NbTriangles() + 1):
                        triangle = triangulation.Triangle(i)
                        a = index_map[triangle.Value(1) - 1]
                        b = index_map[triangle.Value(2) - 1]
                        c = index_map[triangle.Value(3) - 1]
                        if face_reversed:
                            b, c = c, b
                        tris.append([a, b, c])
                explorer.Next()
            if len(verts) >= 4 and len(tris) >= 4:
                return verts, tris
        except Exception:
            return None
        return None

    def _extract_mesh(self, geometry: Any, tolerance: float = 0.1) -> Tuple[List, List]:
        """提取 vertices 和 faces（P0-4 异常隔离版）
        
        支持多种接口（duck-typed）：
        1. .tessellate(tolerance) → (vertices, triangles)  ← 优先（build123d 真实 mesh）
        2. .vertices / .faces（property 或 method）
        3. .to_trimesh() → trimesh object
        4. .bounds + .vertices + .faces
        """
        vertices = None
        faces = None

        # 0. OCP BRepMesh（v2.15 首选）：带角向细分上限。build123d 的
        # tessellate 只约束线性弦差，小圆角/齿面会切出几十度的粗刻面，
        # 着色与特征边都救不回来；角向封顶从源头解决。
        ocp_mesh = self._tessellate_ocp(geometry, self._lin_deflection(geometry, tolerance))
        if ocp_mesh is not None:
            return ocp_mesh

        # 1. 优先用 tessellate（build123d 真实 mesh）
        # P1-3（v8 DeepSeek）：缓存键含 tolerance（防止 0.1/0.01 缓存混淆）
        if hasattr(geometry, "tessellate"):
            try:
                tess = geometry.tessellate(tolerance)
                v_arr, f_arr = tess
                # v_arr: list of Vector (build123d) or list of tuples
                v_list = []
                for v in v_arr:
                    if hasattr(v, 'X') and hasattr(v, 'Y') and hasattr(v, 'Z'):
                        v_list.append((v.X, v.Y, v.Z))
                    elif isinstance(v, (list, tuple)) and len(v) >= 3:
                        v_list.append((v[0], v[1], v[2]))
                    elif hasattr(v, 'tolist'):
                        t = v.tolist()
                        if len(t) >= 3:
                            v_list.append(tuple(t[:3]))
                # f_arr: list of (i, j, k) tuples
                f_list = [list(f) for f in f_arr if isinstance(f, (list, tuple)) and len(f) >= 3]
                if len(v_list) >= 4 and len(f_list) >= 4:
                    return v_list, f_list
            except Exception:
                pass
        
        # 2. vertices/faces（property 或 method）
        if hasattr(geometry, "vertices") and hasattr(geometry, "faces"):
            try:
                v = geometry.vertices
                f = geometry.faces
                v = v() if callable(v) else v
                f = f() if callable(f) else f
                # 转换 build123d Vertex/Face 对象到 (x,y,z) / [indices]
                vertices = self._convert_vertices(v)
                faces = self._convert_faces(f)
                if len(vertices) >= 4 and len(faces) >= 4:
                    return vertices, faces
            except Exception:
                pass
        
        # 3. to_trimesh()
        if hasattr(geometry, "to_trimesh"):
            try:
                tm = geometry.to_trimesh()
                if hasattr(tm, "vertices"):
                    vertices = tm.vertices.tolist() if hasattr(tm.vertices, "tolist") else list(tm.vertices)
                if hasattr(tm, "faces"):
                    faces = tm.faces.tolist() if hasattr(tm.faces, "tolist") else list(tm.faces)
                if vertices and faces:
                    return vertices, faces
            except Exception:
                pass
        
        # 4. bounds + vertices + faces
        if hasattr(geometry, "bounds") and hasattr(geometry, "faces"):
            try:
                vertices = geometry.vertices.tolist() if hasattr(geometry.vertices, "tolist") else list(geometry.vertices)
                faces = geometry.faces.tolist() if hasattr(geometry.faces, "tolist") else list(geometry.faces)
                if vertices and faces:
                    return vertices, faces
            except Exception:
                pass
        
        return vertices or [], faces or []
    
    def _convert_vertices(self, v) -> list:
        """转换 build123d Vertex objects → (x, y, z) tuples"""
        result = []
        for item in v:
            if hasattr(item, 'X') and hasattr(item, 'Y') and hasattr(item, 'Z'):
                # build123d Vertex
                result.append((item.X, item.Y, item.Z))
            elif isinstance(item, (tuple, list)) and len(item) >= 3:
                result.append((item[0], item[1], item[2]))
            elif hasattr(item, 'tolist'):
                # numpy array
                t = item.tolist()
                if len(t) >= 3:
                    result.append(tuple(t[:3]))
        return result
    
    def _convert_faces(self, f) -> list:
        """转换 build123d Face objects → index lists"""
        result = []
        for item in f:
            if isinstance(item, (list, tuple)):
                result.append(list(item))
            elif hasattr(item, 'tolist'):
                result.append(item.tolist())
            else:
                # build123d Face object — 用 vertices() 拿顶点
                try:
                    verts = item.vertices() if callable(item.vertices) else item.vertices
                    indices = []
                    # 但需要 build123d 内部的 vertex index，这里用近似
                    result.append(list(range(len(verts))))
                except Exception:
                    pass
        return result
    
    def _compute_bbox(self, vertices: List) -> Optional[tuple]:
        """计算包围盒（P0-4: 异常隔离）"""
        if not vertices:
            return None
        try:
            xs = [v[0] for v in vertices if len(v) >= 3]
            ys = [v[1] for v in vertices if len(v) >= 3]
            zs = [v[2] for v in vertices if len(v) >= 3]
            if not xs or not ys or not zs:
                return None
            return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
        except Exception:
            return None
    
    def _bbox_center(self, bbox: tuple) -> tuple:
        return (
            (bbox[0] + bbox[3]) / 2,
            (bbox[1] + bbox[4]) / 2,
            (bbox[2] + bbox[5]) / 2,
        )
    
    def _bbox_size(self, bbox: tuple) -> tuple:
        return (
            bbox[3] - bbox[0],
            bbox[4] - bbox[1],
            bbox[5] - bbox[2],
        )
    
    def _render_view(
        self,
        vertices: List,
        faces: List,
        bbox: tuple,
        camera_pos: tuple,
        view_name: str,
        cx: float, cy: float, cz: float,
        lim: float, size: tuple,
        image_size: Optional[Tuple[int, int]] = None,
        mesh_groups: Optional[list] = None,
        quality: str = "evidence",
        show_edges: bool = False,
        mesh_styles: Optional[list] = None,
    ) -> bytes:
        """渲染单个视角（P0-4: 异常隔离）"""
        try:
            img_size = image_size or self.image_size
            fig = plt.figure(
                figsize=(img_size[0] / self.dpi, img_size[1] / self.dpi),
                dpi=self.dpi,
                facecolor=self.background,
            )
            ax = fig.add_subplot(111, projection="3d")
            ax.set_facecolor(self.background)

            effective_edges = show_edges or quality == "presentation" or self.show_edges
            triangles = []
            triangle_colors = []
            feature_edges: List[Tuple[tuple, tuple]] = []
            groups = mesh_groups or [(vertices, faces, self.body_color, False)]
            styles = mesh_styles if mesh_styles is not None else [None] * len(groups)
            for (group_vertices, group_faces, group_color, highlighted), style in zip(groups, styles):
                color = tuple(min(1.0, float(channel) * (1.18 if highlighted else 1.0)) for channel in group_color)
                if style is not None:
                    group_triangles = style["triangles"]
                    triangles.extend(group_triangles)
                    triangle_colors.extend(
                        self._triangle_colors(group_triangles, color, normals=style["normals"])
                    )
                    if effective_edges:
                        feature_edges.extend(style["edges"])
                else:
                    # Analysed as too odd or over-budget: plain flat shading,
                    # still no per-facet lines (v2.15 removed that mode).
                    group_triangles = []
                    for face in group_faces:
                        if len(face) >= 3:
                            try:
                                group_triangles.append([
                                    group_vertices[face[0]],
                                    group_vertices[face[1]],
                                    group_vertices[face[2]],
                                ])
                            except (IndexError, TypeError):
                                continue
                    triangles.extend(group_triangles)
                    triangle_colors.extend(self._triangle_colors(group_triangles, color))

            if not triangles:
                plt.close(fig)
                return b""

            if effective_edges and feature_edges:
                edge_triangles = self._edge_ribbons(
                    feature_edges,
                    (camera_pos[0] - cx, camera_pos[1] - cy, camera_pos[2] - cz),
                    max(size) if size else 1.0,
                )
                triangles.extend(edge_triangles)
                triangle_colors.extend([self.EDGE_RGB] * len(edge_triangles))

            mesh = Poly3DCollection(
                triangles,
                facecolors=triangle_colors,
                alpha=1.0,
                edgecolor="none",
            )
            ax.add_collection3d(mesh)

            # Neutral panes retain depth cues without the default matplotlib grid.
            for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
                axis.pane.set_facecolor((*self.background, 1.0))
                axis.pane.set_edgecolor((*self.background, 1.0))
                axis._axinfo["grid"]["linewidth"] = 0.0

            # Per-axis framing keeps long parts large in their useful views.
            # The old max-dimension cube left most pixels empty for a motor.
            padding = 0.09
            half = [max(v * (0.5 + padding), 1.0) for v in size]
            ax.set_xlim(cx - half[0], cx + half[0])
            ax.set_ylim(cy - half[1], cy + half[1])
            ax.set_zlim(cz - half[2], cz + half[2])
            
            dx, dy, dz = camera_pos[0] - cx, camera_pos[1] - cy, camera_pos[2] - cz
            norm = math.sqrt(dx*dx + dy*dy + dz*dz)
            if norm > 0:
                ax.view_init(
                    elev=math.degrees(math.asin(dz / norm)),
                    azim=math.degrees(math.atan2(dx, -dy)),
                )
            
            ax.set_box_aspect((size[0] or 1, size[1] or 1, size[2] or 1))
            try:
                ax.set_proj_type("ortho")
            except AttributeError:
                pass
            ax.set_axis_off()
            fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
            
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=self.dpi, pad_inches=0, facecolor=self.background)
            plt.close(fig)
            return buf.getvalue()
        except Exception:
            try:
                plt.close("all")
            except Exception:
                pass
            return b""

    def _triangle_colors(
        self,
        triangles: List[List[Tuple[float, float, float]]],
        color: Optional[Tuple[float, float, float]] = None,
        normals: Optional[List[Tuple[float, float, float]]] = None,
    ) -> List[Tuple[float, float, float, float]]:
        """Apply stable light shading without relying on matplotlib's version-specific shade API.

        ``normals`` supplies v2.15 crease-aware smooth normals; when absent the
        flat per-triangle normal is used (legacy behaviour).
        """
        light = (-0.45, -0.55, 0.70)
        light_len = math.sqrt(sum(value * value for value in light)) or 1.0
        light = tuple(value / light_len for value in light)
        colors = []
        body_color = color or self.body_color
        for position, tri in enumerate(triangles):
            normal = normals[position] if normals is not None and position < len(normals) else None
            try:
                if normal is None:
                    ax, ay, az = (tri[1][i] - tri[0][i] for i in range(3))
                    bx, by, bz = (tri[2][i] - tri[0][i] for i in range(3))
                    nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
                    normal_len = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                    normal = (nx / normal_len, ny / normal_len, nz / normal_len)
                illumination = abs(normal[0] * light[0] + normal[1] * light[1] + normal[2] * light[2])
                factor = 0.58 + 0.42 * illumination
            except (IndexError, TypeError, ValueError):
                factor = 0.78
            colors.append(tuple(min(1.0, max(0.0, channel * factor)) for channel in body_color) + (1.0,))
        return colors

    @staticmethod
    def _edge_ribbons(
        edges: List[Tuple[tuple, tuple]],
        view_vector: Tuple[float, float, float],
        max_dim: float,
    ) -> List[List[Tuple[float, float, float]]]:
        """Expand feature edges into camera-facing ribbon quads.

        mplot3d paints each artist as one unit, so a Line3DCollection of edge
        lines would float over the front faces of the model (the old renderer
        hid this by drawing lines per facet *inside* the face collection at
        0.18pt). Ribbons join the same Poly3DCollection, where per-patch depth
        sorting occludes back-side edges behind the body correctly.
        """
        vx, vy, vz = view_vector
        vlen = math.sqrt(vx * vx + vy * vy + vz * vz) or 1.0
        vx, vy, vz = vx / vlen, vy / vlen, vz / vlen
        half_width = max(max_dim * 0.00035, 1e-4)
        offset = max_dim * 0.0008
        ribbons: List[List[Tuple[float, float, float]]] = []
        for point_a, point_b in edges:
            try:
                ex = point_b[0] - point_a[0]
                ey = point_b[1] - point_a[1]
                ez = point_b[2] - point_a[2]
            except (TypeError, IndexError):
                continue
            # ribbon width direction = edge cross view
            wx, wy, wz = ey * vz - ez * vy, ez * vx - ex * vz, ex * vy - ey * vx
            wlen = math.sqrt(wx * wx + wy * wy + wz * wz)
            if wlen < 1e-12:
                continue  # edge parallel to view: zero silhouette, skip
            wx, wy, wz = wx / wlen * half_width, wy / wlen * half_width, wz / wlen * half_width
            ox, oy, oz = vx * offset, vy * offset, vz * offset
            a1 = (point_a[0] + ox - wx, point_a[1] + oy - wy, point_a[2] + oz - wz)
            a2 = (point_a[0] + ox + wx, point_a[1] + oy + wy, point_a[2] + oz + wz)
            b1 = (point_b[0] + ox - wx, point_b[1] + oy - wy, point_b[2] + oz - wz)
            b2 = (point_b[0] + ox + wx, point_b[1] + oy + wy, point_b[2] + oz + wz)
            ribbons.append([a1, a2, b2])
            ribbons.append([a1, b2, b1])
        return ribbons

    def _analyze_group(self, vertices: List, faces: List) -> Optional[Dict]:
        """Merge coincident vertices, then derive feature edges and smooth normals.

        Raw triangle soup has both problems v2.15 fixes: planar faces expose
        fan diagonals once anything is drawn along every triangle edge, and
        flat per-triangle shading bands on curved faces. Position merging gives
        one shared structure for the dihedral edge test and the crease-aware
        normal clustering. Returns None when the input cannot be analysed; the
        caller then falls back to plain flat shading.
        """
        try:
            merged: Dict = {}
            mverts: List = []
            remap: List = []
            for vertex in vertices:
                key = (round(vertex[0], 5), round(vertex[1], 5), round(vertex[2], 5))
                target = merged.get(key)
                if target is None:
                    target = len(mverts)
                    merged[key] = target
                    mverts.append((float(vertex[0]), float(vertex[1]), float(vertex[2])))
                remap.append(target)
            tris: List[Tuple[int, int, int]] = []
            for face in faces:
                if len(face) < 3:
                    continue
                a, b, c = remap[face[0]], remap[face[1]], remap[face[2]]
                if a != b and b != c and a != c:
                    tris.append((a, b, c))
            if len(tris) < 3:
                return None
            normals: List[Tuple[float, float, float]] = []
            for a, b, c in tris:
                va, vb, vc = mverts[a], mverts[b], mverts[c]
                ex, ey, ez = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
                fx, fy, fz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
                nx, ny, nz = ey * fz - ez * fy, ez * fx - ex * fz, ex * fy - ey * fx
                length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                normals.append((nx / length, ny / length, nz / length))

            sharp_cos = math.cos(math.radians(self.SHARP_EDGE_DEG))
            edge_faces: Dict[Tuple[int, int], Any] = {}
            for index, (a, b, c) in enumerate(tris):
                for u, w in ((a, b), (b, c), (c, a)):
                    key = (u, w) if u < w else (w, u)
                    bucket = edge_faces.get(key)
                    if bucket is None and key not in edge_faces:
                        edge_faces[key] = index
                    elif isinstance(bucket, int):
                        edge_faces[key] = (bucket, index)
                    else:
                        edge_faces[key] = None  # non-manifold: leave un-drawn
            feature_edges: List[Tuple[tuple, tuple]] = []
            for (u, w), bucket in edge_faces.items():
                if bucket is None:
                    continue
                if isinstance(bucket, int):
                    keep = True  # real open boundary
                else:
                    n1, n2 = normals[bucket[0]], normals[bucket[1]]
                    keep = n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2] < sharp_cos
                if keep:
                    feature_edges.append((mverts[u], mverts[w]))

            smooth = self._crease_normals(tris, normals, mverts)
            triangles = [[mverts[a], mverts[b], mverts[c]] for a, b, c in tris]
            return {"triangles": triangles, "normals": smooth, "edges": feature_edges}
        except Exception:
            return None

    def _crease_normals(
        self,
        tris: List[Tuple[int, int, int]],
        face_normals: List[Tuple[float, float, float]],
        mverts: List,
    ) -> List[Tuple[float, float, float]]:
        """Average face normals per vertex, but only within direction clusters.

        Curved faces (fillets, gear flanks, cylinders) collapse to one cluster
        and shade smooth; faces meeting across a crease wider than CREASE_DEG
        land in separate clusters, so box corners stay crisp. Shared OCC
        triangulations do not weld across sharp edges, but position merging can,
        hence the clustering instead of a naive average.
        """
        crease_cos = math.cos(math.radians(self.CREASE_DEG))
        clusters: Dict[int, List[List[float]]] = {}
        for index, (a, b, c) in enumerate(tris):
            n = face_normals[index]
            for vertex in (a, b, c):
                bucket = clusters.get(vertex)
                if bucket is None:
                    clusters[vertex] = [[n[0], n[1], n[2]]]
                    continue
                best = None
                best_dot = -2.0
                for acc in bucket:
                    length = math.sqrt(acc[0] * acc[0] + acc[1] * acc[1] + acc[2] * acc[2]) or 1.0
                    dot = (acc[0] * n[0] + acc[1] * n[1] + acc[2] * n[2]) / length
                    if dot > best_dot:
                        best_dot = dot
                        best = acc
                if best is not None and best_dot >= crease_cos:
                    best[0] += n[0]
                    best[1] += n[1]
                    best[2] += n[2]
                else:
                    bucket.append([n[0], n[1], n[2]])
        smooth: List[Tuple[float, float, float]] = []
        for index, (a, b, c) in enumerate(tris):
            n = face_normals[index]
            sx = sy = sz = 0.0
            for vertex in (a, b, c):
                best = None
                best_dot = -2.0
                for acc in clusters[vertex]:
                    length = math.sqrt(acc[0] * acc[0] + acc[1] * acc[1] + acc[2] * acc[2]) or 1.0
                    dot = (acc[0] * n[0] + acc[1] * n[1] + acc[2] * n[2]) / length
                    if dot > best_dot:
                        best_dot = dot
                        best = (acc[0] / length, acc[1] / length, acc[2] / length)
                if best is not None:
                    sx += best[0]
                    sy += best[1]
                    sz += best[2]
            length = math.sqrt(sx * sx + sy * sy + sz * sz) or 1.0
            smooth.append((sx / length, sy / length, sz / length))
        return smooth
    
    def clear_cache(self):
        """清空缓存（kernel.undo/redo 时自动调用）"""
        self._cache.clear()
    
    def disable_cache(self):
        """禁用缓存（调试用）"""
        self._use_cache = False
        self.clear_cache()
    
    def enable_cache(self):
        """启用缓存"""
        self._use_cache = True
