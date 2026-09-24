"""Deterministic checks for content cut off inside a captured asset bitmap."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image


@dataclass(frozen=True)
class EdgeMeasurement:
    side: str
    ink_density: float
    boundary_density: float
    edge_coverage: float
    depth_coverage: float
    transition_density: float
    line_like: bool
    clipped: bool

    def to_dict(self) -> dict[str, float | str | bool]:
        return asdict(self)


@dataclass(frozen=True)
class CropIntegrityResult:
    width: int
    height: int
    edges: tuple[EdgeMeasurement, ...]

    @property
    def clipped_sides(self) -> tuple[str, ...]:
        return tuple(edge.side for edge in self.edges if edge.clipped)

    @property
    def clipped(self) -> bool:
        return bool(self.clipped_sides)

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "clipped": self.clipped,
            "clipped_sides": list(self.clipped_sides),
            "edges": [edge.to_dict() for edge in self.edges],
        }


def inspect_crop_integrity(path: Path, *, kind: str = "") -> CropIntegrityResult | None:
    """Measure whether meaningful dark content continues through a crop edge.

    A blocking edge needs several independent pixel signals: ink in the outer
    strip, ink at the bitmap boundary, coverage along the edge, and ink across
    multiple strip depths.  A single straight table rule is classified as
    line-like and does not count as truncation.
    """

    if kind == "formula":
        return None
    try:
        with Image.open(path) as source:
            image = source.convert("L")
    except (OSError, ValueError):
        return None
    width, height = image.size
    if width < 80 or height < 60:
        return CropIntegrityResult(width, height, ())

    pixels = image.load()
    strip = max(4, min(16, round(min(width, height) * 0.015)))

    def dark(x: int, y: int) -> bool:
        return pixels[x, y] < 235

    measurements: list[EdgeMeasurement] = []
    for side in ("left", "right", "top", "bottom"):
        vertical = side in {"left", "right"}
        if vertical:
            layers = list(range(strip)) if side == "left" else list(range(width - strip, width))
            boundary_layers = list(range(2)) if side == "left" else list(range(width - 2, width))
            extent = height
            layer_density = [sum(dark(layer, y) for y in range(height)) / height for layer in layers]
            ink_density = sum(sum(dark(x, y) for y in range(height)) for x in layers) / (strip * height)
            boundary_density = sum(sum(dark(x, y) for y in range(height)) for x in boundary_layers) / (2 * height)
            edge_coverage = sum(any(dark(x, y) for x in layers) for y in range(height)) / height
            boundary_mask = [dark(boundary_layers[0], y) for y in range(height)]
        else:
            layers = list(range(strip)) if side == "top" else list(range(height - strip, height))
            boundary_layers = list(range(2)) if side == "top" else list(range(height - 2, height))
            extent = width
            layer_density = [sum(dark(x, layer) for x in range(width)) / width for layer in layers]
            ink_density = sum(sum(dark(x, y) for x in range(width)) for y in layers) / (strip * width)
            boundary_density = sum(sum(dark(x, y) for x in range(width)) for y in boundary_layers) / (2 * width)
            edge_coverage = sum(any(dark(x, y) for y in layers) for x in range(width)) / width
            boundary_mask = [dark(x, boundary_layers[0]) for x in range(width)]

        depth_coverage = sum(value >= 0.004 for value in layer_density) / len(layer_density)
        transition_density = sum(
            boundary_mask[index] != boundary_mask[index - 1]
            for index in range(1, len(boundary_mask))
        ) / max(1, len(boundary_mask) - 1)
        # A full table rule can touch the crop edge without losing cells.  It
        # is narrow in strip depth and unusually continuous along the edge.
        line_like = edge_coverage >= 0.72 and depth_coverage <= 0.34 and ink_density <= 0.18
        clipped = (
            not line_like
            and ink_density >= 0.03
            and boundary_density >= 0.01
            and edge_coverage >= 0.05
            and depth_coverage >= 0.5
            and transition_density >= 0.01
            and extent >= 80
        )
        measurements.append(
            EdgeMeasurement(
                side,
                round(ink_density, 6),
                round(boundary_density, 6),
                round(edge_coverage, 6),
                round(depth_coverage, 6),
                round(transition_density, 6),
                line_like,
                clipped,
            )
        )
    return CropIntegrityResult(width, height, tuple(measurements))


def adjacent_table_edge_sides(
    asset: object,
    peers: Iterable[object],
    *,
    max_gap: float = 4.0,
) -> set[str]:
    """Return bitmap edges that touch a neighboring table on the same page."""

    if str(getattr(asset, "kind", "")) != "table":
        return set()
    rect = _rect_tuple(getattr(asset, "rect", None))
    if rect is None:
        return set()
    result: set[str] = set()
    for peer in peers:
        if peer is asset or str(getattr(peer, "kind", "")) != "table":
            continue
        if int(getattr(peer, "page_number", 0) or 0) != int(getattr(asset, "page_number", 0) or 0):
            continue
        peer_rect = _rect_tuple(getattr(peer, "rect", None))
        if peer_rect is None:
            continue
        vertical_overlap = max(0.0, min(rect[3], peer_rect[3]) - max(rect[1], peer_rect[1]))
        shorter_height = max(1.0, min(rect[3] - rect[1], peer_rect[3] - peer_rect[1]))
        if vertical_overlap / shorter_height < 0.25:
            continue
        if abs(peer_rect[0] - rect[2]) <= max_gap:
            result.add("right")
        if abs(rect[0] - peer_rect[2]) <= max_gap:
            result.add("left")
    return result


def _rect_tuple(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        return tuple(float(getattr(value, name)) for name in ("x0", "y0", "x1", "y1"))  # type: ignore[return-value]
    except (AttributeError, TypeError, ValueError):
        try:
            values = tuple(float(item) for item in value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return values if len(values) == 4 else None


__all__ = [
    "CropIntegrityResult",
    "EdgeMeasurement",
    "adjacent_table_edge_sides",
    "inspect_crop_integrity",
]
