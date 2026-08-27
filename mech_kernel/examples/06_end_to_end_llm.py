"""
Demo 06: 端到端 Vision LLM + Planner LLM + MechKernel + 真实 build123d 几何

流程：
1. 准备手绘 PNG（matplotlib 模拟）
2. Vision LLM（DeepSeek Vision）→ 零件 JSON
3. Planner LLM（DeepSeek Chat）→ op 序列
4. MechKernel 执行 op → build123d 真实几何
5. 渲染对比图（手绘草图 vs 真实几何）

运行：PYTHONPATH=/workspace python3 mech_kernel/examples/06_end_to_end_llm.py
环境：DSKEY（DeepSeek API key）
"""
from __future__ import annotations
import os
import sys
import json
import base64
from pathlib import Path

# 准备路径
HERE = Path(__file__).parent
OUT = HERE / "e2e_out"
OUT.mkdir(exist_ok=True)


def make_handdrawn_part(path: Path, kind: str = "disk"):
    """matplotlib 画一个手绘风零件 PNG（白底 + 黑色线条 + 标注）"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import Circle, Rectangle, FancyArrowPatch
    
    fig, ax = plt.subplots(figsize=(6, 6), dpi=100)
    ax.set_xlim(-150, 150)
    ax.set_ylim(-150, 150)
    ax.set_aspect("equal")
    ax.set_facecolor("white")
    ax.set_title(f"Hand-drawn: {kind}", fontsize=14, color="#333")
    # 用中文字体
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'Noto Sans CJK SC', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    
    # 坐标网格（淡）
    ax.grid(True, alpha=0.2, color="gray", linewidth=0.5)
    ax.axhline(0, color="gray", linewidth=0.5, alpha=0.3)
    ax.axvline(0, color="gray", linewidth=0.5, alpha=0.3)
    
    if kind == "disk":
        # 圆盘俯视图
        c1 = Circle((0, 0), 50, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(c1)
        # 直径标注
        ax.annotate("", xy=(50, 70), xytext=(-50, 70),
                    arrowprops=dict(arrowstyle="<->", color="red", lw=1.2))
        ax.text(0, 78, "Ø100", ha="center", color="red", fontsize=12, weight="bold")
        # 厚度
        ax.annotate("t=20", xy=(-90, -40), color="blue", fontsize=12, weight="bold")
        # 中心标记
        ax.plot(0, 0, "k+", markersize=8, markeredgewidth=1.5)
        
    elif kind == "ring":
        # 环面
        c1 = Circle((0, 0), 80, fill=False, edgecolor="black", linewidth=2.5)
        c2 = Circle((0, 0), 40, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(c1)
        ax.add_patch(c2)
        ax.annotate("", xy=(80, 100), xytext=(-80, 100),
                    arrowprops=dict(arrowstyle="<->", color="red", lw=1.2))
        ax.text(0, 108, "Ø160", ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("", xy=(40, 60), xytext=(-40, 60),
                    arrowprops=dict(arrowstyle="<->", color="red", lw=1.0))
        ax.text(0, 67, "Ø80", ha="center", color="red", fontsize=10)
        ax.annotate("t=10", xy=(-100, -50), color="blue", fontsize=12, weight="bold")
        
    elif kind == "block":
        # 立方体
        r = Rectangle((-40, -30), 80, 60, fill=False, edgecolor="black", linewidth=2.5)
        ax.add_patch(r)
        ax.annotate("80", xy=(0, 33), ha="center", color="red", fontsize=12, weight="bold")
        ax.annotate("60", xy=(-48, 0), color="red", fontsize=12, weight="bold", rotation=90, ha="center")
        ax.annotate("t=10", xy=(-100, -50), color="blue", fontsize=12, weight="bold")
    
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#888")
    plt.tight_layout()
    plt.savefig(path, dpi=120, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] 画手绘 PNG: {path}")


def encode_image(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def execute_ops(ops: list):
    """在 MechKernel 上跑 op 列表"""
    from mech_kernel import MechKernel
    
    kernel = MechKernel()
    results = []
    for i, op_spec in enumerate(ops):
        op_name = op_spec.get("op")
        args = op_spec.get("args", {})
        # 过滤掉 None 参数
        args = {k: v for k, v in args.items() if v is not None}
        method = getattr(kernel, op_name, None)
        if not method:
            results.append({"op": op_name, "error": f"op {op_name} 不存在"})
            continue
        try:
            r = method(**args)
            results.append({"op": op_name, "args": args, "result": r.to_dict() if hasattr(r, "to_dict") else str(r)})
            if not r.success if hasattr(r, "success") else False:
                print(f"  [FAIL] {op_name}: {r}")
        except Exception as e:
            results.append({"op": op_name, "args": args, "error": str(e)})
            print(f"  [ERR] {op_name}: {e}")
    return kernel, results


def render_comparison(handdrawn_path: Path, kernel, out_path: Path, kind: str):
    """渲染对比图：左 = 手绘草图，右 = 真实几何 iso 视图"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mech_kernel.renderer import Renderer
    
    # 真实几何 iso 视图
    renderer = Renderer()
    geom = kernel._current_geometry
    if geom is None:
        print("[WARN] 几何为空，跳过渲染")
        return
    views = renderer.render(geom, "iso_only", geometry_revision=kernel._geometry_revision)
    iso_png = views.get("iso")
    
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    # 用中文字体（matplotlib 把所有 Noto CJK 都识别为 CJK JP）
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'Noto Sans CJK SC', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    # 左：手绘图
    from PIL import Image
    img = Image.open(handdrawn_path)
    axes[0].imshow(img)
    axes[0].set_title(f"Input: {kind} (hand-drawn)", fontsize=14, weight="bold")
    axes[0].axis("off")
    
    # 右：真实几何
    if iso_png:
        import io
        iso_img = Image.open(io.BytesIO(iso_png))
        axes[1].imshow(iso_img)
        axes[1].set_title("Output: build123d 真实几何", fontsize=14, weight="bold")
    else:
        axes[1].text(0.5, 0.5, "(no geometry)", ha="center", va="center", fontsize=14)
    axes[1].axis("off")
    
    plt.suptitle(f"End-to-End: Vision LLM → Planner LLM → MechKernel → build123d", fontsize=15, weight="bold")
    plt.tight_layout()
    plt.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] 对比图: {out_path}")


