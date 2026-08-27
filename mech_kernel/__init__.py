"""
MechKernel v1.1 - AI-native 建模内核

基于 build123d，提供 18 个原子 API + 自适应渲染 + 语义引用 + 事务回滚
"""
from .kernel import MechKernel
from .step_result import StepResult, GeometrySummary, make_success, make_failure
from .features import (
    FeatureType, FeatureState, FeatureNode, Sketch, SketchEntity, Reference
)
from .feature_graph import FeatureGraph
from .workplane import Workplane, WorkplaneType, WorkplaneRegistry
from .persistent_naming import PersistentNamingResolver, PersistentName
from .transaction import Transaction, transaction
from .errors import (
    MechKernelError, InvalidRequestError, KernelBugError, StateCorruptionError,
    DeprecatedInternalAPIError,
    make_geometry_failure, make_recoverable, GeometryFailureReason
)

__version__ = "1.1.0"
__all__ = [
    "MechKernel",
    "StepResult", "GeometrySummary", "make_success", "make_failure",
    "FeatureType", "FeatureState", "FeatureNode", "Sketch", "SketchEntity", "Reference",
    "FeatureGraph",
    "Workplane", "WorkplaneType", "WorkplaneRegistry",
    "PersistentNamingResolver", "PersistentName",
    "Transaction", "transaction",
    "MechKernelError", "InvalidRequestError", "KernelBugError", "StateCorruptionError",
    "DeprecatedInternalAPIError",
    "make_geometry_failure", "make_recoverable", "GeometryFailureReason",
]
