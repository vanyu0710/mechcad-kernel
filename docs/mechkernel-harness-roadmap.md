# MechKernel × aicad — 带 CAD 的 Codex：harness 化 + 人机协作集成路线图

> 状态：规划文档 · 供接手对话直接开工
> 更新：2026-09-02
> 范围：本文件只描述方案与分阶段任务，不含可执行代码。接手对话应从这里开工，从 Phase 0 + Phase 1 的"单会话垂直切片"做起。

---

## 0. 一句话定位

把 `mechcad-kernel`（MechKernel v2.11，已 harness-ready 的参数化 CAD 内核）接入 `aicad`（FastAPI + React + Three.js 的 AI CAD IDE），把产品形态从 aicad 现在的「一次性规划 → 特征树」改成 **CAD 领域的 Codex**：AI agent 逐步自主建模，用户像 review 代码一样随时看进度、改参数、确认关键决策，同时保留 CAD 内核的专业能力（参数化特征历史、精确 BRep 几何、测量、选边/选面、可重放历史）。

## 1. 两仓现状（2026-09-02 核对）

### 1.1 MechKernel (`G:\lfy design\ai cad\mechcad-kernel`)

- Python 3.12 + build123d==0.11.1 + cadquery-ocp-novtk==7.9.3.0，真实 OCC 7.9.3 几何。
- 主类 `mech_kernel/kernel.py` (4961 行)。`PUBLIC_OPS` 33 + `EXPERIMENTAL_OPS` 10（装配/参考系/碰撞，见 kernel.py:58-79）。
- 对外执行入口 `MechKernel.execute(op, *args, allow_experimental=False, **kwargs)`（kernel.py:4311）。
- **harness-ready 关键点（写方案时已确认存在）**：

| 能力 | 位置 | 对"CAD 版 Codex"的意义 |
|---|---|---|
| `cap.list_public()` → 33 op JSON schema | `capability_registry.py` | LLM tool schema 直接动态生成 |
| `StepResult`（success/geometry_summary/渲染/错误/`suggestion{action,fix,reason_code}`） | `step_result.py` | 逐步执行的结构化反馈 + 自修复燃料 |
| `feature_graph`（DAG）+ `FeatureNode` | `feature_graph.py`、`kernel.py` | **特征树数据源**（人机协作编辑锚点） |
| `_op_history` + `_replay()` + `delete_feature/update_feature/rebuild` | kernel.py:2660-2785, 4097 | 参数化重放：改参数 → 全量重算 |
| `select(element_type="face"/"edge")` → 引用 `F03`/`E12` | kernel.py:2460 | 选边/选面 + 几何证据（面积/长度/中心）回喂 |
| 任意面草图 `create_workplane(face_ref)`、任意方向 `hole(direction)`、`shell(face_refs)` | kernel.py | 从"只能 Z 向 box/hole"大幅解放 |
| `snapshot()/_restore()`、事务 Savepoint、undo/redo | kernel.py:3694, transaction.py | 会话状态保存 / 回退 |
| `export`、`render`、`query`、`measure`、`validate_geometry` | kernel.py | 几何产物 + 证据 |
| ID 生成器实例化 `_ids`（多会话互不污染） | kernel.py `__init__` | 一会话一实例时保证隔离 |

### 1.2 aicad (`G:\lfy design\ai cad\varen cad\aicad`)

- FastAPI + React + Three.js + WS。AI 层 `backend/mechcad_ai/`（OpenAI/Anthropic 兼容 client + prompts.yaml 集中提示词）。CAD 层 `cad_worker/freecad_executor.py`（受控子进程，`--plan`/`--out` 契约）。
- **FeaturePlanV3 语义层**（`backend/schemas.py`）：LLM 一次性输出特征树 → `normalize_feature_plan` → `validate` → evidence gate → worker 映射 build123d。特征集 16 种（box/cylinder/hole/slot/groove/boss/pad/rib/pattern），fillet/chamfer/非 Z 轴 unsupported（`cad_worker/freecad_executor.py` 会把 fillet/chamfer `_skip`）。
- 前端 `frontend/src/FeatureTree.tsx` / `FeatureForm.tsx` 直接渲染 FeatureV3（any 类型），无独立 feature palette。
- `backend/session.py` snapshot/undo/redo、`backend/storage.py` 工件、`backend/cad.py` 调 worker。
- **关键：backend 进程本身不 import CAD 库**（除 `backend/geometry/measurement.py` 只读测量），CAD 只跑在 worker 子进程。这正是接入 MechKernel 的天然边界。

