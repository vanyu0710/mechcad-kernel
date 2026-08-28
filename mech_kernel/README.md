# MechKernel v1.1

> **为 MechCAD（vanyu0710/aicad）打造的几何内核中间层**  
> 让 AI 像人类工程师一样建模：看 → 想 → 做 → 验

---

## 项目简介

**MechKernel** 是一个为 AI CAD 设计的"语义几何内核"。

它把脆弱的 `build123d` / `trimesh` 调用，包装成对 LLM 友好的**原子 API**：
- ✅ **30 个公共操作**（草图、实体、细节、复用、查询、编辑、装配与可视化）
- ✅ **5 类类型化错误**（INVALID_REQUEST / KERNEL_BUG / STATE_CORRUPTION / GEOMETRY_FAILURE / RECOVERABLE / NOT_IMPLEMENTED）
- ✅ **事务 Savepoint**（失败整体回滚，可独立撤销）
- ✅ **Feature Graph DAG**（持久化命名 + 增量循环检测）
- ✅ **Capability Registry**（自动注册 + JSON Schema + LLM 友好）
- ✅ **自适应多视图渲染**（拓扑变化 full 视图，间隔步骤 iso 快照）
- ✅ **AI 截面与转台视图**（真实几何半空间切割，不改变模型）
- ✅ **三态拓扑检查**（valid / invalid / unknown，不伪造 False）

**核心创新**：每个 API 调用的结果（`StepResult`）包含 `success / error_kind / error / hint / suggestion / geometry_summary / render_png / next_hints`，让 LLM 拿到**可决策的反馈**。

---

## 架构

```
┌─────────────────────────────────────────────────────┐
│ L1: Planner  ← LLM 决策（"下一步做什么"）           │
└───────────────────┬─────────────────────────────────┘
                    │ PlannerAction
                    ▼
┌─────────────────────────────────────────────────────┐
│ L2: Orchestrator  ← run_loop 主循环                  │
│     Plan → Execute → Inspect → Decide               │
│     监督：超时 / 重试 / 参数去重 / UNSUPPORTED 拒收 │
└───────────────────┬─────────────────────────────────┘
                    │ kernel.execute(op, **args)
                    ▼
┌─────────────────────────────────────────────────────┐
│ L3: MechKernel  ← 30 个公共 API + capability registry│
│     5 类错误 / 事务 / 撤销栈 / 几何缓存              │
└───────────────────┬─────────────────────────────────┘
                    │ build123d 调用（v1.1 用 MockBox 占位）
                    ▼
┌─────────────────────────────────────────────────────┐
│ L4: Build123d  ← 真实几何计算（OCC 后端）            │
│     Shape / Compound / B-rep                       │
└───────────────────┬─────────────────────────────────┘
                    │ vertices/faces
                    ▼
┌─────────────────────────────────────────────────────┐
│ L5: Renderer  ← matplotlib 离屏 4 视角              │
│     iso / front / top / side PNG                    │
└─────────────────────────────────────────────────────┘
                    │
                    ▼
            回到 L1（AI 看到渲染图）
```

---

## 公共 API

| 分类 | API | 状态 |
|------|------|------|
| **草图** | `create_workplane` / `new_sketch` / `add_circle` / `add_rectangle` / `add_line` / `close_sketch` | ✅ v1.1 全部实现 |
| **主体** | `extrude` / `revolve` / `sweep` / `boolean` | ✅ extrude / 3 占位 |
| **细节** | `hole` / `fillet` / `chamfer` / `shell` | ⏳ v1.2 占位 |
| **复用** | `linear_pattern` / `circular_pattern` / `mirror` | ⏳ v1.2 占位 |
| **查询** | `query` / `select` / `measure` | ⏳ v1.2 占位 |
| **编辑** | `undo` / `redo` / `delete_feature` / `update_feature` / `rebuild` / `export` | ✅ 参数化重放 |
| **可视化** | `render` | ✅ 多视图、转台、标注、真实截面 |

