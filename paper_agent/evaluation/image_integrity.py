"""Deterministic checks for content cut off inside a captured asset bitmap."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

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


__all__ = ["CropIntegrityResult", "EdgeMeasurement", "inspect_crop_integrity"]