### 1.3 关键差距（为什么 FeaturePlanV3 不是好的执行层）

FeaturePlanV3 一步 `box_base` ≈ MechKernel 四步（create_workplane→new_sketch→add_rectangle→extrude）。aicad 用"语义特征树"抽象来掩盖这个粒度差，但代价是：a) 只能在 16 种预定义特征里选；b) worker 是第二套 build123d 映射，与内核脱节；c) 无法表达选边选面/参考系/自由 op。**结论：执行层应当直接是 MechKernel，FeaturePlanV3 不承担执行语义。**

## 2. 目标架构（已确认的 5 条决策）

```
┌───────────────────────────── React IDE (保留 aicad 前端壳) ─────────────────────────────┐
│  Three.js 视口(OBJ/STL) · 特征树(渲染 feature_graph) · 属性面板(改参数) ·                │
│  确认请求面板 · 改动清单 diff 面板 · 接管/恢复开关                                        │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │ REST + WS (agent_step / approval_required / changeset_ready / take_over)
┌───────────────▼───────────────────────────────────────────────────────────────┐
│  FastAPI backend (D2: 永不 import CAD 库; 保留 session/WS/工件/mechcad_ai)      │
│   Agent loop (核心改造):                                                        │
│     cap.list_public() 动态生成 LLM tools                                        │
│     循环: 观察(feature_tree 摘要+narrative+geometry_summary) → LLM 决策 →       │
│           调 worker RPC execute → 读 StepResult → 自修复(RECOVERABLE+fix)       │
│     人在回路: 低打扰确认点(推断尺寸/破坏性操作/选边歧义) + 改动清单批量审批 +       │
│              用户随时暂停接管手动编辑(D4)                                        │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │ JSON-lines RPC (子进程 stdin/stdout)
┌───────────────▼───────────────────────────────────────────────────────────────┐
│  MechKernel worker 子进程 (D2) — 常驻, 一会话一实例                             │
│   commands: capabilities / execute / feature_tree / select_refs /               │
│             update_feature / delete_feature / undo / redo / set_parameter /     │
│             export / measure / snapshot / restore / shutdown                    │
│   内部: MechKernel().execute(op, **kw) → StepResult                             │
│         feature_graph + _op_history 即特征树与重放源 (D1)                       │
└───────────────────────────────────────────────────────────────────────────────┘
```

**决策清单**：
- **D1 特征锚点 = MechKernel feature_graph / _op_history**。前端特征树/属性面板直接渲染它；"改参数" = `update_feature`/`set_parameter` → 参数化重放 → 3D 刷新。FeaturePlanV3 不做执行语义层（可作为历史参考，不进主链路）。
- **D2 进程边界**：MechKernel 跑 worker 子进程 RPC，backend 永不 import CAD 库。一会话一实例。
- **D3 验证策略**：本轮不重建 aicad 的 semantic verifier / evidence gate。正确性靠：(a) 每步 `validate_geometry`；(b) `select` 返回的几何摘要（面积/长度/中心）作为证据喂给 agent；(c) RECOVERABLE+fix 自修复；(d) 快照可回退。保留只读测量。
- **D4 人在回路 = 低打扰 + 改动清单 + 随时接管**：默认只在"推断尺寸、破坏性操作(替换 confirm_replace / cut / 清空)、选边歧义"暂停征求用户；定期生成"改动清单(diff 式)"批量审批；用户可随时暂停 agent 接管手动编辑，改完交回。
- **D5 保留 aicad**：React IDE 壳、Three.js 视口、mechcad_ai 客户端、WS/会话/工件基础设施、family template 概念（迁为宏 op）。

## 3. 组件拆分