`delete_feature` / `update_feature` / `rebuild` 会在会话内通过 op 历史全量重放；导入/加载/装配会话保留外部几何，因此重放返回 `RECOVERABLE`。

---

## 5 类类型化错误

```python
class InvalidRequestError(Exception):  # 编程错误，必抛
    """参数非法、状态不一致"""

class KernelBugError(Exception):  # 内部 bug，必抛
    """Kernel 自己的问题"""

class StateCorruptionError(Exception):  # 状态损坏，必抛
    """Feature Graph 环、引用解析失败"""

# 这两类不抛异常，通过 StepResult 表达：

class StepResult:
    error_kind: "GEOMETRY_FAILURE"  # 几何问题（自相交、孔位超出面）
    error_kind: "RECOVERABLE"        # 参数超界，带 suggestion
    error_kind: "NOT_IMPLEMENTED"    # 占位 API
    error_kind: "UNSUPPORTED"        # Orchestrator 收到未知指令
```

**AI 决策表**：

| error_kind | AI 动作 |
|------------|---------|
| success=True | 继续 |
| INVALID_REQUEST | 我的代码错了，重写 |
| GEOMETRY_FAILURE | 几何不行，调整参数 |
| RECOVERABLE | 用 `suggestion` 再试 |
| NOT_IMPLEMENTED | 跳过这步，换别的 |
| UNSUPPORTED | 停止，问用户 |

---

## Capability Registry（v1.1.1 新增）

```python
from mech_kernel import MechKernel
from mech_kernel.capability_registry import string, number, tuple2

k = MechKernel()

# 1. 查询 LLM 用的 op 列表
public_ops = k.cap.list_public()
# 返回 [{name, category, description, inputs, permission, examples}, ...]

# 2. 校验参数
ok, err = k.cap.validate_call('add_circle', {
    'sketch_name': 'sk_1',
    'center': (0, 0),
    'radius': 5,
})
assert ok

# 3. execute 通用入口
r = k.execute('add_circle', sketch_name='sk_1', center=(0, 0), radius=5)
# 校验失败 → INVALID_REQUEST
# 未知 op → NOT_IMPLEMENTED
# 下划线方法 → INVALID_REQUEST（hint: "op 名不能以下划线开头"）
```

**特性**：
- 自动注册（30 个公共 op 都有 schema）
- JSON Schema 风格（type / required / min / max / enum / length）
- LLM 友好的 op 描述（含 `examples` Few-shot）
- 权限分级（`public` / `read` / `internal`）

---

## 安装

```bash
# 基础（无 build123d）
git clone <repo>
cd mech_kernel
python3 -m pip install -r requirements.txt

# 完整（含 build123d + matplotlib + Pillow）
python3 -m pip install build123d trimesh matplotlib Pillow
```

**requirements.txt**：
```
pydantic>=2.0
matplotlib>=3.5
Pillow>=9.0
```

---

## 运行测试

```bash
# 全量测试（v2.2）
PYTHONPATH=. python3 -c "
import sys
sys.path.insert(0, '.')
import mech_kernel._pytest_compat as mock
sys.modules['pytest'] = mock
mock.main(['mech_kernel/tests'])
"
```

**测试覆盖**：

| 模块 | 测试数 | 状态 |
|------|--------|------|
| M0 核心（API + Feature Graph + Workplane + Transaction） | 89 | ✅ |
| M1 渲染（GeometryInspector + Renderer + AdaptiveRenderer） | 25 | ✅ |
| M1 P0 修复（缓存键 + 拓扑三态 + revision + 异常隔离） | 30 | ✅ |
| M2 Orchestrator（PlannerAction + MockPlanner + run_loop） | 11 | ✅ |
| M2 P0/P1 修复（capability registry + 超时 + UNSUPPORTED） | 20 | ✅ |
| **总计** | **175/175** | ✅ 全过 |

---

## 4 个 Demo

