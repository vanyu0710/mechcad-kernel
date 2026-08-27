"""
Demo 07: 复杂实际零件端到端

4 个工程件：
1. 带孔方板（80×60×10 + Ø20 通孔）
2. 阶梯轴（Ø40×30 + Ø25×50，两段同轴）
3. 键槽轴（Ø40×80 + 12×5×40 键槽）
4. L 形支架（两个矩形 extrude union）

每件：
- 画手绘 PNG
- Vision LLM 识别
- Planner LLM 拆 op
- Kernel 真实几何
- 渲染对比图
"""
from __future__ import annotations
import os
import sys
import json
import base64
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "complex_out"
OUT.mkdir(exist_ok=True)


def make_handdrawn_part(path: Path, kind: str):
    """画手绘 PNG（matplot 模拟）"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle, FancyArrowPatch, Polygon
    
    fig, ax = plt.subplots(figsize=(8, 6), dpi=100)
    ax.set_xlim(-200, 200)
    ax.set_ylim(-150, 150)
    ax.set_aspect("equal")
    ax.set_facecolor("white")
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    ax.set_title(f"Hand-drawn: {kind}", fontsize=14, color="#333", weight="bold")
    ax.grid(True, alpha=0.2, color="gray", linewidth=0.5)
    ax.axhline(0, color="gray", linewidth=0.5, alpha=0.3)
    ax.axvline(0, color="gray", linewidth=0.5, alpha=0.3)
    
    if kind == "plate_with_hole":
        r = Rectangle((-40, -30), 80, 60, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(r)
        c = Circle((0, 0), 10, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(c)
        ax.annotate("80", xy=(0, 33), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("60", xy=(-48, 0), color="red", fontsize=12, weight="bold", rotation=90, ha="center")
        ax.annotate("Ø20", xy=(0, 14), ha="center", color="blue", fontsize=11, weight="bold")
        ax.annotate("t=10", xy=(-100, -50), color="blue", fontsize=12, weight="bold")
        
    elif kind == "stepped_shaft":
        # 阶梯轴：左 Ø40×30 + 右 Ø25×50
        r1 = Rectangle((-50, -20), 30, 40, fill=False, edgecolor="black", linewidth=2.5)
        r2 = Rectangle((-20, -12.5), 50, 25, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(r1)
        ax.add_patch(r2)
        ax.annotate("Ø40×30", xy=(-35, 25), ha="center", color="red", fontsize=11, weight="bold")
        ax.annotate("Ø25×50", xy=(5, 17), ha="center", color="red", fontsize=11, weight="bold")
        # 中心线
        ax.plot([-55, 35], [0, 0], "k--", alpha=0.3, linewidth=0.8)
        
    elif kind == "keyway_shaft":
        # 键槽轴：Ø40×80 + 矩形键槽
        r = Rectangle((-40, -20), 80, 40, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(r)
        # 键槽（顶部 12×40）
        kw = Rectangle((-20, 20), 40, 5, fill=False, edgecolor="black", linewidth=2.5, linestyle="--")
        ax.add_patch(kw)
        ax.annotate("Ø40×80", xy=(0, -30), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("key 12x5", xy=(50, 25), color="red", fontsize=10, weight="bold")
        ax.annotate("length 40", xy=(20, 30), color="blue", fontsize=10, weight="bold")
        
    elif kind == "L_bracket":
        # L 形支架（侧视图）
        # 底板 80×10 + 立板 60×10 高 50
        r1 = Rectangle((-40, -5), 80, 10, fill=False, edgecolor="black", linewidth=2.5)
        r2 = Rectangle((-40, 5), 10, 50, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(r1)
        ax.add_patch(r2)
        ax.annotate("80", xy=(0, 9), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("60", xy=(-48, 30), color="red", fontsize=12, weight="bold")
        ax.annotate("t=10", xy=(-100, -20), color="blue", fontsize=12, weight="bold")
    
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#888")
    plt.tight_layout()
    plt.savefig(path, dpi=120, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] 画手绘 PNG: {path.name}")


def encode_image(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def execute_ops(ops: list):
    from mech_kernel import MechKernel
    kernel = MechKernel()
    results = []
    for op_spec in ops:
        op_name = op_spec.get("op")
        args = op_spec.get("args", {})
        args = {k: v for k, v in args.items() if v is not None}
        method = getattr(kernel, op_name, None)
        if not method:
            results.append({"op": op_name, "error": f"op {op_name} 不存在"})
            continue
        try:
            r = method(**args)
            ok = r.success if hasattr(r, "success") else True
            results.append({"op": op_name, "args": args, "ok": ok})
            if not ok:
                print(f"  [FAIL] {op_name}: {r}")
        except Exception as e:
            results.append({"op": op_name, "args": args, "error": str(e)})
            print(f"  [ERR] {op_name}: {e}")
    return kernel, results


def render_comparison(handdrawn_path: Path, kernel, out_path: Path, kind: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mech_kernel.renderer import Renderer
    
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    
    renderer = Renderer()
    geom = kernel._current_geometry
    if geom is None:
        print("[WARN] 几何为空")
        return
    
    # 多视角
    views = renderer.render(geom, "full", geometry_revision=kernel._geometry_revision)
    iso_png = views.get("iso")
    top_png = views.get("top")
    front_png = views.get("front")
    
    from PIL import Image
    import io
    fig, axes = plt.subplots(1, 4, figsize=(20, 5))
    
    # 1. 手绘
    img = Image.open(handdrawn_path)
    axes[0].imshow(img)
    axes[0].set_title(f"Input: {kind}", fontsize=12, weight="bold")
    axes[0].axis("off")
    
    # 2. iso
    if iso_png:
        axes[1].imshow(Image.open(io.BytesIO(iso_png)))
        axes[1].set_title("iso", fontsize=12, weight="bold")
    axes[1].axis("off")
    
    # 3. top
    if top_png:
        axes[2].imshow(Image.open(io.BytesIO(top_png)))
        axes[2].set_title("top", fontsize=12, weight="bold")
    axes[2].axis("off")
    
    # 4. front
    if front_png:
        axes[3].imshow(Image.open(io.BytesIO(front_png)))
        axes[3].set_title("front", fontsize=12, weight="bold")
    axes[3].axis("off")
    
    vol = geom.volume if hasattr(geom, 'volume') else 0
    plt.suptitle(f"End-to-End: {kind} | vol = {vol:.1f} mm³", fontsize=14, weight="bold")
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] 对比图: {out_path.name} (vol={vol:.1f} mm³)")


def main():
    print("=" * 60)
    print("Demo 07: 复杂实际零件端到端")
    print("=" * 60)
    
    from mech_kernel.llm.deepseek import DeepSeekVisionLLM, DeepSeekPlannerLLM
    vision = DeepSeekVisionLLM()
    planner = DeepSeekPlannerLLM()
    
    # 4 个零件 + 对应 LLM 提示
    configs = [
        {
            "kind": "plate_with_hole",
            "user": "做一个 80x60x10 的方板，中间有一个直径20的通孔",
        },
        {
            "kind": "stepped_shaft",
            "user": "做一个阶梯轴：左段直径40长30，右段直径25长50，两段同轴",
        },
        {
            "kind": "keyway_shaft",
            "user": "做一个直径40长80的轴，在顶部切一个 12x5 宽40 的键槽",
        },
        {
            "kind": "L_bracket",
            "user": "做一个 L 形支架：底板 80x10 厚 10，立板在底板左端 60x10 厚 10",
        },
    ]
    
    for cfg in configs:
        kind = cfg["kind"]
        print(f"\n{'='*60}")
        print(f"[Part: {kind}]")
        print(f"{'='*60}")
        
        handdrawn = OUT / f"handdrawn_{kind}.png"
        compare = OUT / f"compare_{kind}.png"
        
        make_handdrawn_part(handdrawn, kind=kind)
        b64 = encode_image(handdrawn)
        
        print(f"\n[Vision] DeepSeek Vision ...")
        try:
            vision_result = vision.analyze(b64, user_prompt=cfg["user"])
        except Exception as e:
            print(f"  [ERR] Vision 失败: {e}")
            continue
        print(f"  part_type: {vision_result.get('part_type', vision_result.get('type', '?'))}")
        print(f"  dimensions: {vision_result.get('dimensions', '?')}")
        print(f"  confidence: {vision_result.get('confidence', '?')}")
        
        print(f"\n[Planner] DeepSeek Chat ...")
        try:
            plan_result = planner.plan(vision_result, user_intent=cfg["user"])
        except Exception as e:
            print(f"  [ERR] Planner 失败: {e}")
            continue
        ops = plan_result.get("ops", [])
        if not ops:
            print(f"  [ERR] Planner 没生成 op: {plan_result}")
            continue
        print(f"  ops ({len(ops)}):")
        for op_spec in ops:
            print(f"    - {op_spec['op']}: {op_spec.get('args', {})}")
        if plan_result.get("estimated_volume_mm3"):
            print(f"  estimated_volume: {plan_result['estimated_volume_mm3']:.1f} mm³")
        
        print(f"\n[Kernel] 执行 {len(ops)} ops...")
        kernel, exec_results = execute_ops(ops)
        for er in exec_results:
            status = "OK" if er.get("ok") else ("ERR" if "error" in er else "FAIL")
            print(f"  [{status}] {er['op']}: {er.get('args', {})}")
            if "error" in er:
                print(f"         {er['error']}")
        
        if kernel._current_geometry is None:
            print(f"  [WARN] 几何为空，跳过渲染")
            continue
        render_comparison(handdrawn, kernel, compare, kind)
    
    print(f"\n{'='*60}")
    print(f"所有输出: {OUT}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
