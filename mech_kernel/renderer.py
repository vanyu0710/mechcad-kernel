"""
MechKernel Renderer（v2.5 工程视觉版）

第 4 轮专家审查修复：
- P0-1 缓存键：用 (id, geometry_revision, level, config) 替代裸 id
- P0-4 异常隔离：坏几何/NaN/空/缺顶点不崩
- LRU 限制（默认 32 个）
- 任何异常都隔离，**绝不**让 kernel 崩溃
- 面向视觉模型的干净正交/ISO 工程证据图，不输出调试坐标轴
- 默认使用无网格线实体着色；需要检查拓扑时可打开 show_edges
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
            
            triangles = []
            triangle_colors = []
            groups = mesh_groups or [(vertices, faces, self.body_color, False)]
            for group_vertices, group_faces, group_color, highlighted in groups:
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
                color = tuple(min(1.0, float(channel) * (1.18 if highlighted else 1.0)) for channel in group_color)
                triangle_colors.extend(self._triangle_colors(group_triangles, color))
            
            if not triangles:
                plt.close(fig)
                return b""
            
            effective_edges = show_edges or quality == "presentation" or self.show_edges
            edge_color = (0.16, 0.22, 0.30, 0.34) if effective_edges else "none"
            mesh = Poly3DCollection(
                triangles,
                facecolors=triangle_colors,
                alpha=1.0,
                edgecolor=edge_color,
                linewidth=0.18 if effective_edges else 0.0,
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

    def _triangle_colors(self, triangles: List[List[Tuple[float, float, float]]], color: Optional[Tuple[float, float, float]] = None) -> List[Tuple[float, float, float, float]]:
        """Apply stable light shading without relying on matplotlib's version-specific shade API."""
        light = (-0.45, -0.55, 0.70)
        light_len = math.sqrt(sum(value * value for value in light)) or 1.0
        light = tuple(value / light_len for value in light)
        colors = []
        body_color = color or self.body_color
        for tri in triangles:
            try:
                ax, ay, az = (tri[1][i] - tri[0][i] for i in range(3))
                bx, by, bz = (tri[2][i] - tri[0][i] for i in range(3))
                nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
                normal_len = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                illumination = abs((nx * light[0] + ny * light[1] + nz * light[2]) / normal_len)
                factor = 0.58 + 0.42 * illumination
            except (IndexError, TypeError, ValueError):
                factor = 0.78
            colors.append(tuple(min(1.0, max(0.0, channel * factor)) for channel in body_color) + (1.0,))
        return colors
    
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