### 3.1 MechKernel worker server（新建，`mech_kernel/server.py`）
- 常驻子进程，stdio JSON-lines 协议：`{"id", "cmd", "payload"}` → `{"id", "ok", "data" | "error"}`。
- 命令集（见架构图）。每个 kernel 实例独立，进程崩溃可重建并 `load_project` 恢复（或从 `_op_history` 重放）。
- 复用 MechKernel 现成方法，server 只是薄 JSON 壳。`execute` 需带 `allow_experimental` 透传。
- **验收**：Python 脚本起 server → `capabilities` 返回 33 op schema → `execute` 建 box+hole → `export` 出 STEP → `feature_tree` 返回图 → `undo` 生效。

### 3.2 Agent loop（新建 `backend/agent/`，核心）
- 工具描述：由 worker `capabilities` 动态生成（description/inputs/examples），不硬编码。
- 每轮上下文：当前 `feature_graph` 摘要 + `narrative` + `geometry_summary`（体积/bbox）+ 上一步 `StepResult`。
- 决策 → 调 worker `execute` → 读 `StepResult`：
  - `success`：记入历史，必要时触发几何变更事件让前端刷新。
  - `RECOVERABLE` + `suggestion.fix`：直接按 fix 自动重试（先做"只改 fix 字段、排除非 schema 键"的过滤，参考 v2.11 修过的 orchestrator bug）。
  - `error_kind` 分类提示。
- **人在回路确认点**（低打扰）：
  - 推断尺寸：agent 要用的关键尺寸若 `confirmed_by_user=False`，且操作含 `mode="cut"`/`confirm_replace`，先问用户。
  - 选边歧义 / 曲面建面：`select` 无匹配或 `face_not_planar` 等 → 暂停问用户。
- **改动清单**：agent 完成一批（如 3-5 步）后，生成 `changeset`（"把 flange_sk 的 Ø120 拉伸到 12mm；加 Ø30 通孔…"），前端 diff 面板展示，用户 approve / 改参 / 拒绝。
- **验收**：输入"做一块 120×120×12 的法兰，中心 Ø30 通孔，6 个 Ø8 螺栓孔在 Ø90 圆上"，agent 自主完成建模并产出 STEP；中途能弹出确认。

### 3.3 前端改造（`frontend/src/`）
- FeatureTree / FeatureForm 数据源切到 `feature_graph`（不再读 FeatureV3）。
- 新增：确认请求面板、改动清单 diff 面板、接管/恢复开关。
- 3D 视口基本复用（OBJ/STL），agent 每步后可触发重新拉取。

### 3.4 后端接线
- `session` 持有当前 worker 进程句柄；WS 事件扩 `agent_step/approval_required/changeset_ready/take_over`。
- `backend/cad.py` 改为 RPC client（或另写 `worker_client.py`），保留超时/重连/崩溃恢复。

### 3.5 宏 op（可选，降低 LLM 步数与错误率）
- 把 aicad 的 family template（flange/tube/link_plate/phone_stand/spur_gear）改写成 **MechKernel 宏 op**：一个命令内部跑多步内核 op + 参数化（`kernel_server` 支持一次发一串 op）。
- 给 agent 的 tool 列表同时含"原子 op"和"宏 op"，LLM 按需选择。

## 4. 分阶段任务 + 验收

| Phase | 目标 | 关键任务 | 验收 / 测试 |
|---|---|---|---|
| **P0** | MechKernel worker RPC 冒烟 | `server.py` 命令集；单实例会话 | 脚本驱动：capabilities/execute(box+hole)/export STEP/feature_tree/undo 全通过 |
| **P1** | Agent 垂直切片（文字 → 3D） | `backend/agent/` 最小 loop：动态 tools + execute + StepResult 读回；接一个真实 LLM（mechcad_ai client） | 输入"法兰盘 + 中心孔 + 6 螺栓孔" 描述 → agent 自主建模 → 前端 3D 可见 + feature_graph 正确 |
| **P2** | 人在回路确认/接管 | 确认点（推断尺寸/破坏性 op/选边歧义）；暂停/接管/恢复 | 上述场景会弹确认；用户接管改参数后 agent 能继续 |
| **P3** | 改动清单批量审批 | changeset diff 面板 + approve/改参/拒绝 | agent 一批后出 diff，approve 后继续，改参/拒绝生效 |
| **P4** | 打磨 | 每步几何验证与证据回喂、渲染喂 vision、多会话并发池、worker 崩溃重放恢复、prompts 集中化 | 长流程稳定；崩溃恢复成功；2 会话并行互不干扰 |

