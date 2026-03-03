"""
Transmutor v3 — Stem measurement.

Measures vertical and horizontal reference stem widths from font masters
using glyph intersection analysis on capital I and H glyphs.
Ported from MutatorScale's fontUtils with safety improvements.
"""

import math
from typing import Dict, List, Optional, Tuple

from fontParts.fontshell import RGlyph
from fontTools.pens.boundsPen import BoundsPen
from fontTools.misc import bezierTools, arrayTools, transform

from constants import (
    VSTEM_REFERENCE_GLYPH,
    HSTEM_REFERENCE_GLYPH,
    verbosePrint,
)


class StemMeasurer:
    """Measures reference stem widths from font masters.

    Auto-measures vertical stems from the capital I glyph and
    horizontal stems from the capital H glyph. Results are cached
    per font and can be invalidated when fonts change.
    """

    def __init__(self) -> None:
        verbosePrint("StemMeasurer::__init__")
        self._cache: Dict[str, Tuple[Optional[int], Optional[int]]] = {}

    def clearCache(self) -> None:
        """Clear all cached stem measurements."""
        verbosePrint("StemMeasurer::clearCache")
        self._cache.clear()

    def invalidateFont(self, fontKey: str) -> None:
        """Remove a specific font's cached measurements.

        Args:
            fontKey: The font identifier key to invalidate.
        """
        verbosePrint("StemMeasurer::invalidateFont")
        self._cache.pop(fontKey, None)

    def getStems(self, font) -> Tuple[Optional[int], Optional[int]]:
        """Return (vStem, hStem) for a font, using cache when available.

        Args:
            font: A RoboFont/FontParts font object.

        Returns:
            Tuple of (vertical stem width, horizontal stem width).
            Either value may be None if measurement fails.
        """
        verbosePrint("StemMeasurer::getStems")
        fontKey = _makeFontKey(font)
        if fontKey not in self._cache:
            self._cache[fontKey] = _measureRefStems(font)
        return self._cache[fontKey]

    def getStemsUncached(self, font) -> Tuple[Optional[int], Optional[int]]:
        """Measure stems without using or updating the cache.

        Args:
            font: A RoboFont/FontParts font object.

        Returns:
            Tuple of (vertical stem width, horizontal stem width).
        """
        verbosePrint("StemMeasurer::getStemsUncached")
        return _measureRefStems(font)


def _makeFontKey(font) -> str:
    """Create a unique identifying string for a font.

    Args:
        font: A RoboFont/FontParts font object.

    Returns:
        A string like 'FamilyName - StyleName'.
    """
    verbosePrint("_makeFontKey")
    familyName = font.info.familyName or "Unnamed"
    styleName = font.info.styleName or "Unnamed"
    return f"{familyName} - {styleName}"


def makeFontDisplayName(font) -> str:
    """Create a user-friendly display name for a font.

    Args:
        font: A RoboFont/FontParts font object.

    Returns:
        A formatted display name.
    """
    verbosePrint("makeFontDisplayName")
    return _makeFontKey(font)


def _measureRefStems(font) -> Tuple[Optional[int], Optional[int]]:
    """Measure reference stems from a font's I and H glyphs.

    Vertical stem: measured from a horizontal cut through the center
    of the capital I (thick stem).
    Horizontal stem: measured from a vertical cut through the center
    of the capital H (thin/crossbar stem).

    Args:
        font: A RoboFont/FontParts font object.

    Returns:
        Tuple of (vertical stem width, horizontal stem width).
    """
    verbosePrint("_measureRefStems")
    angle = _getSlantAngle(font)
    stems: List[Optional[int]] = []

    for i, glyphName in enumerate([VSTEM_REFERENCE_GLYPH, HSTEM_REFERENCE_GLYPH]):
        if glyphName not in font:
            stems.append(None)
            continue

        try:
            baseGlyph = font[glyphName]
            glyph = _freezeGlyph(baseGlyph)
            width = int(glyph.width) or 0

            # Correct for italic slant
            if angle:
                glyph.skewBy((-angle, 0))

            bounds = _getGlyphBounds(glyph)
            if bounds is None:
                stems.append(None)
                continue

            xMin, yMin, xMax, yMax = bounds
            xCenter = width / 2
            yCenter = (yMax - yMin) / 2

            if i == 0:
                # Capital I: horizontal cut for vertical stem — outermost
                # intersection pair gives the stem width.
                intersections = _intersectGlyph(glyph, yCenter, isHorizontal=True)
                if len(intersections) > 1:
                    intersections.sort(key=lambda p: p[0])
                    (x1, y1) = intersections[0]
                    (x2, y2) = intersections[-1]
                    stemWidth = math.hypot(x2 - x1, y2 - y1)
                    stems.append(round(stemWidth))
                else:
                    stems.append(None)
            else:
                # Capital H: vertical cut at xCenter.
                # The H outline produces 4 intersections (bottom, crossbar-bottom,
                # crossbar-top, top). The crossbar height is the minimum gap
                # between consecutive y-sorted intersections.
                intersections = _intersectGlyph(glyph, xCenter, isHorizontal=False)
                if len(intersections) >= 2:
                    intersections.sort(key=lambda p: p[1])
                    minGap: Optional[float] = None
                    for j in range(len(intersections) - 1):
                        (x1, y1) = intersections[j]
                        (x2, y2) = intersections[j + 1]
                        gap = math.hypot(x2 - x1, y2 - y1)
                        if gap > 0 and (minGap is None or gap < minGap):
                            minGap = gap
                    stems.append(round(minGap) if minGap is not None else None)
                else:
                    stems.append(None)

        except Exception as e:
            verbosePrint(f"Stem measurement failed for {glyphName} in {_makeFontKey(font)}: {e}")
            stems.append(None)

    # Apply slant correction to vertical stem
    if angle and stems[0] is not None:
        stems[0] = round(stems[0] * math.cos(math.radians(angle)))

    return (stems[0] if len(stems) > 0 else None,
            stems[1] if len(stems) > 1 else None)


