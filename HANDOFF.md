# MechKernel Handoff

Updated: 2026-09-01 (v2.11)

## Current Repository State

- Repository: `https://github.com/vanyu0710/mechcad-kernel.git`
- Branch: `main`
- Latest stable commit: v2.11 (零件建模全流程)
- Current test result: **329/329 通过** (302 旧 + 27 新 v2.11 测试)
- 测试环境: 仓库根 `.venv` (Python 3.12 + build123d 0.11.1 + OCP 7.9.3), 无 fixture 依赖 mock runner
- License: **AGPL-3.0-or-later** (since v2.5 commit `aa9e2b3`)
- No API keys, `.env` files, VPN settings, or provider credentials are committed.

## Implemented Features (v2.11)

### v2.11 (本次) — 零件建模全流程, 装配降级
- **装配 10 op 标 experimental**: PUBLIC_OPS 43→33 + EXPERIMENTAL_OPS;
  execute() 默认拒绝, `allow_experimental=True` 放行; capability permission=experimental;
  LLM 默认能力集聚焦零件建模。代码冻结保留, 直调可用, demo 14 不受影响
- **选边/选面闭环** (新模块 `topology_refs.py`):
  - select 扩展 `element_type="edge"`, 返回项带 `ref` ("F03"/"E12") + 类型/长度或面积/中心/法向摘要
  - 引用按 geometry revision 校验新鲜度; 过期 → RECOVERABLE(reason_code="stale_topo_ref")
  - fillet/chamfer `edges` 接受 "all" | 引用列表; shell `face_refs` 任意开口面
  - 面上草图: `create_workplane(face_ref=...)` 从选中平面派生坐标系 (曲面诚实拒绝)
- **安全基线**:
  - extrude/revolve/sweep `mode='new_body'` 已有几何时 RECOVERABLE + `confirm_replace=True` 逃生门
    (此前静默清空整个零件 — LLM 驱动流程高频事故源)
  - sweep 加 mode (new_body/add/cut) + rectangle 剖面, 消除已有几何静默丢弃
  - linear_pattern/mirror/boolean 深度参数化 (硬编码 50 → 显式/自动推导零件 Z 尺寸+2mm + warning)
- **草图强化**: custom workplane origin/axes 修复 (此前被丢弃) + offset 偏置;
  extrude 全链路尊重 workplane 放置 (BuildSketch 必须显式传 plane 的 build123d 坑已修);
  混合剖面真弧线 wire (ThreePointArc, 0 采样误差; 回退采样折线 + warning); revolve 拒绝非标准面草图
- **hole 升级**: `direction` 任意面进入 (top/bottom/x+/x-/y+/y-); countersink 真 90° 锥面
- **harness 接口**: RecoverableError 类型化异常 + suggestion `{action, fix, reason_code}`;
  unknown field 附 valid_fields; cut 无切除警告; extrude `reverse` 参数 (面上草图切削进材料);
  ID 生成器实例化 (多实例互不污染); orchestrator 重试合并 bug 修复; reference frame 持久化

### v2.9.1
- **专家审查修复** (DeepSeek gpt-5.4-mini 13 findings + 自我审查):
  - **P0** 修 `assemble()` `local_origin` 死代码 (`position or [...]` → `position`)
  - **P1** `coaxial` 校验允许反向 (齿轮啮合需要 `|dot| > 1-eps`); 加 `coaxial_aligned` 严格模式
  - **P1** `collision.check_pair_interference()` 加 `strict=False` 参数, 区分 expected vs unexpected 异常
  - **P1** `reference_frames.resolve_placement()` 旋转语义 docstring 详细化
  - **P2** `gear.py` 加 WARNING: 梯形齿形是 proxy, 啮合有 100-500mm³ 假干涉
  - **+3 测试** (v7.1: 旋转语义, coaxial 反向, coaxial_aligned 严格)