建议**先做 P0+P1 的"单会话垂直切片"**：投入小、能端到端验证架构对，再扩展人机协作（P2/P3）。

## 5. 依赖与运行坑

- **版本对齐**：MechKernel 需 build123d==0.11.1 + cadquery-ocp-novtk==7.9.3.0 + Python 3.12（Windows x64）。aicad 只声明 `build123d>=0.7.0` → 统一到 0.11.1 / 7.9.3.0。ai cad 父目录下 `mechcad-kernel/.venv` 已是该版本，可直接让 worker 用这个解释器，或给 aicad 建同版本 venv。
- **MechKernel 可导入**：worker server 需把 `mechcad-kernel/` 加进 `sys.path`（或装成包）。backend 进程不要 import。
- **常驻进程生命周期**：worker 需处理 shutdown/重启；崩溃后按 `_op_history` 重放或 `load_project` 恢复；超时/断线在 backend 侧兜底。
- **Windows / 编码**：RPC 用 UTF-8；子进程启动注意 `start-mechcad-pro.cmd` 那条已有系统托盘包装可复用。
- **examples 佐证**：MechKernel `mech_kernel/examples/16_gearbox_housing.py` 就是"仅公开 API + 选边/选面 + 任意方向孔 + 面上草图"的复杂单零件全流程参考；`mech_kernel/tests/test_v11_part_modeling.py` 是这批能力的测试范式。

## 6. 保留 / 冻结 / 新建清单

| 动作 | 内容 |
|---|---|
| 保留（aicad） | React IDE、Three.js 视口、mechcad_ai 客户端、WS/会话/工件、family template 概念 |
| 保留（MechKernel） | 全部 33 公开 op + 10 experimental、feature_graph/_op_history/replay、select 引用、事务 undo、渲染 |
| 冻结（本轮不动） | aicad 的 FeaturePlanV3 执行链（可留作旧路径用 `MECHCAD_CAD_ENGINE` 切回）、semantic verifier/evidence gate |
| 新建 | `mech_kernel/server.py`（worker RPC）、`backend/agent/`（loop）、worker_client、前端 3 个新面板 |
| 迁移 | aicad family template → 宏 op |

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| op 细粒度导致 LLM 长序列易错 | harness 自修复 + 宏 op 降步数 + 每步 validate_geometry |
| 丢掉 semantic verifier 后质量下降 | select 几何证据回喂 + 快照回退 + 批量审批人眼把关（用户已接受 D3） |
| worker 崩溃丢会话 | 一会话一实例 + _op_history 重放恢复 + 崩溃自动重启 |
| execute 同步黑盒（无进度/取消） | P4 加事件/进度/取消；前端先靠 WS agent_step 事件 |
| 版本漂移（build123d） | 统一 pin 0.11.1 / OCP 7.9.3 / py3.12 |

## 8. 接手开工指引

1. 目录：`G:\lfy design\ai cad\mechcad-kernel`（内核）、`G:\lfy design\ai cad\varen cad\aicad`（IDE）。
2. venv：`mechcad-kernel\.venv`（py3.12 + build123d 0.11.1 + OCP 7.9.3）。验证：`\.venv\Scripts\python.exe -c "import mech_kernel; print('ok')"`。
3. 先读：内核 `docs/HANDOFF.md`（v2.11）+ `mech_kernel/kernel.py` 的 execute/feature_graph；aicad `HANDOFF.md` + `backend/cad.py` + `cad_worker/freecad_executor.py`（学习 RPC/工件契约参考）。
4. 建议首任务：**P0** — 写 `mech_kernel/server.py` 薄 JSON-RPC 壳，用 `mech_kernel/examples/01_cylinder.py` 的建模序列冒烟；然后 **P1** — 用最小 agent loop 接真实 LLM，跑通"文字 → 3D"单会话切片。
5. 每阶段保持两仓各自测试可跑：内核 `PYTHONPATH=. .\.venv\Scripts\python.exe mech_kernel/tests`（当前 334/334）；aicad `python -m unittest discover -s tests`。
