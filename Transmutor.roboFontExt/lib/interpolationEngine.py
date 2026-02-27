"""
Transmutor v3 — Interpolation engine.

Wraps MutatorMath directly to provide scaled, interpolated glyphs
from multiple font masters. Replaces the MutatorScale submodule
with a safer, self-contained implementation.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from fontMath.mathGlyph import MathGlyph
from fontParts.fontshell import RGlyph
from mutatorMath.objects.location import Location
from mutatorMath.objects.mutator import buildMutator

from constants import (
    MIN_SCALE,
    MAX_SCALE,
    MAX_COORDINATE,
    verbosePrint,
)
from stemMeasurer import StemMeasurer, makeFontDisplayName


class MasterFont:
    """Wraps a font master with its measured stem values.

    Attributes:
        font: The RoboFont/FontParts font object.
        name: Display name for this master.
        vStem: Vertical stem width (from I glyph).
        hStem: Horizontal stem width (from H glyph).
    """

    def __init__(
        self,
        font: Any,
        vStem: Optional[int] = None,
        hStem: Optional[int] = None,
    ) -> None:
        verbosePrint("MasterFont::__init__")
        self.font = font
        self.name: str = makeFontDisplayName(font)
        self.vStem: Optional[int] = vStem
        self.hStem: Optional[int] = hStem

    def __repr__(self) -> str:
        return f"<MasterFont {self.name} v:{self.vStem} h:{self.hStem}>"

    def hasGlyph(self, glyphName: str) -> bool:
        """Check if this master contains a glyph by name."""
        verbosePrint("MasterFont::hasGlyph")
        return glyphName in self.font

    def getGlyph(self, glyphName: str) -> Optional[MathGlyph]:
        """Return a MathGlyph copy of the named glyph, or None.

        Args:
            glyphName: The glyph to retrieve.

        Returns:
            A MathGlyph instance, or None if not found.
        """
        verbosePrint("MasterFont::getGlyph")
        if glyphName not in self.font:
            return None
        try:
            source = self.font[glyphName]
            # Draw only contours into a clean glyph — skip anchors and
            # components (anchors break MathGlyph iteration; components
            # require recursive resolution that isn't needed here).
            clean = RGlyph()
            clean.width = source.width
            clean.name = source.name
            pen = clean.getPen()
            for contour in source.contours:
                contour.draw(pen)
            return clean.toMathGlyph()
        except Exception as e:
            verbosePrint(f"Failed to create MathGlyph for {glyphName} in {self.name}: {e}")
            return None


class InterpolationEngine:
    """Produces scaled, interpolated glyphs from multiple font masters.

    The engine:
    1. Accepts font masters with stem values.
    2. For a given glyph + scale + target stems, scales each master's glyph
       independently.
    3. Builds a MutatorMath interpolation space keyed by scaled stem values.
    4. Returns the interpolated result at the target stem location.

    This replaces the MutatorScale submodule with full control over
    all scaling math and comprehensive safety bounds.
    """

    def __init__(self) -> None:
        verbosePrint("InterpolationEngine::__init__")
        self._masters: List[MasterFont] = []
        self._stemMeasurer: StemMeasurer = StemMeasurer()
        self._workingStems: Optional[str] = None

    @property
    def masters(self) -> List[MasterFont]:
        """The list of current master fonts."""
        return self._masters

    @property
    def hasMasters(self) -> bool:
        """True if at least one master is loaded."""
        return len(self._masters) > 0

    def setMasters(self, fonts: List[Any]) -> None:
        """Set the master fonts, measuring stems automatically.

        Args:
            fonts: List of RoboFont/FontParts font objects.
        """
        verbosePrint("InterpolationEngine::setMasters")
        self._masters = []
        for font in fonts:
            vStem, hStem = self._stemMeasurer.getStems(font)
            master = MasterFont(font, vStem=vStem, hStem=hStem)
            self._masters.append(master)
        self._determineWorkingStems()
        verbosePrint(f"InterpolationEngine: set {len(self._masters)} masters, stems={self._workingStems}")

    def clearMasters(self) -> None:
        """Remove all masters."""
        verbosePrint("InterpolationEngine::clearMasters")
        self._masters = []
        self._workingStems = None

    def clearStemCache(self) -> None:
        """Clear the stem measurement cache."""
        verbosePrint("InterpolationEngine::clearStemCache")
        self._stemMeasurer.clearCache()

    def hasGlyph(self, glyphName: str) -> bool:
        """Check if all masters contain the named glyph.

        Args:
            glyphName: The glyph name to check.

        Returns:
            True if all masters have this glyph.
        """
        verbosePrint("InterpolationEngine::hasGlyph")
        if not self._masters:
            return False
        return all(m.hasGlyph(glyphName) for m in self._masters)

    def getScaledGlyph(
        self,
        glyphName: str,
        scaleH: float,
        scaleV: float,
        stemRatioV: float = 1.0,
        stemRatioH: float = 1.0,
        referenceHeight: Optional[float] = None,
    ) -> Optional[RGlyph]:
        """Return a scaled, interpolated glyph.

        Args:
            glyphName: Name of the glyph to scale.
            scaleH: Horizontal scale factor.
            scaleV: Vertical scale factor.
            stemRatioV: Vertical stem weight ratio multiplier.
            stemRatioH: Horizontal stem weight ratio multiplier.
            referenceHeight: The UPM or reference height for scaling.
                If None, uses the first master's UPM.

        Returns:
            An RGlyph with the scaled/interpolated outlines, or None on failure.
        """
        verbosePrint("InterpolationEngine::getScaledGlyph")
        if not self._masters:
            verbosePrint("getScaledGlyph: no masters")
            return None

        if not self.hasGlyph(glyphName):
            verbosePrint(f"getScaledGlyph: glyph '{glyphName}' not in all masters")
            return None

        # Clamp scale values
        scaleH = _clampScale(scaleH)
        scaleV = _clampScale(scaleV)

        if referenceHeight is None:
            referenceHeight = self._masters[0].font.info.unitsPerEm or 1000

        if referenceHeight == 0:
            referenceHeight = 1000

        # Compute target stem values from the first available master
        targetVStem, targetHStem = self._getTargetStems(stemRatioV, stemRatioH)
        if targetVStem is None and targetHStem is None:
            verbosePrint("getScaledGlyph: no valid stem values")
            return None

        # Build scaled masters and interpolation space
        try:
            result = self._buildAndInterpolate(
                glyphName=glyphName,
                scaleH=scaleH,
                scaleV=scaleV,
                targetVStem=targetVStem,
                targetHStem=targetHStem,
                referenceHeight=referenceHeight,
            )
        except Exception as e:
            verbosePrint(f"getScaledGlyph: interpolation failed: {e}")
            return None

        if result is None:
            return None

        # Validate output bounds
        if not _validateGlyphBounds(result):
            verbosePrint("getScaledGlyph: output bounds exceeded maximum")
            return None

        return result

    def _getTargetStems(
        self,
        stemRatioV: float,
        stemRatioH: float,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Compute target stem values by applying ratios to the first master's stems.

        Args:
            stemRatioV: Multiplier for vertical stem.
            stemRatioH: Multiplier for horizontal stem.

        Returns:
            (targetVStem, targetHStem) tuple.
        """
        verbosePrint("InterpolationEngine::_getTargetStems")
        # Find the first master with valid stems
        for master in self._masters:
            if master.vStem is not None and master.hStem is not None:
                return (master.vStem * stemRatioV, master.hStem * stemRatioH)

        # Fallback: use whatever stems are available
        for master in self._masters:
            vStem = master.vStem * stemRatioV if master.vStem is not None else None
            hStem = master.hStem * stemRatioH if master.hStem is not None else None
            if vStem is not None or hStem is not None:
                return (vStem, hStem)

        return (None, None)

    def _buildAndInterpolate(
        self,
        glyphName: str,
        scaleH: float,
        scaleV: float,
        targetVStem: Optional[float],
        targetHStem: Optional[float],
        referenceHeight: float,
    ) -> Optional[RGlyph]:
        """Scale each master's glyph and interpolate at target stems.

        Args:
            glyphName: Glyph to process.
            scaleH: Horizontal scale factor.
            scaleV: Vertical scale factor.
            targetVStem: Target vertical stem width.
            targetHStem: Target horizontal stem width.
            referenceHeight: Reference height for computing scale ratios.

        Returns:
            Interpolated RGlyph or None.
        """
        verbosePrint("InterpolationEngine::_buildAndInterpolate")
        workingStems = self._workingStems
        mutatorMasters = []

        # Calculate actual x/y scale factors
        # scaleV determines the y scale (targetHeight / referenceHeight)
        # scaleH adjusts the x scale relative to y (width ratio)
        yScale = scaleV
        xScale = scaleH

        if len(self._masters) == 1 or workingStems is None:
            # Single master: just scale directly, no interpolation
            master = self._masters[0]
            mathGlyph = master.getGlyph(glyphName)
            if mathGlyph is None:
                return None

            # Apply scale: multiply by (xScale, yScale)
            scaledGlyph = mathGlyph * (xScale, yScale)
            scaledGlyph.round()

            result = RGlyph()
            result.fromMathGlyph(scaledGlyph)
            return result

        # Multiple masters: build interpolation space
        for master in self._masters:
            vStem = master.vStem
            hStem = master.hStem

            # Only require the stems relevant to the working axis
            if workingStems == "both" and (vStem is None or hStem is None):
                continue
            elif workingStems == "vstem" and vStem is None:
                continue
            elif workingStems == "hstem" and hStem is None:
                continue
            elif vStem is None and hStem is None:
                continue

            mathGlyph = master.getGlyph(glyphName)
            if mathGlyph is None:
                continue

            # Scale the master glyph
            scaledMasterGlyph = mathGlyph * (xScale, yScale)

            # Place in interpolation space using scaled stem values
            if workingStems == "both":
                axis = {
                    "vstem": vStem * xScale,
                    "hstem": hStem * yScale,
                }
            elif workingStems == "vstem":
                axis = {"stem": vStem * xScale}
            elif workingStems == "hstem":
                axis = {"stem": hStem * yScale}
            else:
                axis = {"stem": vStem * xScale}

            mutatorMasters.append((Location(**axis), scaledMasterGlyph))

        if not mutatorMasters:
            verbosePrint("_buildAndInterpolate: no valid masters for interpolation")
            return None

        # Build target location
        targetLocation = self._buildTargetLocation(
            targetVStem=targetVStem,
            targetHStem=targetHStem,
            xScale=xScale,
            yScale=yScale,
            workingStems=workingStems,
        )

        # Build mutator and get instance
        try:
            bias, mutator = buildMutator(mutatorMasters)
            if mutator is None:
                verbosePrint("_buildAndInterpolate: buildMutator returned None")
                return None
            instance = mutator.makeInstance(targetLocation)
        except Exception as e:
            verbosePrint(f"_buildAndInterpolate: MutatorMath error: {e}")
            return None

        if instance is None:
            return None

        # Extract to RGlyph
        try:
            result = RGlyph()
            result.fromMathGlyph(instance)
            result.round()
            return result
        except Exception as e:
            verbosePrint(f"_buildAndInterpolate: extraction failed: {e}")
            return None

    def _buildTargetLocation(
        self,
        targetVStem: Optional[float],
        targetHStem: Optional[float],
        xScale: float,
        yScale: float,
        workingStems: Optional[str],
    ) -> Location:
        """Build a MutatorMath Location for the target stem values.

        Args:
            targetVStem: Target vertical stem width.
            targetHStem: Target horizontal stem width.
            xScale: Current horizontal scale.
            yScale: Current vertical scale.
            workingStems: The stem mode ('both', 'vstem', 'hstem').

        Returns:
            A MutatorMath Location object.
        """
        verbosePrint("InterpolationEngine::_buildTargetLocation")
        if workingStems == "both" and targetVStem is not None and targetHStem is not None:
            return Location(vstem=targetVStem, hstem=targetHStem)

        elif workingStems == "vstem":
            if targetVStem is not None and targetHStem is not None:
                # Map hStem to vStem space for single-axis interpolation
                vStems = [m.vStem * xScale for m in self._masters if m.vStem is not None]
                hStems = [m.hStem * yScale for m in self._masters if m.hStem is not None]
                if len(vStems) >= 2 and len(hStems) >= 2:
                    minV, maxV = min(vStems), max(vStems)
                    minH = hStems[vStems.index(minV)] if minV in vStems else hStems[0]
                    maxH = hStems[vStems.index(maxV)] if maxV in vStems else hStems[-1]
                    if maxH != minH:
                        mappedH = _mapValue(targetHStem, (minH, maxH), (minV, maxV))
                        return Location(stem=(targetVStem, mappedH))
            return Location(stem=targetVStem)

        elif workingStems == "hstem":
            return Location(stem=targetHStem if targetHStem is not None else 0)

        # Fallback
        return Location(stem=targetVStem if targetVStem is not None else 0)

    def _determineWorkingStems(self) -> None:
        """Determine the interpolation axis mode from available masters.

        Sets self._workingStems to 'both', 'vstem', 'hstem', or None.
        """
        verbosePrint("InterpolationEngine::_determineWorkingStems")
        if len(self._masters) < 2:
            self._workingStems = None
            return

        vStems = [m.vStem for m in self._masters if m.vStem is not None]
        hStems = [m.hStem for m in self._masters if m.hStem is not None]

        if len(self._masters) > 2:
            # Check for two-axis capability
            vHasDiff = _hasDifferential(vStems)
            hHasSplitDiff = _hasSplitDifferential(hStems)
            if vHasDiff and hHasSplitDiff:
                self._workingStems = "both"
                return

        # Single axis: prefer vstem
        if _hasDifferential(vStems):
            self._workingStems = "vstem"
        elif _hasDifferential(hStems):
            self._workingStems = "hstem"
        else:
            self._workingStems = None


