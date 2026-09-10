"""MechKernel v2.14: 装配场景命令 —— F2a「零件库 + 位姿 manifest 的视图」。

设计：docs/ASSEMBLY_F2_DESIGN.md D1/D4。装配不是新的活编辑上下文：本模块
**纯函数式**——import 已归档零件 STEP → 应用位姿 → XCAF 装配导出 / 全对干涉 /
分色渲染。不读写任何 MechKernel 实例状态（不动 _current_geometry、不进事务/
快照/历史），由 server.py 的三个命令直接调用。

XCAF 装配树能力来自 build123d.export_step 的 _create_xde（实测 exporters3d.py
:75-233）：带 label 的形状树 → STEP PRODUCT 具名节点；SetAutoNaming(False)
保留用户 label。干涉求交复用 collision.check_pair_interference，外加 bbox
预过滤（N² boolean 的性能兜底，不动 collision 契约）。
"""
from __future__ import annotations

from typing import Any, Optional

MAX_ASSEMBLY_PARTS = 32


class AssemblyError(ValueError):
    """装配命令输入错误（server 层映射为 RPC BAD_REQUEST）。"""


def _apply_pose(part: Any, pose: dict) -> Any:
    """位姿 = 先绕世界轴旋转（rotation_deg:[angle,[ax,ay,az]]，与 assemble 同格式）
    再平移。与 kernel.assemble（kernel.py:3369-3371）语义一致。"""
    from build123d import Axis, Vector

    rotation = pose.get("rotation_deg")
    if rotation:
        angle, axis = float(rotation[0]), tuple(float(v) for v in rotation[1])
        part = part.rotate(Axis((0.0, 0.0, 0.0), axis), angle)
    position = pose.get("position")
    if position:
        part = part.translate(Vector(*[float(v) for v in position]))
    return part


def _bbox_of(part: Any) -> tuple:
    bb = part.bounding_box()
    return (bb.min.X, bb.min.Y, bb.min.Z, bb.max.X, bb.max.Y, bb.max.Z)


def _bbox_overlap_depth(box_a: tuple, box_b: tuple) -> float:
    """三轴重叠深度的最小值；<=0 表示两 bbox 不相交（可安全跳过求交）。"""
    overlaps = [
        min(box_a[3] - box_b[0], box_b[3] - box_a[0]),
        min(box_a[4] - box_b[1], box_b[4] - box_a[1]),
        min(box_a[5] - box_b[2], box_b[5] - box_a[2]),
    ]
    return min(overlaps)


def _load_parts(parts: Any) -> list:
    """校验并导入全部零件 STEP、应用位姿。返回 [(name, part, color)]。"""
    from build123d import import_step

    if not isinstance(parts, list) or not parts:
        raise AssemblyError("parts 必须是非空列表")
    if len(parts) > MAX_ASSEMBLY_PARTS:
        raise AssemblyError(f"零件数 {len(parts)} 超上限 {MAX_ASSEMBLY_PARTS}")
    loaded, seen = [], set()
    for index, item in enumerate(parts):
        if not isinstance(item, dict):
            raise AssemblyError(f"parts[{index}] 必须是对象")
        path = str(item.get("path") or "")
        name = str(item.get("name") or f"part_{index + 1:02d}")
        if not path:
            raise AssemblyError(f"parts[{index}] 缺少 path")
        if name in seen:
            raise AssemblyError(f"零件名重复: {name}")
        seen.add(name)
        pose = item.get("pose") or {}
        if not isinstance(pose, dict):
            raise AssemblyError(f"零件 {name} 的 pose 必须是对象")
        try:
            part = import_step(path)
        except AssemblyError:
            raise
        except Exception as exc:
            raise AssemblyError(
                f"零件 {name} 导入失败: {type(exc).__name__}: {exc}") from exc
        try:
            part = _apply_pose(part, pose)
        except (TypeError, ValueError, IndexError) as exc:
            raise AssemblyError(f"零件 {name} 位姿非法: {exc}") from exc
        color = item.get("color")
        if color is not None and (not isinstance(color, (list, tuple)) or len(color) != 3):
            raise AssemblyError(f"零件 {name} 的 color 必须是 [r,g,b]")
        loaded.append((name, part, color))
    return loaded


