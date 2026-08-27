"""
Demo 08: 多步骤实际零件（4 个工程件，每个 3+ 步操作）

每个零件至少 3 个 boolean 操作：
1. 轴向通孔轴: Ø40×80 + 横向 Ø10 通孔（沿 X 方向）
2. 沉孔板: 80×60×10 + 同心 Ø30 沉 5 + Ø15 通孔
3. T 形槽板: 80×60×10 + T 形凹槽（两个矩形叠切）
4. 带 6 孔圆盘: Ø100×10 + 6 个 Ø8 螺栓孔（R70 圆周上）
"""
from __future__ import annotations
import os, sys, json, base64, math
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "multistep_out"
OUT.mkdir(exist_ok=True)


def make_handdrawn_part(path: Path, kind: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle, RegularPolygon
    
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
    
    if kind == "shaft_with_cross_hole":
        # 圆柱主视图 + 横向孔
        c = Circle((0, 0), 20, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(c)
        # 横向孔（穿过圆心）
        ax.plot([-25, 25], [0, 0], "b-", linewidth=2.5)
        ax.plot([-25, 25], [0, 0], "b--", linewidth=0.8, dashes=(2, 2))
        # 横向剖面圆
        ax.add_patch(Circle((-25, 0), 5, fill=False, edgecolor="blue", linewidth=2))
        ax.add_patch(Circle((25, 0), 5, fill=False, edgecolor="blue", linewidth=2))
        ax.annotate("Ø40×80", xy=(0, 25), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("Ø10 cross", xy=(0, -30), ha="center", color="blue", fontsize=11)
        
    elif kind == "counterbored_plate":
        # 板 + 沉孔（俯视图 + 侧视图）
        r = Rectangle((-40, -30), 80, 60, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(r)
        # 沉孔大圆
        c1 = Circle((0, 0), 15, fill=False, edgecolor="blue", linewidth=2.5)
        # 通孔小圆
        c2 = Circle((0, 0), 7.5, fill=False, edgecolor="blue", linewidth=2.5, linestyle="--")
        ax.add_patch(c1)
        ax.add_patch(c2)
        ax.annotate("80x60", xy=(0, 33), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("60", xy=(-48, 0), color="red", fontsize=12, weight="bold", rotation=90, ha="center")
        ax.annotate("Ø30/Ø15", xy=(28, 0), color="blue", fontsize=11, weight="bold")
        ax.annotate("t=10", xy=(-100, -50), color="blue", fontsize=12, weight="bold")
        
    elif kind == "t_slot_plate":
        # 板 + T 形凹槽（俯视图 + 顶视）
        r = Rectangle((-40, -30), 80, 60, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(r)
        # T 形：垂直窄条 + 顶部宽横条
        t_v = Rectangle((-3, -25), 6, 50, fill=False, edgecolor="blue", linewidth=2)
        t_h = Rectangle((-15, 15), 30, 10, fill=False, edgecolor="blue", linewidth=2)
        ax.add_patch(t_v)
        ax.add_patch(t_h)
        ax.annotate("80x60", xy=(0, 33), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("T-slot 6x50", xy=(50, 30), color="blue", fontsize=10, weight="bold")
        ax.annotate("T-head 30x10", xy=(50, 20), color="blue", fontsize=10, weight="bold")
        
    elif kind == "flange_disc":
        # 圆盘 + 6 孔 pattern
        c = Circle((0, 0), 50, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(c)
        # 6 个螺栓孔在 R=35 圆周
        for i in range(6):
            angle = i * 60 * math.pi / 180
            x = 35 * math.cos(angle)
            y = 35 * math.sin(angle)
            ax.add_patch(Circle((x, y), 4, fill=False, edgecolor="blue", linewidth=2))
        ax.annotate("Ø100", xy=(0, 55), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("6xØ8", xy=(0, -60), ha="center", color="blue", fontsize=11, weight="bold")
        ax.annotate("R70", xy=(35, 0), color="blue", fontsize=10, weight="bold")
    
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
    views = renderer.render(geom, "full", geometry_revision=kernel._geometry_revision)
    iso_png = views.get("iso")
    top_png = views.get("top")
    front_png = views.get("front")
    from PIL import Image
    import io
    fig, axes = plt.subplots(1, 4, figsize=(20, 5))
    img = Image.open(handdrawn_path)
    axes[0].imshow(img)
    axes[0].set_title(f"Input: {kind}", fontsize=12, weight="bold")
    axes[0].axis("off")
    for i, (k, p) in enumerate([("iso", iso_png), ("top", top_png), ("front", front_png)]):
        if p:
            axes[i+1].imshow(Image.open(io.BytesIO(p)))
            axes[i+1].set_title(k, fontsize=12, weight="bold")
        axes[i+1].axis("off")
    vol = geom.volume if hasattr(geom, 'volume') else 0
    plt.suptitle(f"End-to-End: {kind} | vol = {vol:.1f} mm³", fontsize=14, weight="bold")
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] 对比图: {out_path.name} (vol={vol:.1f} mm³)")


def main():
    print("=" * 60)
    print("Demo 08: 多步骤实际零件（每件 3+ boolean 操作）")
    print("=" * 60)
    
    from mech_kernel.llm.deepseek import DeepSeekVisionLLM, DeepSeekPlannerLLM
    vision = DeepSeekVisionLLM()
    planner = DeepSeekPlannerLLM()
    
    configs = [
        {"kind": "shaft_with_cross_hole",
         "user": "做一个直径40长80的圆柱，垂直轴线方向打一个直径10的通孔（横穿）"},
        {"kind": "counterbored_plate",
         "user": "做一个 80x60x10 的方板，中间有一个沉头孔：表面大圆 Ø30 深 5，下面小圆 Ø15 通孔"},
        {"kind": "t_slot_plate",
         "user": "做一个 80x60x10 的方板，板面有一个 T 形凹槽：垂直窄条 6x50 + 顶部宽横条 30x10"},
        {"kind": "flange_disc",
         "user": "做一个 Ø100x10 的圆盘法兰，在 R=70 的圆周上均匀分布 6 个 Ø8 螺栓孔"},
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
            vr = vision.analyze(b64, user_prompt=cfg["user"])
        except Exception as e:
            print(f"  [ERR] Vision: {e}")
            continue
        print(f"  part_type: {vr.get('part_type', vr.get('type', '?'))}")
        print(f"  confidence: {vr.get('confidence', '?')}")
        
        print(f"\n[Planner] DeepSeek Chat ...")
        try:
            pr = planner.plan(vr, user_intent=cfg["user"])
        except Exception as e:
            print(f"  [ERR] Planner: {e}")
            continue
        ops = pr.get("ops", [])
        if not ops:
            print(f"  [ERR] Planner 没生成 op")
            continue
        print(f"  ops ({len(ops)}):")
        for o in ops:
            print(f"    - {o['op']}: {o.get('args', {})}")
        
        print(f"\n[Kernel] 执行 {len(ops)} ops...")
        kernel, exec_results = execute_ops(ops)
        ok_count = sum(1 for r in exec_results if r.get("ok"))
        err_count = sum(1 for r in exec_results if "error" in r)
        print(f"  [{ok_count} OK, {err_count} ERR]")
        for er in exec_results:
            status = "OK" if er.get("ok") else ("ERR" if "error" in er else "FAIL")
            print(f"    [{status}] {er['op']}: {er.get('args', {})}")
            if "error" in er:
                print(f"           {er['error']}")
        
        if kernel._current_geometry is None:
            print(f"  [WARN] 几何为空")
            continue
        render_comparison(handdrawn, kernel, compare, kind)
    
    print(f"\n{'='*60}")
    print(f"所有输出: {OUT}")


if __name__ == "__main__":
    main()