def _clampScale(value: float) -> float:
    """Clamp a scale value to safe bounds.

    Args:
        value: The scale value to clamp.

    Returns:
        The clamped scale value.
    """
    verbosePrint("_clampScale")
    return max(MIN_SCALE, min(MAX_SCALE, value))


def _validateGlyphBounds(glyph: RGlyph) -> bool:
    """Check that a glyph's coordinates are within safe bounds.

    Args:
        glyph: The glyph to validate.

    Returns:
        True if all coordinates are within MAX_COORDINATE, or glyph has no bounds.
    """
    verbosePrint("_validateGlyphBounds")
    bounds = glyph.bounds
    if bounds is None:
        return True  # Empty glyph is fine

    for val in bounds:
        if not math.isfinite(val):
            return False
        if abs(val) > MAX_COORDINATE:
            return False
    return True


def _mapValue(
    value: float,
    sourceRange: Tuple[float, float],
    targetRange: Tuple[float, float],
) -> float:
    """Map a value from one range to another.

    Args:
        value: The value to map.
        sourceRange: (min, max) of the source range.
        targetRange: (min, max) of the target range.

    Returns:
        The mapped value.
    """
    verbosePrint("_mapValue")
    srcMin, srcMax = sourceRange
    tgtMin, tgtMax = targetRange
    if srcMax == srcMin:
        return tgtMin
    normalized = (value - srcMin) / (srcMax - srcMin)
    return tgtMin + normalized * (tgtMax - tgtMin)


def _hasDifferential(values: List[Optional[float]]) -> bool:
    """Check if a list contains at least two different non-None values.

    Args:
        values: List of numeric values (may contain None).

    Returns:
        True if there's at least one differential.
    """
    verbosePrint("_hasDifferential")
    cleanValues = sorted(v for v in values if v is not None)
    if len(cleanValues) < 2:
        return False
    return cleanValues[0] != cleanValues[-1]


def _hasSplitDifferential(values: List[Optional[float]]) -> bool:
    """Check for at least two identical + one different value (two-axis requirement).

    Args:
        values: List of numeric values (may contain None).

    Returns:
        True if the split differential condition is met.
    """
    verbosePrint("_hasSplitDifferential")
    cleanValues = sorted(v for v in values if v is not None)
    if len(cleanValues) < 3:
        return False

    hasIdentical = False
    hasDifferent = False
    for i in range(len(cleanValues) - 1):
        if cleanValues[i] == cleanValues[i + 1]:
            hasIdentical = True
        else:
            hasDifferent = True
    return hasIdentical and hasDifferent
