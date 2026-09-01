# v2.11 零件建模全流程(装配降级 + harness 集成铺路)

已确认范围: **全量一个版本**;装配 10 op **标记 experimental**(代码保留)。

## WS1 安全基线 (P0)
1. 修 3 个失败测试(test_v7_assembly_renderer.py:38,61、test_v8_geometry_validation.py:76):`tmp_path` fixture 参数改为 `tempfile.TemporaryDirectory()` helper,兼容自写 mock pytest,目标全绿。
2. `extrude` new_body 防护(kernel.py:793-820):已有几何时 new_body → RECOVERABLE,suggestion 给 `{"fix": {"mode": "add"}}` 与 `{"fix": {"confirm_replace": true}}` 逃生门;schema/受影响 demos/tests 同步。import_step 不变。
3. sweep 修复(kernel.py:1723-1807):加 `mode` (new_body/add/cut) 对齐 extrude 语义,消除已有几何静默丢弃;剖面支持 circle + rectangle。
4. pattern/mirror 深度必填化(kernel.py:1588,1695,boolean 内 1127/1133):去掉硬编码 50,schema 与 demos/tests 同步。

## WS2 选边/选面闭环 (P1 核心)
1. 拓扑引用层(新文件 `mech_kernel/topology_refs.py`):按 `_geometry_revision` 缓存 face/edge 枚举(index → TopoDS_Shape + 类型/长度或面积/中心摘要);引用格式 `"F03"`/`"E12"`;revision 变更后旧引用 → RECOVERABLE `{"action": "re_select"}`。
2. `select` 扩展(kernel.py:2100-2173):加 `element_type` ("face"|"edge",默认 face 向后兼容),edge 按类型过滤;返回项带 `ref` + 几何摘要,替换"不可回喂"声明。
3. `fillet`/`chamfer`(kernel.py:1194,1251):`edges` 接受 `"all" | [refs]`,解析后传 build123d。
4. `shell`(kernel.py:1407-1494):开口面接受 face refs(方向匹配保留为 fallback)。
5. `hole`(kernel.py:1279-1405):加 `direction`("X"/"Y"/"Z" 带正负,默认 "+Z" 兼容);countersink 尝试真锥面(OCC cone cut),不稳则降级直壁 + warning。
6. 面上草图:`create_workplane` 接受 `face_ref`,从选中平面派生 origin/normal/x_axis;曲面 → RECOVERABLE。
7. capability schema 同步 + 重点 op 填 examples few-shot。

## WS3 草图强化 (P2)
1. custom workplane 修复(kernel.py:390-402):origin/axes 真正传入 Workplane;基准面支持 `offset`。
2. 混合轮廓 arc 保真(`_collect_closed_profile` kernel.py:3395-3456):先尝试 build123d 三点弧真 wire,失败 fallback 采样折线 + StepResult warning。

## WS4 harness 接口准备 (P2)
1. ID 生成器实例化(features.py:297-301):模块级全局 → 实例属性;`_replay` 播种(kernel.py:3519)只重置本实例;多 kernel 实例同进程互不污染。
2. 结构化自修复:suggestion 统一 `{"action", "fix": {param: value}, "reason_code"}`,覆盖 new_body 冲突 / fillet 过大 / unknown field;接通 errors.py 死代码 reason codes ≥3 个高频路径;修 orchestrator 重试合并 bug(ai_orchestrator.py:332 过滤非 schema 字段)。
3. reference frame 持久化:`_snapshot`(kernel.py:3310-3328)+ save/load 接入 FrameRegistry to_dict/from_dict。
4. 装配 op 标 experimental:模块级 PUBLIC_OPS(kernel.py:51-68)移除 10 op;注册循环(kernel.py:378-382)per-op permission="experimental";CapabilityRegistry 加 experimental 字段;`execute()` 加 `allow_experimental=True` 开关;demo 14 与受影响测试(test_v2_fixes.py:44-61、test_v4_profiles.py:67)改为直调方法或带开关。

## WS5 文档 + 验证
1. 新增 `test_v11_*.py`(无 fixture 风格):引用生命周期、选边 fillet 体积验证、new_body 防护、sweep fuse、hole 方向、面上草图、ID 隔离、suggestion 结构。
2. README/HANDOFF 更新:op 表(公开 43→35 + 10 experimental)、零件建模全流程指南、harness 集成就绪度章节(含 AGPL 商业集成注意事项)。
3. 全量测试 + demo 01-12 冒烟 + demo 14 回归。

## 验收标准
- 全测试通过(302+/302+)
- 典型流程跑通:select edges → fillet 指定边 → select face → 面上草图 → extrude cut → 指定方向 hole → export
- 同进程两个 MechKernel 实例互不污染
- LLM 默认能力集不含装配 op;错误 StepResult 带可执行 fix 参数

## 明确不做(留到集成阶段)
planner prompt 动态化与 run_loop 适配器;事件/进度/取消;loft/rib/draft/特征级 pattern;spline/ellipse 实体;约束求解覆盖 arc;多 body 管理。

## 执行顺序
WS1 → WS2 → WS3 → WS4 → WS5,每条线完成即跑全量测试,分批提交。