```bash
# 1. M0 数据流：草图 → 拉伸
PYTHONPATH=. python3 mech_kernel/examples/01_cylinder.py

# 2. 5 类错误演示
PYTHONPATH=. python3 mech_kernel/examples/02_error_types.py

# 3. M1 渲染：4 视角 PNG
PYTHONPATH=. python3 mech_kernel/examples/03_mock_render.py
# 输出：mock_box_iso.png, mock_box_front.png, mock_box_top.png, mock_box_side.png

# 4. M2 端到端：用户说"建一个圆柱" → 几何生成
PYTHONPATH=. python3 mech_kernel/examples/04_e2e_orchestrator.py

# 13. 固体发动机：装配体多视图、转台和轴向截面
PYTHONPATH=. python3 mech_kernel/examples/13_rocket_motor.py
```

Demo 13 的可视化报告：

- [整机四视图](examples/rocket_motor_out/motor_views.png)
- [整机转台八视图](examples/rocket_motor_out/motor_turntable.png)
- [整机 X=0 轴向半截面](examples/rocket_motor_out/motor_section.png)

---

## 5 轮专家审查记录

| 轮 | 关注点 | 找到的 P0/P1 | 修复 |
|---|--------|------------|------|
| 1 | 架构大方向 | 7 建议 | ✅ 全部落地 |
| 2 | M0 实现 | 7 P0/P1 | ✅ 全部修复 |
| 3 | M0 + 修复版 | 4 新问题 | ✅ 全部修复 |
| 4 | M1 | 4 P0 + 1 P1 | ✅ 全部修复 |
| 5 | M2 + P0 | 3 P0 + 2 P1 | ✅ 全部修复 |

**核心修复**：
- 第 2 轮：事务污染、占位 API 误导、Reference hash、命名歧义、失败渲染、validators、cycle detection
- 第 3 轮：`_push_undo` 尸体、savepoint 语义、嵌套事务、`finally` 保护
- 第 4 轮：缓存键含 revision、manifold 三态、`_geometry_revision`、异常隔离
- 第 5 轮：execute 改 capability registry、`run_loop` 加超时 + 重试 + 去重、Mock 拒绝 UNSUPPORTED

**专家**：gpt-5.6-sol via api.lingshuai.cc

---

## v2 升级路径（3 步就 ready）

### Step 1: 装 build123d

```bash
pip install build123d trimesh
```

### Step 2: 替换 MockBox 为真实几何

```python
# 当前 v1.1：kernel.py 里 extrude 写的是
from .features import MockBox
self._current_geometry = MockBox(float(depth), float(depth), float(depth))

# v2 改为：
from build123d import Part, BuildPart, extrude as b3d_extrude
with BuildPart() as bp:
    b3d_extrude(sketch.to_build123d(), amount=depth)
self._current_geometry = bp.part  # ← property setter 自动 bump revision
```

### Step 3: 替换 Mock Planner 为 LLM Planner

```python
from openai import OpenAI

class LLMPlanner:
    def __init__(self, api_key: str, base_url: str = "https://api.lingshuai.cc/v1"):
        self.llm = OpenAI(api_key=api_key, base_url=base_url)
    
    def decide(self, user_prompt, current_narrative, geometry_summary, last_render_base64):
        ops = self.kernel.cap.list_public()  # LLM 用的 op 列表
        prompt = f"""你是 CAD 工程师。可用 op: {json.dumps(ops)}

        当前状态: {current_narrative}
        几何: {geometry_summary}
        用户: {user_prompt}
        
        下一步 (JSON):
        {{"op": "...", "args": {{...}}, "description": "..."}}"""
        
        resp = self.llm.chat.completions.create(
            model="gpt-5.6-sol",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return PlannerAction(**json.loads(resp.choices[0].message.content))

# 接入
k = MechKernel()
planner = LLMPlanner(api_key="${GPTKEY}")
vision = VisionLLM(api_key="${GPTKEY}")
result = run_loop(k, planner, vision, "建一个法兰盘 Ø100 厚 20，4 个 M8 孔")
```