### v2.9 (碰撞检查)
- 新模块 `mech_kernel/collision.py` (229 行)
- 3 公开 API: `check_pair_interference`, `check_assembly_interference`, `check_interference_matrix`
- `MechKernel.check_interference()` (PUBLIC_OPS 42→43)
- 算法: build123d `Part & Part` → OCC `BRepAlgoAPI_Common` → volume > tolerance = 干涉
- 11 测试 (`test_v9_collision.py`): 有/无干涉, tolerance, 矩阵对称性, 真实齿轮啮合 (307mm³)
- **已知限制**: O(N²) 复杂度, 100 parts 估 20-30s. 留给 v2.10 加 AABB broad-phase

### v2.8 (真实齿轮)
- 新模块 `mech_kernel/gear.py` (170 行)
- 3 公开 API: `gear_geometry`, `center_distance`, `build_involute_gear`
- 严格 ISO 6336-1 / AGMA 2015 几何参数
- 梯形齿形 proxy (避开 build123d 真 involute 曲线 + OCP `TopoDS::Face` 异常)
- 10 测试 (`test_v8_gear.py`)
- Demo 14 v2.8.1-v2.8.4: 装配布局演化 (散点 → inline 三轴 → 统一中心距 → in-kernel build123d)

### v2.7 (参考坐标系)
- 新模块 `mech_kernel/reference_frames.py` (326 行)
- 5 公开 API: `create_reference_plane`, `query_reference`, `resolve_point`, `resolve_placement`, `validate_assembly`
- 8 kind 关系校验: coaxial / coaxial_aligned / parallel / perpendicular / clearance / mounted / inside / gear_mesh
- 20 测试 (`test_v7_reference_frames.py`)
- Demo 14 v2.7: 5 reference frame, 13 instance, 0 issues

### v2.6 (几何可靠性)
- `validate_geometry(target, level)` 三级校验 (basic/standard/strict)
- 提交前事务回滚, 稳定 reason code, 确定性 fingerprint
- 5 类类型化错误 (InvalidRequest / KernelBug / StateCorruption / GeometryFailure / Recoverable)

### v2.5 (渲染 + 装配)
- OCC 优先双渲染后端, 无头 matplotlib fallback
- 实例级装配显示 (visibility / color / scene manifest)
- 4 视图 + 转台 + 真实截面

### v2.4 (约束参数化)
- 8 草图约束: coincident / horizontal / vertical / parallel / perpendicular / distance / radius / equal
- SciPy 求解器, 命名参数触发全量重放
- 严格模式失败回滚, `best_effort` 诊断

### v2.0-v2.3 (基础架构)
- v2.3 视觉证据引擎
- v2.2 多视角截面渲染
- v2.1 剖面 (revolve) + 草图实体 (add_polyline / add_arc) + 装配 (assemble)
- v2.0 参数化重放引擎 (delete/update/rebuild 真实重算)

### v1.x (基础 op)
- 25+ op 全部真实实现 (草图 / 主体 / 细节 / pattern / query / I/O / 事务)
- build123d 0.11.1 + OCP 0.18 真实集成 (清华源)
- 5 类类型化错误 + CapabilityRegistry 自动注册

## Demo 14 Status (v3.1)

`mech_kernel/examples/14_gearbox.py` 完整 in-kernel build123d 建模, 包含:

- **Housing** (220×200×100): 中空外壳, 上下打通, 看见内部齿轮
- **3 根轴** (长度 200, Z=30 居中): input + intermediate + output
- **4 个齿轮** (梯形齿形, module=2.0, z=20/60/60/20): 真实 ISO 6336 几何
- **5 reference frame** (world + housing_mount_plane + 3 *shaft_axis)
- **13 装配 instance** (housing + 3 shafts + 4 gears + 4 caps + 2 bearings), 全部通过 `mount_frame` + `resolve_placement` 放置
- **减速比 3:1**: input 1500 RPM → intermediate 500 → output 166 RPM
- **多视角渲染**: iso / front / top / side / presentation / ratio_diagram
- **v2.9 碰撞报告**: 21 pair (7 parts), 7 干涉 (housing 切齿轮 8275mm³, 齿轮啮合 307mm³, 轴穿齿轮孔 23-44mm³)
- **导出**: STEP (2.7MB), JSON report, 6 PNG

