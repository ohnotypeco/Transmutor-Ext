"""
Transmutor v3 — View.

Manages Merz layer drawing for the Transmutor tool: bounding box,
resize handles, stem measurement overlays, and all visual feedback
in the glyph editor.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from mojo.tools import IntersectGlyphWithLine as intersect
from mojo.UI import getDefault

from constants import (
    BOX_DASH_PATTERN,
    EXTENSION_IDENTIFIER,
    HANDLE_SIZE,
    MAX_COORDINATE,
    verbosePrint,
)


def _interpolate(a: float, b: float, v: float) -> float:
    """Linear interpolation between two values.

    Args:
        a: Start value.
        b: End value.
        v: Interpolation factor (0.0 = a, 1.0 = b).

    Returns:
        Interpolated value.
    """
    verbosePrint("_interpolate")
    return a + (b - a) * v


def _distance(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """Euclidean distance between two points.

    Args:
        p1: First point (x, y).
        p2: Second point (x, y).

    Returns:
        Distance rounded to 2 decimal places.
    """
    verbosePrint("_distance")
    return round(math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2), 2)


def _isFinite(value: float) -> bool:
    """Check that a value is finite and within safe bounds."""
    verbosePrint("_isFinite")
    return math.isfinite(value) and abs(value) < MAX_COORDINATE


def _safeBounds(bounds: Optional[Tuple[float, float, float, float]]) -> bool:
    """Validate that bounds are finite and within safe coordinate range.

    Args:
        bounds: (xMin, yMin, xMax, yMax) or None.

    Returns:
        True if bounds are safe to use for rendering.
    """
    verbosePrint("_safeBounds")
    if bounds is None:
        return False
    return all(_isFinite(v) for v in bounds)


class TransmutorView:
    """Draws Transmutor visual elements in the glyph editor.

    Manages Merz foreground/preview containers and draws:
    - Scaled glyph fill (foreground + preview)
    - Dashed bounding box
    - 8 resize handles (N, S, E, W, NE, NW, SE, SW)
    - Stem measurement intersections with distance labels
    """

    def __init__(self) -> None:
        verbosePrint("TransmutorView::__init__")
        self._foregroundContainer: Optional[Any] = None
        self._previewContainer: Optional[Any] = None

    @property
    def scaledGlyphColor(self) -> Tuple[float, ...]:
        """Color for the scaled glyph overlay (from RoboFont preferences)."""
        return getDefault("glyphViewTransformationColor")

    @property
    def previewColor(self) -> Tuple[float, ...]:
        """Color for the preview fill (from RoboFont preferences)."""
        return getDefault("glyphViewPreviewFillColor")

    @property
    def textColor(self) -> Tuple[float, ...]:
        """Color for measurement text (from RoboFont preferences)."""
        return getDefault("glyphViewMeasurementsTextColor")

    # ──────────────────────────────────────────────
    #  Container lifecycle
    # ──────────────────────────────────────────────

    def setupContainers(self, glyphEditor: Any) -> None:
        """Initialize Merz extension containers from a glyph editor.

        Args:
            glyphEditor: The RoboFont glyph editor window.
        """
        verbosePrint("TransmutorView::setupContainers")
        self._foregroundContainer = glyphEditor.extensionContainer(
            identifier=EXTENSION_IDENTIFIER + ".foreground",
            location="foreground",
            clear=True,
        )
        self._previewContainer = glyphEditor.extensionContainer(
            identifier=EXTENSION_IDENTIFIER + ".preview",
            location="preview",
            clear=True,
        )
        self._foregroundContainer.setVisible(True)
        self._previewContainer.setVisible(True)
        verbosePrint("TransmutorView: containers set up")

    def teardownContainers(self) -> None:
        """Clean up and hide Merz containers."""
        verbosePrint("TransmutorView::teardownContainers")
        if self._foregroundContainer is not None:
            self._foregroundContainer.setVisible(False)
            self._foregroundContainer.clearSublayers()
        if self._previewContainer is not None:
            self._previewContainer.setVisible(False)
            self._previewContainer.clearSublayers()
        self._foregroundContainer = None
        self._previewContainer = None
        verbosePrint("TransmutorView: containers torn down")

    def clearAll(self) -> None:
        """Clear all sublayers without tearing down containers."""
        verbosePrint("TransmutorView::clearAll")
        if self._foregroundContainer is not None:
            self._foregroundContainer.clearSublayers()
        if self._previewContainer is not None:
            self._previewContainer.clearSublayers()

    @property
    def hasContainers(self) -> bool:
        """True if containers are initialized."""
        return self._foregroundContainer is not None

    # ──────────────────────────────────────────────
    #  Block drawing
    # ──────────────────────────────────────────────

    def drawBlock(
        self,
        bounds: Tuple[float, float, float, float],
        scaledGlyph: Optional[Any] = None,
        offset: Tuple[float, float] = (0, 0),
        measurements: Optional[List[Any]] = None,
    ) -> None:
        """Draw the full visual representation of a Transmutor block.

        Draws the scaled glyph fill, bounding box, resize handles,
        and optional measurement intersections.

        Args:
            bounds: (xMin, yMin, xMax, yMax) of the block.
            scaledGlyph: Optional RGlyph to draw fill from.
            offset: (x, y) translation offset.
            measurements: Optional list of measurement objects from the glyph editor.
        """
        verbosePrint("TransmutorView::drawBlock")
        self.clearAll()

        if not _safeBounds(bounds):
            verbosePrint("drawBlock: unsafe bounds, skipping draw")
            return

        if self._foregroundContainer is None or self._previewContainer is None:
            return

        offsetX, offsetY = offset
        xMin, yMin, xMax, yMax = bounds

        # Draw scaled glyph fill
        if scaledGlyph is not None:
            self._drawGlyphFill(scaledGlyph, offsetX, offsetY)

        # Draw bounding box
        self._drawBoundingBox(xMin, yMin, xMax, yMax, offsetX, offsetY)

        # Draw resize handles
        self._drawHandles(xMin, yMin, xMax, yMax, offsetX, offsetY)

        # Draw measurements
        if measurements is not None and scaledGlyph is not None:
            self._drawMeasurements(scaledGlyph, measurements, offsetX, offsetY)

    def drawBlockHighlight(
        self,
        bounds: Tuple[float, float, float, float],
    ) -> None:
        """Draw a lightweight highlight around a block (for hover state).

        Args:
            bounds: (xMin, yMin, xMax, yMax) of the block.
        """
        verbosePrint("TransmutorView::drawBlockHighlight")
        self.clearAll()

        if not _safeBounds(bounds) or self._foregroundContainer is None:
            return

        xMin, yMin, xMax, yMax = bounds
        self._drawBoundingBox(xMin, yMin, xMax, yMax, 0, 0)
        self._drawHandles(xMin, yMin, xMax, yMax, 0, 0)

    # ──────────────────────────────────────────────
    #  Private drawing methods
    # ──────────────────────────────────────────────

    def _drawGlyphFill(self, glyph: Any, offsetX: float, offsetY: float) -> None:
        """Draw the scaled glyph as a filled path in foreground and preview.

        Args:
            glyph: The RGlyph to draw.
            offsetX: Horizontal offset.
            offsetY: Vertical offset.
        """
        verbosePrint("TransmutorView::_drawGlyphFill")
        # Foreground fill
        fgLayer = self._foregroundContainer.appendPathSublayer(
            fillColor=self.scaledGlyphColor,
            strokeColor=None,
            opacity=0.5,
        )
        pen = fgLayer.getPen()
        glyph.draw(pen)
        fgLayer.addTranslationTransformation((offsetX, offsetY))

        # Preview fill
        pvLayer = self._previewContainer.appendPathSublayer(
            fillColor=self.previewColor,
            strokeColor=None,
            opacity=1,
        )
        pen = pvLayer.getPen()
        glyph.draw(pen)
        pvLayer.addTranslationTransformation((offsetX, offsetY))

    def _drawBoundingBox(
        self,
        xMin: float, yMin: float, xMax: float, yMax: float,
        offsetX: float, offsetY: float,
    ) -> None:
        """Draw a dashed bounding box rectangle.

        Args:
            xMin, yMin, xMax, yMax: Bounding box coordinates.
            offsetX, offsetY: Translation offset.
        """
        verbosePrint("TransmutorView::_drawBoundingBox")
        boxLayer = self._foregroundContainer.appendPathSublayer(
            fillColor=None,
            strokeColor=self.scaledGlyphColor,
            strokeWidth=1,
            name="box",
        )
        pen = boxLayer.getPen()
        pen.moveTo((xMin, yMin))
        pen.lineTo((xMax, yMin))
        pen.lineTo((xMax, yMax))
        pen.lineTo((xMin, yMax))
        pen.closePath()
        boxLayer.setStrokeDash(BOX_DASH_PATTERN)
        boxLayer.addTranslationTransformation((offsetX, offsetY))

    def _drawHandles(
        self,
        xMin: float, yMin: float, xMax: float, yMax: float,
        offsetX: float, offsetY: float,
    ) -> None:
        """Draw 8 resize handles around the bounding box.

        Args:
            xMin, yMin, xMax, yMax: Bounding box coordinates.
            offsetX, offsetY: Translation offset.
        """
        verbosePrint("TransmutorView::_drawHandles")
        xMid = _interpolate(xMin, xMax, 0.5)
        yMid = _interpolate(yMin, yMax, 0.5)

        handlePositions = [
            (xMin, yMin),   # SW
            (xMid, yMin),   # S
            (xMax, yMin),   # SE
            (xMax, yMid),   # E
            (xMax, yMax),   # NE
            (xMid, yMax),   # N
            (xMin, yMax),   # NW
            (xMin, yMid),   # W
        ]

        for (hx, hy) in handlePositions:
            handle = self._foregroundContainer.appendSymbolSublayer(
                position=(hx, hy),
                imageSettings=dict(
                    name="rectangle",
                    size=(HANDLE_SIZE, HANDLE_SIZE),
                    fillColor=self.scaledGlyphColor,
                ),
            )
            handle.addTranslationTransformation((offsetX, offsetY))

    def _drawMeasurements(
        self,
        scaledGlyph: Any,
        measurements: List[Any],
        offsetX: float,
        offsetY: float,
    ) -> None:
        """Draw stem measurement intersections and distance labels.

        Args:
            scaledGlyph: The scaled glyph to intersect against.
            measurements: List of measurement objects from the glyph editor.
            offsetX, offsetY: Offset to account for when computing intersections.
        """
        verbosePrint("TransmutorView::_drawMeasurements")
        for m in measurements:
            if not hasattr(m, "startPoint") or not hasattr(m, "endPoint"):
                continue
            if m.startPoint is None or m.endPoint is None:
                continue

            sx, sy = m.startPoint
            ex, ey = m.endPoint

            # Adjust measurement line to glyph's local space
            sx -= offsetX
            sy -= offsetY
            ex -= offsetX
            ey -= offsetY

            line = (sx, sy), (ex, ey)

            try:
                intersections = sorted(intersect(scaledGlyph, line))
            except Exception:
                continue

            # Process intersection pairs
            pairs = [intersections[i:i + 2] for i in range(len(intersections) - 1)]

            for coords in pairs:
                if len(coords) == 2:
                    front = (coords[0][0] + offsetX, coords[0][1] + offsetY)
                    back = (coords[1][0] + offsetX, coords[1][1] + offsetY)

                    # Draw intersection dots
                    dotSize = HANDLE_SIZE * 0.5
                    self._foregroundContainer.appendSymbolSublayer(
                        position=front,
                        imageSettings=dict(
                            name="oval",
                            size=(dotSize, dotSize),
                            fillColor=self.scaledGlyphColor,
                        ),
                    )
                    self._foregroundContainer.appendSymbolSublayer(
                        position=back,
                        imageSettings=dict(
                            name="oval",
                            size=(dotSize, dotSize),
                            fillColor=self.scaledGlyphColor,
                        ),
                    )

                    # Draw distance label
                    xM = _interpolate(front[0], back[0], 0.5)
                    yM = _interpolate(front[1], back[1], 0.5)
                    dist = _distance(front, back)

                    self._foregroundContainer.appendTextLineSublayer(
                        position=(xM, yM),
                        size=(20, 20),
                        pointSize=8,
                        weight="bold",
                        text=f"{dist}",
                        fillColor=self.textColor,
                        horizontalAlignment="center",
                        verticalAlignment="center",
                    )


# ──────────────────────────────────────────────────
#  Handle hit-testing utilities
# ──────────────────────────────────────────────────

# Handle identifiers and their (originX, originY) for transform origin
# When you drag a handle, the opposite side becomes the transform origin
HANDLE_MAP: Dict[str, Dict[str, Tuple[float, float]]] = {
    "sw": {"corner": (0.0, 0.0), "origin": (1.0, 1.0)},
    "s":  {"corner": (0.5, 0.0), "origin": (0.5, 1.0)},
    "se": {"corner": (1.0, 0.0), "origin": (0.0, 1.0)},
    "e":  {"corner": (1.0, 0.5), "origin": (0.0, 0.5)},
    "ne": {"corner": (1.0, 1.0), "origin": (0.0, 0.0)},
    "n":  {"corner": (0.5, 1.0), "origin": (0.5, 0.0)},
    "nw": {"corner": (0.0, 1.0), "origin": (1.0, 0.0)},
    "w":  {"corner": (0.0, 0.5), "origin": (1.0, 0.5)},
}


def hitTestHandles(
    point: Tuple[float, float],
    bounds: Tuple[float, float, float, float],
    viewScale: float = 1.0,
) -> Optional[str]:
    """Test if a point hits one of the 8 bounding box handles.

    Args:
        point: The (x, y) click position.
        bounds: (xMin, yMin, xMax, yMax) of the block.
        viewScale: Current glyph view scale (for hit radius adjustment).

    Returns:
        Handle key ('sw', 's', 'se', 'e', 'ne', 'n', 'nw', 'w') or None.
    """
    verbosePrint("hitTestHandles")
    if not _safeBounds(bounds):
        return None

    xMin, yMin, xMax, yMax = bounds
    hitRadius = HANDLE_SIZE * (1 / max(viewScale, 0.001))
    px, py = point

    handlePositions = {
        "sw": (xMin, yMin),
        "s":  (_interpolate(xMin, xMax, 0.5), yMin),
        "se": (xMax, yMin),
        "e":  (xMax, _interpolate(yMin, yMax, 0.5)),
        "ne": (xMax, yMax),
        "n":  (_interpolate(xMin, xMax, 0.5), yMax),
        "nw": (xMin, yMax),
        "w":  (xMin, _interpolate(yMin, yMax, 0.5)),
    }

    for key, (hx, hy) in handlePositions.items():
        if (hx - hitRadius <= px <= hx + hitRadius and
                hy - hitRadius <= py <= hy + hitRadius):
            return key

    return None


def hitTestBounds(
    point: Tuple[float, float],
    bounds: Tuple[float, float, float, float],
) -> bool:
    """Test if a point is inside a bounding box.

    Args:
        point: The (x, y) position to test.
        bounds: (xMin, yMin, xMax, yMax).

    Returns:
        True if the point is inside the bounds.
    """
    verbosePrint("hitTestBounds")
    if not _safeBounds(bounds):
        return False

    xMin, yMin, xMax, yMax = bounds
    px, py = point
    return xMin <= px <= xMax and yMin <= py <= yMax
