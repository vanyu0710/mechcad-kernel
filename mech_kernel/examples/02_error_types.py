"""
Demo 2: 演示 5 类错误分类
"""
from mech_kernel import (
    MechKernel, InvalidRequestError, make_geometry_failure, make_recoverable,
    GeometryFailureReason
)


def main():
    print("=" * 60)
    print("MechKernel v1.1 Demo 2: 5 类错误分类")
    print("=" * 60)
    
    # === 类型 1: InvalidRequestError（编程错误，必抛） ===
    print("\n[1] InvalidRequestError（编程错误，必抛异常）")
    k = MechKernel()
    try:
        k.create_workplane("", "XY")
    except InvalidRequestError as e:
        print(f"  ✓ 正确抛出: {e}")
    
    try:
        k.create_workplane("base", "INVALID_TYPE")
    except InvalidRequestError as e:
        print(f"  ✓ 正确抛出: {e}")
    
    # === 类型 2: GEOMETRY_FAILURE（预期内失败，StepResult 表达） ===
    print("\n[2] GEOMETRY_FAILURE（预期内失败，返回 StepResult）")
    failure = make_geometry_failure(
        GeometryFailureReason.SELF_INTERSECTION,
        "草图自相交"
    )
    print(f"  ✓ error_kind = {failure['error_kind']}")
    print(f"  ✓ error = {failure['error']}")
    
    # === 类型 3: RECOVERABLE（可修复，返回 StepResult + suggestion） ===
    print("\n[3] RECOVERABLE（可修复，带建议值）")
    recoverable = make_recoverable(
        GeometryFailureReason.FILLET_TOO_LARGE,
        suggestion={
            "operation": "fillet",
            "new_radius": 2.0,
            "original_radius": 5.0,
            "reason": "R5 超过边长 1/10，建议降到 R2",
        },
        detail="R5 过大"
    )
    print(f"  ✓ error_kind = {recoverable['error_kind']}")
    print(f"  ✓ error = {recoverable['error']}")
    print(f"  ✓ suggestion = {recoverable['suggestion']}")
    
    # === 类型 4: KernelBugError（内部 bug） ===
    print("\n[4] KernelBugError（内部 bug，发生即记录）")
    print("  ✓ 仅在内核 invariant 违例时抛出，正常流程不触发")
    
    # === 类型 5: StateCorruptionError（状态损坏） ===
    print("\n[5] StateCorruptionError（状态损坏）")
    print("  ✓ Feature Graph 出现环、引用解析失败时抛出")
    print("  ✓ 调用方应重建状态")
    
    print("\n" + "=" * 60)
    print("✓ 5 类错误分类演示完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
