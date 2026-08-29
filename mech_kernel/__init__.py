"""
MechKernel v1.1 - AI-native 建模内核

基于 build123d，提供 18 个原子 API + 自适应渲染 + 语义引用 + 事务回滚
"""
from ._runtime_compat import ensure_build123d_import

ensure_build123d_import()

from .kernel import MechKernel
from .step_result import StepResult, GeometrySummary, make_success, make_failure
from .features import (
    FeatureType, FeatureState, ConstraintStatus, FeatureNode, Sketch, SketchEntity, Constraint, Reference
)
from .feature_graph import FeatureGraph
from .workplane import Workplane, WorkplaneType, WorkplaneRegistry
from .persistent_naming import PersistentNamingResolver, PersistentName
from .transaction import Transaction, transaction
from .errors import (
    MechKernelError, InvalidRequestError, KernelBugError, StateCorruptionError,
    GeometryValidationError, DeprecatedInternalAPIError,
    make_geometry_failure, make_recoverable, GeometryFailureReason
)

__version__ = "2.6.0"
__all__ = [
    "MechKernel",
    "StepResult", "GeometrySummary", "make_success", "make_failure",
    "FeatureType", "FeatureState", "ConstraintStatus", "FeatureNode", "Sketch", "SketchEntity", "Constraint", "Reference",
    "FeatureGraph",
    "Workplane", "WorkplaneType", "WorkplaneRegistry",
    "PersistentNamingResolver", "PersistentName",
    "Transaction", "transaction",
    "MechKernelError", "InvalidRequestError", "KernelBugError", "StateCorruptionError", "GeometryValidationError",
    "DeprecatedInternalAPIError",
    "make_geometry_failure", "make_recoverable", "GeometryFailureReason",
]