def _getSlantAngle(font) -> float:
    """Measure the italic/slant angle of a font from its capital I.

    Args:
        font: A RoboFont/FontParts font object.

    Returns:
        The slant angle in degrees (0 for upright fonts).
    """
    verbosePrint("_getSlantAngle")
    if "I" not in font:
        return 0

    try:
        testGlyph = font["I"]
        bounds = _getGlyphBounds(testGlyph)
        if bounds is None:
            return 0

        xMin, yMin, xMax, yMax = bounds
        hCenter = (yMax - yMin) / 2
        delta = 10
        glyph = _freezeGlyph(testGlyph)

        intersections = []
        for i in range(2):
            horizontal = hCenter + (i * delta)
            intersections.append(_intersectGlyph(glyph, horizontal, isHorizontal=True))

        if len(intersections) > 1:
            if len(intersections[0]) > 1 and len(intersections[1]) > 1:
                (x1, y1) = intersections[0][0]
                (x2, y2) = intersections[1][0]
                angle = math.atan2(x2 - x1, y2 - y1)
                return round(math.degrees(angle), 2)
    except Exception as e:
        verbosePrint(f"Slant angle measurement failed: {e}")

    return 0


def _freezeGlyph(glyph) -> RGlyph:
    """Return a copy of a glyph with components decomposed.

    Args:
        glyph: A glyph object to copy.

    Returns:
        A new RGlyph with all components decomposed into contours.
    """
    verbosePrint("_freezeGlyph")
    result = RGlyph()
    pen = result.getPen()
    glyph.draw(pen)

    # Decompose components
    if len(glyph.components):
        _decomposeComponents(glyph, pen)

    result.width = glyph.width
    result.name = glyph.name
    return result


def _decomposeComponents(glyph, pen) -> None:
    """Decompose components of a glyph and draw them to a pen.

    Args:
        glyph: The source glyph with components.
        pen: The target pen to draw decomposed outlines to.
    """
    verbosePrint("_decomposeComponents")
    try:
        font = glyph.layer
        for comp in reversed(glyph.components):
            baseGlyphName = comp.baseGlyph
            if baseGlyphName in font:
                baseGlyph = font[baseGlyphName]
                t = transform.Transform(*comp.transformation)
                tempGlyph = RGlyph()
                tempPen = tempGlyph.getPen()
                baseGlyph.draw(tempPen)
                tempGlyph.transformBy(tuple(t))
                tempGlyph.draw(pen)
    except Exception as e:
        verbosePrint(f"Component decomposition failed: {e}")


def _getGlyphBounds(glyph) -> Optional[Tuple[float, float, float, float]]:
    """Return the bounding box of a glyph, or None if empty.

    Args:
        glyph: A glyph object.

    Returns:
        (xMin, yMin, xMax, yMax) or None.
    """
    verbosePrint("_getGlyphBounds")
    try:
        pen = BoundsPen(glyph.layer if hasattr(glyph, 'layer') else None)
        glyph.draw(pen)
        return pen.bounds
    except Exception:
        return None