**已知限制** (仍需 v2.10+):
- 齿轮是梯形齿形 (真 involute v2.10)
- 啮合时 ~307mm³ 假干涉 (梯形 vs 真圆齿)
- housing 切齿轮 8275mm³ (设计选择: housing 边缘覆盖齿轮齿顶)
- 轴在齿轮 bore 内 (boolean union 行为)

**测试 demo 14**:
```bash
PYTHONPATH=. python3 mech_kernel/examples/14_gearbox.py
# 耗时 ~10s, 输出到 mech_kernel/examples/gearbox_out/
```

## Module Map

```
mech_kernel/
├── kernel.py              (4296 行, 43 PUBLIC_OPS, 真实 build123d + OCP)
├── step_result.py         (StepResult dataclass)
├── errors.py              (5 类类型化错误)
├── units.py / validators.py
├── features.py            (FeatureType / Sketch / Workplane / Reference / Constraint)
├── feature_graph.py       (DAG)
├── persistent_naming.py
├── workplane.py
├── transaction.py         (Savepoint, MAX_NESTING_DEPTH=10)
├── geometry_inspector.py  (validation + fingerprint)
├── renderer.py            (OCC 优先 + matplotlib fallback)
├── adaptive_renderer.py
├── ai_orchestrator.py
├── capability_registry.py (43 op JSON Schema)
├── build123d_adapter.py   (fallback)
├── _pytest_compat.py
├── collision.py           (v2.9, OCC BRepAlgoAPI_Common)
├── topology_refs.py       (v2.11, face/edge 引用缓存 + 新鲜度校验)
├── reference_frames.py    (v2.7, CoordinateFrame + FrameRegistry)
├── assembly.py            (v2.5 + v2.7 mount 字段)
├── constraint_solver.py   (v2.4, SciPy)
├── occ_renderer.py        (v2.5, AIS/V3d 边界)
├── sketch_renderer.py     (v2.5)
├── _runtime_compat.py     (v2.5)
├── gear.py                (v2.8, ISO 6336 + 梯形齿形)
├── llm/
│   ├── deepseek.py        (DeepSeek Vision/Chat 客户端)
│   └── openai_compatible.py
├── tests/                 (291 tests)
│   ├── test_v7_reference_frames.py  (23 tests)
│   ├── test_v8_gear.py              (10 tests)
│   ├── test_v9_collision.py          (11 tests)
│   └── ... (其他 247 tests)
└── examples/              (12 + demo 14 v3.1)
    ├── 01_cylinder.py ... 12_query_measure.py
    └── 14_gearbox.py      (v3.1: 完整 gearbox pipeline)
```

## Public Ops (v2.11: 33 公开 + 10 experimental)

**公开 (默认 LLM 能力集, 33)**:

| 类别 | op | 数量 |
|------|-----|------|
| 草图 | create_workplane (custom/offset/face_ref), new_sketch, close_sketch, add_circle, add_rectangle, add_line | 6 |
| 主体 | extrude (+reverse/confirm_replace), revolve, sweep (+mode), boolean | 4 |
| 细节 | fillet (+边引用), chamfer (+边引用), shell (+face_refs), hole (+direction/真锥面) | 4 |
| pattern | circular_pattern, linear_pattern (+depth), mirror (+depth) | 3 |
| query | query, select (+element_type=edge/引用), measure | 3 |
| I/O | export, import_step, save_project, load_project | 4 |
| 事务/编辑 | undo, redo, delete_feature, update_feature, rebuild | 5 |
| 剖面 | add_polyline, add_arc | 2 |
| 渲染/校验 | render, validate_geometry | 2 |

**experimental (装配相关, 10)**: assemble, query_assembly, set_instance_visibility, set_instance_color,
create_reference_plane, query_reference, resolve_point, resolve_placement, validate_assembly, check_interference

## Next Development Target: v2.12+

方向已定: 装配冻结, 内核将集成到 AI CAD 软件 (类 harness 架构: LLM agent 循环 → 内核工具调用 → 结构化结果反馈 → 自修复)。

