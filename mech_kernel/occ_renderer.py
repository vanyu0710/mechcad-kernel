"""Optional OpenCascade AIS/V3d renderer boundary."""

from __future__ import annotations

from typing import Any, Dict, Optional


class OCCRenderError(RuntimeError):
    """Raised when the native OCC display context cannot be created."""


class OCCRenderer:
    """Probe native AIS/V3d support without affecting kernel geometry."""

    def _context(self, size: int):
        try:
            from OCP import Aspect, OpenGl, V3d

            display = Aspect.Aspect_DisplayConnection()
            driver = OpenGl.OpenGl_GraphicDriver(display)
            viewer = V3d.V3d_Viewer(driver)
            view = viewer.CreateView()
            window = Aspect.Aspect_NeutralWindow()
            window.SetSize(size, size)
            window.SetVirtual(True)
            view.SetWindow(window)
            return viewer, view
        except Exception as exc:
            raise OCCRenderError(f"OCC 离屏上下文不可用: {exc}") from exc

    def render(
        self,
        geometry: Any,
        views: list[str],
        size: int,
        annotate: bool = True,
        section: Optional[dict] = None,
        highlight: Optional[list[str]] = None,
        scene: Any = None,
    ) -> Dict[str, bytes]:
        """Render through AIS/V3d when a platform graphics surface is available."""
        del geometry, views, annotate, section, highlight, scene
        self._context(size)
        raise OCCRenderError("OCC 原生离屏导出在当前图形驱动上不可用")