---

## 项目结构

```
mech_kernel/
├── __init__.py                   # 导出 MechKernel
├── kernel.py                     # 主类（30 API + execute + capability）
├── step_result.py                # StepResult 数据结构
├── errors.py                     # 5 类类型化错误
├── features.py                   # FeatureNode + Sketch + MockBox/MockMesh
├── feature_graph.py              # DAG + cycle detection
├── persistent_naming.py          # 语义引用解析
├── workplane.py                  # Workplane + Registry
├── transaction.py                # Savepoint 事务
├── validators.py                 # 参数校验
├── geometry_inspector.py         # BRep 指标 + 三态拓扑
├── renderer.py                   # matplotlib 多视图/拼图 + LRU + 异常隔离
├── adaptive_renderer.py          # C 方案策略
├── ai_orchestrator.py            # PlannerAction + MockPlanner + run_loop
├── capability_registry.py        # v1.1.1 自动注册 + JSON Schema
├── _pytest_compat.py             # 无 pytest 环境兼容层
├── tests/                        # 10 个测试文件
│   ├── test_kernel.py
│   ├── test_feature_graph.py
│   ├── test_workplane.py
│   ├── test_persistent_naming.py
│   ├── test_savepoint.py
│   ├── test_m1.py
│   ├── test_p0_fixes.py
│   ├── test_p0_v2_fixes.py
│   ├── test_m2_orchestrator.py
│   └── test_p0_v3_fixes.py
└── examples/                     # 4 个 demo
    ├── 01_cylinder.py
    ├── 02_error_types.py
    ├── 03_mock_render.py
    └── 04_e2e_orchestrator.py
```

---

## 与 MechCAD 集成路径

`vanyu0710/aicad` 当前架构：

```
backend/mechcad_ai/
├── vision.py        # ← 可改为直接调 MechKernel vision
├── client.py        # ← 可改为直接调 LLMPlanner
├── normalize.py     # ← 保留（数据预处理）
└── planner.py       # ← 可改为 LLMPlanner 的 adapter

backend/cad_worker/
└── freecad_executor.py  # ← 替换为 MechKernel
```

**集成 3 步**：
1. 把 `MechKernel` 装到 `cad_worker` 里（替换 FreeCAD 调用）
2. 把 `vision.py` 的 OpenAI/Anthropic 调用接到 `CapabilityRegistry.list_public()`
3. 把 `planner.py` 改成 `LLMPlanner`（用 `MechKernel.execute`）

---

## 性能指标

- **API 调用延迟**：< 1ms（mock 几何）/ 10-100ms（真实 build123d）
- **事务 commit 开销**：< 5ms（深拷贝 feature_graph + sketches + workplanes）
- **渲染延迟**：50-200ms / 4 视角（matplotlib 离屏）
- **缓存命中**：< 1ms（OrderedDict O(1) 查找）
- **3D PNG 输出**：~200x200px / ~30KB

---

## 已知限制（v2.2）

1. **导入/加载/装配会话暂不支持参数化重放**，会返回 `RECOVERABLE`
2. **截面是渲染用派生几何**，不会改变当前模型或进入历史
3. **无 web 渲染**：当前为 matplotlib 离屏 PNG
4. **单进程**：无并发安全（Feature Graph 内存态）

---

## 版本演进

- v2.0: 参数化 op 历史、全量重放、真实 delete/update/rebuild
- v2.1: 剖面实体、STEP 装配与几何查询
- v2.2: AI 多视图、转台、标注拼图与真实截面
- 后续：约束、装配语义、WebGPU 渲染与持久化历史

---

## 许可证

MIT

---

## 作者

Mavis (MechCAD IDE AI 团队)  
专家审查：gpt-5.6-sol via api.lingshuai.cc

---

**v2.2 状态：多视图/截面视觉闭环已接入，参数化重放与真实几何能力可用。**
