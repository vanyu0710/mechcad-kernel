"""MechKernel v2.19: 材质系统（工程渲染用的简化 PBR 着色）。

离线 matplotlib 后端没有真实 PBR/IBL，但工程图需要区分"铸铁壳体 / 钢齿轮 /
青铜轴承 / 阳极氧化端盖"等材质。这里用一组可观测的光学参数近似：

    base       基础色（反射率）
    ambient    环境光系数（阴影里的亮度下限）
    diffuse    漫反射系数（Lambert 强度）
    specular   高光系数（金属/抛光面更强）
    shininess  高光指数（越大高光越集中）
    edge       特征线颜色（不同材质用不同描边，帮助区分零件）

着色按每个三角形法线、视线方向与光源方向计算（Blinn-Phong 简化）：
    color = base * (ambient + diffuse * max(N·L, 0) * limb)
            + specular * max(N·H, 0)^shininess
其中 limb 为掠射压暗（v2.15 的 limb darkening），避免侧视时整面发亮。
"""
from __future__ import annotations

from typing import Dict, Tuple

# 名称 → (base, ambient, diffuse, specular, shininess, edge_rgba)
MATERIALS: Dict[str, Tuple] = {
    "steel":        ((0.55, 0.62, 0.72), 0.24, 0.80, 0.46, 30.0, (0.08, 0.12, 0.20, 0.62)),
    "cast_iron":    ((0.47, 0.49, 0.51), 0.26, 0.74, 0.18, 14.0, (0.10, 0.12, 0.15, 0.60)),
    "aluminum":     ((0.70, 0.73, 0.76), 0.26, 0.80, 0.34, 20.0, (0.16, 0.20, 0.25, 0.70)),
    "bronze":       ((0.76, 0.55, 0.25), 0.28, 0.76, 0.34, 18.0, (0.22, 0.14, 0.05, 0.60)),
    "copper":       ((0.72, 0.45, 0.30), 0.24, 0.78, 0.32, 20.0, (0.26, 0.15, 0.09, 0.75)),
    "brass":        ((0.74, 0.62, 0.32), 0.24, 0.78, 0.34, 20.0, (0.26, 0.21, 0.09, 0.75)),
    "titanium":     ((0.55, 0.55, 0.58), 0.22, 0.74, 0.28, 16.0, (0.14, 0.15, 0.18, 0.75)),
    "dark_steel":   ((0.36, 0.38, 0.42), 0.22, 0.72, 0.38, 26.0, (0.06, 0.08, 0.11, 0.65)),
    "nylon":        ((0.86, 0.86, 0.82), 0.34, 0.66, 0.10, 8.0, (0.30, 0.30, 0.28, 0.60)),
    "rubber":       ((0.16, 0.16, 0.17), 0.28, 0.50, 0.04, 6.0, (0.04, 0.04, 0.05, 0.70)),
}

# 按零件名关键词推断材质（装配渲染时自动分材质）
_NAME_HINTS = (
    (("箱体", "箱盖", "壳", "盖", "housing", "case", "cover"), "cast_iron"),
    # 换挡机构先于"轴"匹配，否则拨叉轴会被当成普通轴
    (("拨叉", "fork"), "bronze"),
    (("同步器", "接合套", "synchron"), "dark_steel"),
    (("轴承", "bearing", "铜", "bronze", "衬套", "bush"), "bronze"),
    (("齿轮", "gear", "惰轮", "idler", "轴", "shaft", "花键", "spline"), "steel"),
)


def resolve_material(name: str) -> str:
    """按零件名推断材质键；未命中返回 steel。"""
    low = str(name).lower()
    for keys, mat in _NAME_HINTS:
        if any(k in low for k in keys):
            return mat
    return "steel"


def material_color(name: str) -> Tuple[float, float, float]:
    base = MATERIALS.get(name, MATERIALS["steel"])[0]
    return tuple(base)


def material_edge(name: str):
    return MATERIALS.get(name, MATERIALS["steel"])[5]


# v2.21: 供上层（aicad）镜像与一致性测试比对的稳定键集。
# 表结构保持不变；上层不得 import 本模块（内核依赖 OCC/build123d），
# 只按路径读取此列表与其基色，防止两处材质表漂移。
UI_MATERIAL_KEYS = tuple(sorted(MATERIALS.keys()))


def ui_material_table() -> Dict[str, Tuple[float, float, float]]:
    """材质键 → 基色（0..1），给上层 UI 复刻用。"""
    return {key: tuple(MATERIALS[key][0]) for key in UI_MATERIAL_KEYS}