零件建模剩余缺口 (按优先级):
1. **特征级 pattern/mirror** (阵列已有 3D 特征而非重拉草图) + loft/rib/draft. 1-2 周.
2. **sweep 真路径** (圆弧/样条 path + 多剖面形状). 3-5 天.
3. **约束求解覆盖 arc/polyline** + tangent 约束. 3-5 天.
4. **spline/ellipse/slot 草图实体**. 2-3 天.
5. **AABB broad-phase** (collision 性能). 1 周.
6. **拆分 kernel.py** (4400+ 行 → 多模块). 1 周.
7. **螺纹孔 / 孔标注** (简化表示). 待定.

harness 集成阶段 (v2.11 已铺路, 集成时做):
- planner prompt 从 cap.list_public() 动态生成 (现硬编码 6 op 于 deepseek.py)
- run_loop 逐步决策适配器 (现在 plan() 一次性批量 vs run_loop 逐步)
- 事件/进度/取消机制 (execute() 目前同步黑盒)
- 会话管理壳 (每会话一 kernel 实例 + RPC/队列)
- ⚠️ AGPL-3.0 许可证: 闭源商业集成前需法律评估或双授权

## Self-Review Summary (v2.9.1)

| 项 | 数量 | 状态 |
|---|---|---|
| 真实 bug (P0) | 1 | ✅ 1/1 修 (assemble 死代码) |
| 真实问题 (P1) | 3 | ✅ 3/3 修 (coaxial 反向, collision strict, 旋转 docstring) |
| 重要问题 (P2) | 4 | ⚠️ 1/4 修 (gear docstring); 3 留 v2.10 |
| 误判 (专家错) | 2 | ✅ 验证 atomic Transaction + validate_geometry 真只读 |

**距离工业 1.0**: ~2-3 人月 (self-eval 6.5/10, 之前 3-5 人月)

详细 self-review: `/workspace/MechKernel_SelfReview_v29.md`
专家审查: `/workspace/expert_v9_review.md`

## Verification Commands

```bash
# 全测试 (329/329) — 建议用仓库根 .venv
PYTHONPATH=. ./.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, '.')
import mech_kernel._pytest_compat as mock
sys.modules['pytest'] = mock
sys.exit(mock.main(['mech_kernel/tests']))
"

# v2.11 新测试
PYTHONPATH=. ./.venv/Scripts/python.exe mech_kernel/tests/test_v11_part_modeling.py

# 跑 demo 14 (v3.1 in-kernel gearbox)
PYTHONPATH=. python3 mech_kernel/examples/14_gearbox.py

# 单个 v2.9 碰撞测试
PYTHONPATH=. python3 mech_kernel/tests/test_v9_collision.py

# 单个 v2.8 齿轮测试
PYTHONPATH=. python3 mech_kernel/tests/test_v8_gear.py

# 单个 v2.7 reference frame 测试 (v7.1 加 3 个)
PYTHONPATH=. python3 mech_kernel/tests/test_v7_reference_frames.py
```

## Git/Push Notes

- **沙箱里 SSL 验证失败**, 必须用 `GIT_SSL_NO_VERIFY=true`
- Push 命令:
  ```bash
  GIT_SSL_NO_VERIFY=true git push https://${GITHUBKEY}@github.com/vanyu0710/mechcad-kernel.git main
  ```
- 验证 push: `git fetch origin` 后用 `git rev-parse origin/main` 跟 local 对比

## Security Notes

- 真实 API key 只在 process environment (`.env` Git 忽略)
- Planner / Vision 客户端 redact key (不出现在 `repr` / log / exception / 序列化)
- LLM 平台现状 (2026-09-01):
  - ✅ DeepSeek 官方 (`DSKEY` 环境变量, 但需 balance > 0)
  - ✅ lingshuai.cc (`GPTPLUS`, gpt-5.4-mini 性价比高, gpt-5.6-sol 502/timeout)
  - ❌ OpenAI 官方 (沙箱网络隔离)
  - ❌ Anthropic (403)
  - ❌ free.ai / Algion / OpenRouter (无 key 或 401)
- Demo 14 跑不需要 LLM (纯几何 + 渲染), 用 LLM 只在 `06_end_to_end_llm.py`