def _intersectGlyph(glyph, where: float, isHorizontal: bool) -> List[Tuple[float, float]]:
    """Intersect a glyph with a horizontal or vertical line.

    Uses fontTools bezierTools to split cubic curves and lines
    at the specified position.

    Args:
        glyph: The glyph to intersect.
        where: The x or y coordinate of the intersection line.
        isHorizontal: True for horizontal line (y=where), False for vertical (x=where).

    Returns:
        List of intersection points as (x, y) tuples.
    """
    verbosePrint("_intersectGlyph")
    intersections = []

    try:
        for contour in glyph.contours:
            segments = _collectSegments(contour)
            for segment in segments:
                length = len(segment)
                returnedSegments = []

                if length == 2:
                    pt1, pt2 = segment
                    returnedSegments = _splitLine(pt1, pt2, where, int(isHorizontal))
                elif length == 4:
                    pt1, pt2, pt3, pt4 = segment
                    returnedSegments = bezierTools.splitCubic(
                        pt1, pt2, pt3, pt4, where, int(isHorizontal)
                    )

                if len(returnedSegments) > 1:
                    duplicates = _findDuplicatePoints(returnedSegments)
                    if duplicates:
                        box = _calcBounds(segment)
                        duplicates = [
                            pt for pt in duplicates
                            if arrayTools.pointInRect(pt, box)
                        ]
                        intersections.extend(duplicates)
    except Exception as e:
        verbosePrint(f"Intersection failed: {e}")

    return intersections


def _collectSegments(contour) -> List[List[Tuple[float, float]]]:
    """Extract segments from a contour as lists of point tuples.

    Each segment is [startOnCurve, ...offCurves..., endOnCurve],
    including the wraparound segment from the last on-curve back
    to the first.

    Args:
        contour: A FontParts contour object.

    Returns:
        List of segments, each segment being a list of (x, y) points.
    """
    verbosePrint("_collectSegments")
    pts = [(p.x, p.y, p.type) for p in contour.points]
    if not pts:
        return []

    n = len(pts)
    onCurves = [
        i for i, (x, y, t) in enumerate(pts)
        if t in ("line", "curve", "qcurve")
    ]
    if len(onCurves) < 2:
        return []

    segments = []
    for k in range(len(onCurves)):
        a = onCurves[k]
        b = onCurves[(k + 1) % len(onCurves)]

        # Collect from on-curve a up to and including on-curve b (wrapping)
        seg = [(pts[a][0], pts[a][1])]
        i = (a + 1) % n
        stop = (b + 1) % n
        while i != stop:
            seg.append((pts[i][0], pts[i][1]))
            i = (i + 1) % n

        segments.append(seg)

    return segments


def _splitLine(
    pt1: Tuple[float, float],
    pt2: Tuple[float, float],
    where: float,
    isHorizontal: int,
) -> List[Tuple[Tuple[float, float], ...]]:
    """Split a line segment at a horizontal or vertical position.

    Args:
        pt1: Start point of line.
        pt2: End point of line.
        where: Position to split at.
        isHorizontal: 1 for horizontal (y=where), 0 for vertical (x=where).

    Returns:
        List of line segments after splitting.
    """
    verbosePrint("_splitLine")
    pt1x, pt1y = pt1
    pt2x, pt2y = pt2

    ax = pt2x - pt1x
    ay = pt2y - pt1y

    a = (ax, ay)[isHorizontal]
    if a == 0:
        return [(pt1, pt2)]

    b = (pt1x, pt1y)[isHorizontal]
    t = float(where - b) / a

    if 0 <= t < 1:
        midPt = (ax * t + pt1x, ay * t + pt1y)
        return [(pt1, midPt), (midPt, pt2)]
    else:
        return [(pt1, pt2)]


def _calcBounds(
    points: List[Tuple[float, float]],
) -> Tuple[float, float, float, float]:
    """Calculate rectangular bounds of a list of points.

    Args:
        points: List of (x, y) coordinate tuples.

    Returns:
        (xMin, yMin, xMax, yMax) bounding box.
    """
    verbosePrint("_calcBounds")
    xMin = yMin = float("inf")
    xMax = yMax = float("-inf")

    for (x, y) in points:
        if x < xMin:
            xMin = x
        if x > xMax:
            xMax = x
        if y < yMin:
            yMin = y
        if y > yMax:
            yMax = y

    return (
        round(xMin, 4),
        round(yMin, 4),
        round(xMax, 4),
        round(yMax, 4),
    )


def _findDuplicatePoints(
    segments: List[Tuple[Tuple[float, float], ...]],
) -> List[Tuple[float, float]]:
    """Find points that appear in more than one segment (intersection points).

    Args:
        segments: List of segments, each a tuple of (x, y) points.

    Returns:
        List of duplicate (x, y) points.
    """
    verbosePrint("_findDuplicatePoints")
    counter: Dict[Tuple[float, float], int] = {}
    for seg in segments:
        for (x, y) in seg:
            p = (round(x, 4), round(y, 4))
            counter[p] = counter.get(p, 0) + 1
    return [key for key, count in counter.items() if count > 1]