def _ext_string(text: str):
    """构造真正 UTF-16 的 TCollection_ExtendedString。

    pyOCP 的 str 构造器按 UTF-8 字节逐字节转 ExtCharacter（中文→mojibake），
    这里用占位 + SetValue 逐字符写入（单字符 str 会被正确映射为 ExtCharacter）。
    """
    from OCP.TCollection import TCollection_ExtendedString

    holder = TCollection_ExtendedString(" " * max(1, len(text)))
    for index, ch in enumerate(text):
        holder.SetValue(index + 1, ch)
    return holder


def _fix_step_names(step_path: str, names: list[str]) -> None:
    """export_step 之后回写具名节点：reader 读回 → 正确 UTF-16 名字 → writer 写出。

    build123d _create_xde 用 TCollection_ExtendedString(label) 写名字，中文经 pyOCP
    变 mojibake；结构（root + components）本身是对的，这里只替换名字属性。
    """
    from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app = XCAFApp_Application.GetApplication_s()
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if not reader.ReadFile(step_path) or not reader.Transfer(doc):
        raise AssemblyError("装配 STEP 名字回写失败：无法读回导出文件")
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def resolve(label):
        if shape_tool.IsReference_s(label):
            referred = TDF_Label()
            if shape_tool.GetReferredShape_s(label, referred):
                return referred
        return label

    roots = TDF_LabelSequence()
    shape_tool.GetFreeShapes(roots)
    if roots.Length() < 1:
        raise AssemblyError("装配 STEP 名字回写失败：无根产品")
    components = TDF_LabelSequence()
    shape_tool.GetComponents_s(resolve(roots.Value(1)), components)
    if components.Length() != len(names):
        raise AssemblyError(
            f"装配 STEP 名字回写失败：组件数 {components.Length()} != {len(names)}")
    for index in range(1, components.Length() + 1):
        label = components.Value(index)
        for target in (label, resolve(label)):
            TDataStd_Name.Set_s(target, _ext_string(names[index - 1]))
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.SetLayerMode(True)
    from OCP.STEPControl import STEPControl_AsIs

    if not writer.Transfer(doc, STEPControl_AsIs):
        raise AssemblyError("装配 STEP 名字回写失败：XCAF transfer 失败")
    if not writer.Write(step_path):
        raise AssemblyError("装配 STEP 名字回写失败：写出失败")


def export_assembly(parts: Any, out_step: str) -> dict:
    """逐件 import+位姿 → 带 label 的 Compound → export_step（XCAF 装配树）。

    返回 {ok, step, parts, solids, volume, bounding_box, step_bytes}。
    分件 STL 不需要在这里生成：前端按 manifest 位姿叠加已归档的零件 STL。
    """
    import os

    from build123d import Compound, export_step

    loaded = _load_parts(parts)
    labeled = []
    for name, part, _color in loaded:
        part.label = name  # → TDataStd_Name → STEP PRODUCT 名（_create_xde 保留用户 label）
        labeled.append(part)
    # children= 构建树结构（位置参数 prototypes 只做几何合并，XCAF 遍历不到）
    assembly = Compound(children=labeled)
    assembly.label = "assembly"
    try:
        export_step(assembly, str(out_step))
        _fix_step_names(str(out_step), [name for name, _p, _c in loaded])
    except AssemblyError:
        raise
    except Exception as exc:
        raise AssemblyError(f"装配 STEP 导出失败: {type(exc).__name__}: {exc}") from exc
    solids = assembly.solids()
    bb = assembly.bounding_box()
    return {
        "ok": True,
        "step": str(out_step),
        "parts": len(labeled),
        "solids": len(solids),
        "volume": float(sum(s.volume for s in solids)),
        "bounding_box": [bb.min.X, bb.min.Y, bb.min.Z, bb.max.X, bb.max.Y, bb.max.Z],
        "step_bytes": os.path.getsize(out_step) if os.path.exists(out_step) else 0,
    }