def main():
    print("=" * 60)
    print("Demo 06: 端到端 LLM 建模（Vision + Planner + build123d）")
    print("=" * 60)
    
    # 选零件类型
    kinds = ["disk", "ring", "block"]
    for kind in kinds:
        print(f"\n{'='*60}")
        print(f"[Part: {kind}]")
        print(f"{'='*60}")
        
        handdrawn = OUT / f"handdrawn_{kind}.png"
        compare = OUT / f"compare_{kind}.png"
        
        # 1. 画手绘
        make_handdrawn_part(handdrawn, kind=kind)
        b64 = encode_image(handdrawn)
        
        # 2. Vision LLM
        from mech_kernel.llm.deepseek import DeepSeekVisionLLM, DeepSeekPlannerLLM
        vision = DeepSeekVisionLLM()
        planner = DeepSeekPlannerLLM()
        
        print(f"\n[Vision] 调用 DeepSeek Vision...")
        user_prompts = {
            "disk": "做一个直径100mm、厚20mm的圆盘",
            "ring": "做一个外径160mm、内径80mm、厚10mm的环面垫圈",
            "block": "做一个长80mm、宽60mm、厚10mm的方板",
        }
        vision_result = vision.analyze(b64, user_prompt=user_prompts[kind])
        print(f"[Vision] 识别结果:")
        print(json.dumps(vision_result, ensure_ascii=False, indent=2))
        
        # 3. Planner LLM
        print(f"\n[Planner] 调用 DeepSeek Chat...")
        plan_result = planner.plan(vision_result, user_intent=user_prompts[kind])
        print(f"[Planner] op 序列:")
        print(json.dumps(plan_result, ensure_ascii=False, indent=2))
        
        # 4. Kernel 执行
        ops = plan_result.get("ops", [])
        print(f"\n[Kernel] 执行 {len(ops)} ops...")
        kernel, exec_results = execute_ops(ops)
        for er in exec_results:
            status = "OK" if "error" not in er else "ERR"
            print(f"  [{status}] {er['op']}: {er.get('args', {})}")
            if "error" in er:
                print(f"         err: {er['error']}")
        
        # 5. 渲染对比
        render_comparison(handdrawn, kernel, compare, kind)
    
    print(f"\n{'='*60}")
    print(f"所有输出: {OUT}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
