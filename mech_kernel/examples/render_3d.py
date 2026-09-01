"""
3D 渲染器: build123d Part/Compound → matplotlib 3D PNG

不用 OCC, 不用 MechKernel.render (避 OOM)
用 build123d.tessellate() 拿 mesh, matplotlib 3D plot 画
"""
import os
from typing import List, Tuple, Union
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from build123d import Part, Compound, Shape, Vector


def _tessellate_shape(shape, tolerance: float = 1.0):
    """build123d Shape → (vertices, triangles)"""
    if not hasattr(shape, "tessellate"):
        return [], []
    try:
        verts, tris = shape.tessellate(tolerance)
    except Exception:
        return [], []
    # verts: tuple of Vector
    # tris: tuple of (i, j, k)
    V = np.array([(v.X, v.Y, v.Z) for v in verts], dtype=float)
    T = np.array([(t[0], t[1], t[2]) for t in tris], dtype=int)
    return V, T


def render_part_3d(parts_dict, out_path: str, title: str = "3D View",
                   colors: dict = None, size: Tuple[int, int] = (12, 9),
                   elevation: float = 25, azimuth: float = -60,
                   alpha: float = 0.85, edge_alpha: float = 0.15):
    """parts_dict: {name: Part/Compound}, 每个 part 用不同颜色.

    默认配色: blue/housing, green/gear_input, orange/gear_output, purple/shaft
    """
    fig = plt.figure(figsize=size, dpi=110)
    ax = fig.add_subplot(111, projection="3d")

    default_colors = {
        "housing": "#4477aa",
        "gear_input": "#228833",
        "gear_output": "#ee6677",
        "shaft_input": "#ccbb44",
        "shaft_output": "#aa3377",
        "default": "#888888",
    }
    if colors:
        default_colors.update(colors)

    all_verts = []
    for name, part in parts_dict.items():
        color = default_colors.get(name, default_colors["default"])
        # 如果是 Compound, 拆开
        if isinstance(part, Compound):
            shapes = list(part.solids()) if hasattr(part, "solids") else [part]
        elif isinstance(part, Part):
            shapes = [part] if hasattr(part, "solids") else []
        else:
            shapes = [part]
        for s in shapes:
            V, T = _tessellate_shape(s, tolerance=0.8)
            if len(V) == 0:
                continue
            all_verts.append(V)
            # 画三角形
            mesh = Poly3DCollection(V[T], alpha=alpha, facecolor=color, edgecolor="black",
                                    linewidth=0.1)
            ax.add_collection3d(mesh)

    if not all_verts:
        print(f"  ⚠ render_3d: 无几何可渲染")
        return False

    V_all = np.vstack(all_verts)
    # bbox
    mn = V_all.min(axis=0)
    mx = V_all.max(axis=0)
    center = (mn + mx) / 2
    r = (mx - mn).max() / 2 * 1.1
    ax.set_xlim(center[0] - r, center[0] + r)
    ax.set_ylim(center[1] - r, center[1] + r)
    ax.set_zlim(center[2] - r, center[2] + r)

    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.set_zlabel("Z (mm)")
    ax.set_title(title, fontsize=13, weight="bold")
    ax.view_init(elev=elevation, azim=azimuth)

    # 隐藏默认的 grid 看起来更干净
    ax.xaxis.pane.fill = False
    ax.yaxis.pane.fill = False
    ax.zaxis.pane.fill = False
    ax.xaxis.pane.set_edgecolor("gray")
    ax.yaxis.pane.set_edgecolor("gray")
    ax.zaxis.pane.set_edgecolor("gray")
    ax.grid(True, alpha=0.2)

    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close()
    return True


if __name__ == "__main__":
    # 测试: 画 1 个 box + cylinder
    from build123d import Box, Cylinder, Axis
    p1 = Box(50, 50, 50)
    p2 = Cylinder(20, 10).rotate(Axis.Z, 90)
    render_part_3d(
        {"box": p1, "cylinder": p2},
        "/tmp/test_3d.png",
        title="Test 3D Render",
    )
    print(f"  saved: /tmp/test_3d.png")
