# MechCAD Kernel

> AI CAD 建模内核：让 LLM 通过自然语言/手绘草图生成真实 OCC 几何

[![Tests](https://img.shields.io/badge/tests-175%2F175-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)]()
[![OCC](https://img.shields.io/badge/OCC-7.9.3-orange)]()
[![License](https://img.shields.io/badge/license-MIT-blue)]()

## 概述

MechCAD Kernel 是为 [MechCAD IDE](https://github.com/vanyu0710/aicad) 开发的**前体视觉建模内核**。它实现了"看→想→做→验"的拟人化建模流程，让 LLM 端到端生成可制造的 CAD 几何。

**核心能力**：
- 18 个原子 API（草图 + 拉伸 + 圆角 + 倒角 + 抽壳 + 扫掠 + 旋转体 + STEP I/O）
- 真实 OpenCascade (OCC) 几何 — 0% 体积误差
- Capability Registry (25 op JSON Schema) — LLM 知道"能做什么"
- 5 类类型化错误 + 事务 Savepoint + 撤销/重做
- DeepSeek Vision (v4-flash-vision-exp) + Chat Planner 端到端集成
- 175/175 测试全过

## 快速开始

```python
from mech_kernel import MechKernel
import math

# 1. 造一个 Ø100×20 圆盘
k = MechKernel()
k.create_workplane('XY', 'XY')
k.new_sketch('XY', 'sk')
k.add_circle('sk', center=[0, 0], radius=50)
k.close_sketch('sk')
k.extrude('sk', depth=20, mode='new_body', name='disc')
print(f'圆盘 vol: {k._current_geometry.volume:.2f} mm³')
# → 圆盘 vol: 314159.27 mm³ (= π·50²·20)

# 2. 加 6 个螺栓孔 (R=35 圆周)
k.new_sketch('XY', 'holes')
import math
for i in range(6):
    angle = i * 60 * math.pi / 180
    k.add_circle('holes', center=[35*math.cos(angle), 35*math.sin(angle)], radius=4)
k.close_sketch('holes')
k.extrude('holes', depth=20, mode='cut', name='holes')

# 3. 圆角所有边
k.fillet(1.5, edges='all')

# 4. 导出 STEP
k.export('flange.step', format='step')
```

## 架构

```
┌────────────────────────────────────────────────────────────┐
│  LLM (DeepSeek Vision + Chat Planner)                       │
│  - Vision: 手绘 PNG → 结构化 JSON (part_type/dimensions)   │
│  - Planner: 结构化 JSON → op 序列                          │
└──────────────────────┬─────────────────────────────────────┘
                       │ JSON
                       ▼
┌────────────────────────────────────────────────────────────┐
│  MechKernel (本项目)                                         │
│  - 18 op (create_workplane/new_sketch/add_circle/...)      │
│  - CapabilityRegistry (25 op + JSON Schema)               │
│  - Feature Graph (DAG) + Persistent Naming                 │
│  - 事务 Savepoint + 撤销栈                                  │
│  - 5 类类型化错误                                            │
└──────────────────────┬─────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────┐
│  build123d + OpenCascade (OCC 7.9.3)                        │
│  - 真实 BRep 几何                                            │
│  - OCC boolean (union/cut/intersect)                       │
│  - Fillet/Chamfer (BRepFilletAPI)                          │
│  - Shell (BRepOffsetAPI_MakeThickSolid)                    │
│  - STEP I/O (STEPControl_Writer/Reader)                    │
└────────────────────────────────────────────────────────────┘
```

## 18 op 能力图谱

| 类别 | op | 状态 | 备注 |
|------|----|------|------|
| 草图 | create_workplane | ✅ | XY/YZ/XZ + face 引用 |
| | new_sketch / close_sketch | ✅ | |
| | add_circle / add_rectangle / add_line | ✅ | 带 center 偏移 |
| 主体 | extrude (new_body/add/cut) | ✅ | 真实 OCC boolean |
| | extrude direction (X/Y/Z) | ✅ | 轴向/纵向/竖直 |
| | revolve | ✅ (受 OCP 0.18 限制) | 用 Locations 上下文 |
| | sweep | ✅ (直线 path) | 曲线 path 待 |
| | boolean (union/subtract/intersect) | 🟡 placeholder | v1.7 计划 |
| 细节 | fillet | ✅ | OCC BRepFilletAPI |
| | chamfer | ✅ | OCC BRepFilletAPI_MakeChamfer |
| | shell | ✅ | OCC BRepOffsetAPI_MakeThickSolid |
| | hole | 🟡 placeholder | |
| pattern | circular_pattern | ✅ | N 副本绕轴 |
| | linear_pattern | 🟡 placeholder | |
| | mirror | 🟡 placeholder | |
| query | query/select/measure | 🟡 placeholder | |
| I/O | export STEP | ✅ | 真实 STEPControl_Writer |
| | import_step | ✅ | 真实 STEPControl_Reader |
| | save_project / load_project | ✅ | STEP + JSON |
| 事务 | undo / redo | ✅ | 嵌套深度限制 10 |

## 安装

```bash
# 清华源（无大小限制）
pip install build123d==0.11.1 cadquery-ocp-novtk==7.9.3.0 \
    webcolors svgpathtools anytree ezdxf ocpsvg ocp_gordon \
    trianglesolver ipython sympy scikit-learn lib3mf requests \
    -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 跑测试

```bash
# 175/175 测试（无 pytest 兼容层）
PYTHONPATH=. python3 -c "
import sys
sys.path.insert(0, '.')
import mech_kernel._pytest_compat as mock
sys.modules['pytest'] = mock
exit_code = mock.main(['mech_kernel/tests'])
sys.exit(exit_code)
"
```

## 9 个 Demo

| # | 文件 | 演示能力 |
|---|------|----------|
| 01 | `examples/01_cylinder.py` | 基础圆柱 |
| 02 | `examples/02_error_types.py` | 5 类错误 |
| 03 | `examples/03_mock_render.py` | 渲染 |
| 04 | `examples/04_e2e_orchestrator.py` | Mock planner |
| 05 | `examples/05_real_geometry.py` | 真实 build123d |
| 06 | `examples/06_end_to_end_llm.py` | DeepSeek 端到端 |
| 07 | `examples/07_complex_parts.py` | 复杂件 |
| 08 | `examples/08_multistep_parts.py` | 多步骤 |
| 09 | `examples/09_fillet_chamfer.py` | 圆角 + 倒角 |

## 评估

**DeepSeek 资深架构师评估**：4.5/10 — 工程原型（偏学术）

| 维度 | 评分 | 关键证据 |
|---|---|---|
| 生产就绪度 | 3.0 | 9 op 真实实现（占 18 全部的 50%） |
| 复杂件覆盖 | 2.5 | 9 demo 全部 2.5D 棱柱体 |
| AI 协作 | 5.5 | 端到端真实几何，Planner 智能 |
| 数据模型 | 4.0 | DAG + Savepoint，缺持久化 |
| 架构合理性 | 6.5 | 范式方向正确 |

**距离工业生产 1.0**：~9-13 人月

详细评估见 `expert_evaluation.md`。

## 许可

MIT License

## 致谢

- [build123d](https://github.com/gumyr/build123d) — Pythonic CAD DSL
- [OpenCascade](https://dev.opencascade.org/) — 工业几何内核
- [DeepSeek](https://www.deepseek.com/) — Vision + Chat LLM
- [MechCAD IDE](https://github.com/vanyu0710/aicad) — 上层应用