def assembly_interference(
    parts: Any,
    tolerance: float = 0.001,
    expected_overlaps: Optional[list] = None,
) -> dict:
    """装配位姿下全对干涉：bbox 预过滤 → OCC 求交 → 预期重叠豁免表降级。

    返回 {ok, total_pairs, checked_pairs, prefiltered_pairs, interfering_count,
    exempted_count, max_interference_volume, pairs, exempted}。
    重合体求交不可靠是 OCC 已知行为（test_v9_collision 有先例），error 字段透传。
    """
    from .collision import check_pair_interference

    loaded = _load_parts(parts)
    boxes = [_bbox_of(part) for _n, part, _c in loaded]

    exempt_map: dict[tuple, dict] = {}
    for item in expected_overlaps or []:
        if isinstance(item, dict) and item.get("a") and item.get("b"):
            key = tuple(sorted((str(item["a"]), str(item["b"]))))
            exempt_map[key] = item

    pairs, exempted = [], []
    interfering_count = prefiltered_count = 0
    max_volume = 0.0
    total = len(loaded) * (len(loaded) - 1) // 2
    for i in range(len(loaded)):
        name_a, part_a, _ca = loaded[i]
        for j in range(i + 1, len(loaded)):
            name_b, part_b, _cb = loaded[j]
            if _bbox_overlap_depth(boxes[i], boxes[j]) <= 0.0:
                prefiltered_count += 1
                continue
            result = check_pair_interference(
                part_a, part_b, name_a=name_a, name_b=name_b,
                tolerance=float(tolerance),
            )
            volume = float(result.get("volume_mm3") or 0.0)
            entry = {
                "name_a": name_a, "name_b": name_b,
                "interfering": bool(result.get("interfering")),
                "volume_mm3": volume,
                "center": result.get("center"),
                "error": result.get("error"),
            }
            key = tuple(sorted((name_a, name_b)))
            if entry["interfering"] and key in exempt_map:
                rule = exempt_map[key]
                cap = rule.get("max_volume_mm3")
                if cap is None or volume <= float(cap):
                    entry["exempt_reason"] = str(rule.get("reason") or "expected overlap")
                    exempted.append(entry)
                    interfering_count += 1  # 仍算物理重叠，只是被豁免
                    continue
            pairs.append(entry)
            if entry["interfering"]:
                interfering_count += 1
                max_volume = max(max_volume, volume)
    return {
        "ok": True,
        "total_pairs": total,
        "checked_pairs": total - prefiltered_count,
        "prefiltered_pairs": prefiltered_count,
        "interfering_count": interfering_count,
        "exempted_count": len(exempted),
        "max_interference_volume": max_volume,
        "pairs": pairs,
        "exempted": exempted,
    }


def render_assembly(
    parts: Any,
    views: Optional[list] = None,
    size: int = 480,
    quality: str = "presentation",
) -> bytes:
    """装配预览渲染：分件着色 + 四视角证据网格（复用 Renderer 的 scene 通道）。

    返回 PNG bytes；不落任何 kernel 状态。
    """
    from .assembly import AssemblyInstance
    from .renderer import Renderer

    loaded = _load_parts(parts)
    scene = {}
    for index, (name, part, color) in enumerate(loaded, start=1):
        instance = AssemblyInstance(
            id=f"A_{index:04d}", name=name, path="",
            color=list(color) if color else [0.36, 0.56, 0.76],
            visible=True, geometry=part,
        )
        instance.bbox = list(_bbox_of(part))
        scene[instance.id] = instance
    renderer = Renderer(image_size=(size, size), backend="auto")
    # scene 非空时逐实例取几何；base geometry 传首件仅为避开 None 探针路径
    renders = renderer.render(
        loaded[0][1], level="full", geometry_revision=0,
        views=views or ["iso", "front", "top", "side"],
        image_size=(size, size), quality=quality, backend="auto",
        show_edges=True, scene=scene,
    )
    from .renderer import Renderer as _R
    grid = _R.compose_grid({k: v for k, v in renders.items() if k != "default" and v},
                            cols=2, max_size=size * 2)
    if grid:
        return grid
    return renders.get("iso") or renders.get("default") or b""
