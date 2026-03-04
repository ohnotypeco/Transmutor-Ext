"""
Transmutor
==========
Contours added by this script are tagged in public.objectLibs so they can
always be identified and re-interpolated.

Architecture (MVC, single file):
    Model:      DesignSpaceManager — owns DS paths, operator, interpolation
    View/Ctrl:  TransmutorController — EZUI panel, mediates UI ↔ model
    Tool:       TransmutorTool — BaseEventTool, mouse events, Merz drawing
    Helpers:    Block CRUD functions operate on fontParts glyphs directly
"""

import colorsys
import math
import os
import random
import site
import uuid

import ezui
from AppKit import NSMenuItem
from mojo.events import BaseEventTool, installTool
from ufoProcessor.ufoOperator import UFOOperator
from mojo.UI import CurrentGlyphWindow, GetFile

try:
    import mutatorScale
except ImportError:
    _mutatorScaleLib = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "submodules", "MutatorScale", "lib"
    )
    site.addsitedir(_mutatorScaleLib)
finally:
    from mutatorScale.objects.scaler import MutatorScaleEngine
    from mutatorScale.utilities.fontUtils import getRefStems


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_GLYPH_NAME = "A"
OBJECT_LIBS_KEY = "public.objectLibs"
LIB_PREFIX = "co.ohnotype.transmutor"
CONTOUR_SOURCE_KEY = f"{LIB_PREFIX}.source"
BLOCK_ID_KEY = f"{LIB_PREFIX}.blockID"
BLOCK_COLOR_KEY = f"{LIB_PREFIX}.color"

BLOCK_STROKE_WIDTH = 10
OUTLINE_ALPHA = 0.15
HANDLE_SIZE = 10

# Merz container IDs
_MERZ_BG_ID = "co.ohnotype.transmutor"
_MERZ_FG_ID = "co.ohnotype.transmutor.handles"

# Cardinal names for each of the 8 selection handles
_HANDLE_NAMES = ("SW", "S", "SE", "E", "NE", "N", "NW", "W")

# Per-handle scaling metadata.
# handleNorm / anchorNorm: normalised (u, v) within bounds — (0,0)=SW, (1,1)=NE.
# scaleH / scaleV: which axes the handle controls.
_HANDLE_SCALING_INFO = {
    "SW": dict(handleNorm=(0.0, 0.0), anchorNorm=(1.0, 1.0), scaleH=True,  scaleV=True),
    "S":  dict(handleNorm=(0.5, 0.0), anchorNorm=(0.5, 1.0), scaleH=False, scaleV=True),
    "SE": dict(handleNorm=(1.0, 0.0), anchorNorm=(0.0, 1.0), scaleH=True,  scaleV=True),
    "E":  dict(handleNorm=(1.0, 0.5), anchorNorm=(0.0, 0.5), scaleH=True,  scaleV=False),
    "NE": dict(handleNorm=(1.0, 1.0), anchorNorm=(0.0, 0.0), scaleH=True,  scaleV=True),
    "N":  dict(handleNorm=(0.5, 1.0), anchorNorm=(0.5, 0.0), scaleH=False, scaleV=True),
    "NW": dict(handleNorm=(0.0, 1.0), anchorNorm=(1.0, 0.0), scaleH=True,  scaleV=True),
    "W":  dict(handleNorm=(0.0, 0.5), anchorNorm=(1.0, 0.5), scaleH=True,  scaleV=False),
}


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation: a + (b - a) * t."""
    return a + (b - a) * t


def _handlePositions(bounds) -> list:
    """Return 8 (x, y) handle positions for the given bounding box.

    Order matches _HANDLE_NAMES: SW, S, SE, E, NE, N, NW, W.
    """
    xMin, yMin, xMax, yMax = bounds
    midX = (xMin + xMax) / 2
    midY = (yMin + yMax) / 2
    return [
        (xMin, yMin),   # SW
        (midX, yMin),   # S
        (xMax, yMin),   # SE
        (xMax, midY),   # E
        (xMax, yMax),   # NE
        (midX, yMax),   # N
        (xMin, yMax),   # NW
        (xMin, midY),   # W
    ]


def _unionBounds(contours) -> tuple:
    """Return (xMin, yMin, xMax, yMax) union of contour bounding boxes, or None."""
    result = None
    for contour in contours:
        b = contour.bounds
        if b is None:
            continue
        if result is None:
            result = b
        else:
            result = (
                min(result[0], b[0]),
                min(result[1], b[1]),
                max(result[2], b[2]),
                max(result[3], b[3]),
            )
    return result


def _pointInBounds(point, bounds) -> bool:
    """Return True if point is inside the given bounding box."""
    if bounds is None:
        return False
    xMin, yMin, xMax, yMax = bounds
    return xMin <= point.x <= xMax and yMin <= point.y <= yMax


def _generateBlockColor(glyph) -> list:
    """Return a bright [R, G, B] color maximally distinct from existing block colors.

    Colors are chosen in HSV space (S=0.80, V=0.90) to stay vivid and clear.
    Hue is selected by maximising minimum circular distance from existing hues.
    """
    objectLibs = glyph.lib.get(OBJECT_LIBS_KEY, {})
    existingHues = []
    for meta in objectLibs.values():
        if not isinstance(meta, dict):
            continue
        color = meta.get(BLOCK_COLOR_KEY)
        if color and len(color) >= 3:
            h, _s, _v = colorsys.rgb_to_hsv(color[0], color[1], color[2])
            existingHues.append(h)

    candidates = [i / 12.0 for i in range(12)]
    if existingHues:
        def _minCircularDist(h: float) -> float:
            return min(min(abs(h - e), 1.0 - abs(h - e)) for e in existingHues)
        hue = max(candidates, key=_minCircularDist)
        hue = (hue + random.uniform(-0.02, 0.02)) % 1.0
    else:
        hue = random.random()

    r, g, b = colorsys.hsv_to_rgb(hue, 0.80, 0.90)
    return [r, g, b]


def _normalizedToDesign(axis, normalizedValue: float) -> float:
    """Convert a normalized [-1, 1] value to design-space coordinates.

    Uses trilinear denormalization then applies the axis map.
    """
    if normalizedValue >= 0:
        userValue = axis.default + normalizedValue * (axis.maximum - axis.default)
    else:
        userValue = axis.default + normalizedValue * (axis.default - axis.minimum)
    return axis.map_forward(userValue)


def _userToNormalized(axis, userValue: float) -> float:
    """Convert a user-space axis value to the slider's normalized [0, 1] range.

    The slider maps 0 -> axis.default and 1 -> axis.maximum.
    Values below default clamp to 0; values above maximum clamp to 1.
    """
    denom = axis.maximum - axis.default
    if denom == 0:
        return 0.0
    return max(0.0, min(1.0, (userValue - axis.default) / denom))


# ---------------------------------------------------------------------------
# Block CRUD — pure glyph-level operations
# ---------------------------------------------------------------------------

def _getBlocksFromGlyph(glyph) -> dict:
    """Return {blockID: {"contours": [...], "meta": {...}}} for tagged contours."""
    objectLibs = glyph.lib.get(OBJECT_LIBS_KEY, {})
    blocks = {}
    for contour in glyph.contours:
        if not contour.identifier:
            continue
        meta = objectLibs.get(contour.identifier, {})
        if BLOCK_ID_KEY not in meta:
            continue
        blockID = meta[BLOCK_ID_KEY]
        if blockID not in blocks:
            blocks[blockID] = {"contours": [], "meta": meta}
        blocks[blockID]["contours"].append(contour)
    return blocks


def _removeBlockContours(glyph, blockID: str) -> int:
    """Remove contours belonging to blockID. Returns count removed."""
    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    toRemove = [
        c for c in glyph.contours
        if c.identifier and objectLibs.get(c.identifier, {}).get(BLOCK_ID_KEY) == blockID
    ]
    for contour in toRemove:
        del objectLibs[contour.identifier]
        glyph.removeContour(contour)
    glyph.lib[OBJECT_LIBS_KEY] = objectLibs
    return len(toRemove)


def _scrubOrphanedObjectLibs(glyph) -> int:
    """Remove public.objectLibs entries whose contour identifiers no longer exist.

    Returns the number of stale entries removed.
    """
    objectLibs = glyph.lib.get(OBJECT_LIBS_KEY)
    if not objectLibs:
        return 0
    liveIds = {c.identifier for c in glyph.contours if c.identifier}
    stale = [k for k in objectLibs if k not in liveIds]
    if not stale:
        return 0
    cleaned = dict(objectLibs)
    for k in stale:
        del cleaned[k]
    glyph.lib[OBJECT_LIBS_KEY] = cleaned
    return len(stale)


def _insertTaggedContours(glyph, glyphMathObj, location: dict,
                          normalizedLocation: dict, dsRelPath: str,
                          glyphName: str,
                          transform: tuple = (1, 0, 0, 1, 0, 0),
                          color: list = None,
                          blockID: str = None,
                          scalePercent: tuple = None,
                          stemRatioV: float = 1.0,
                          stemRatioH: float = 1.0) -> list:
    """Draw interpolated contours into glyph, apply transform, tag in objectLibs.

    location:           {axisName: designValue, ...}
    normalizedLocation: {axisTag: normalizedValue, ...}
    Returns list of new contour identifiers.
    """
    if color is None:
        color = _generateBlockColor(glyph)
    if blockID is None:
        blockID = str(uuid.uuid4())

    countBefore = len(glyph)
    if hasattr(glyphMathObj, 'drawPoints'):
        glyphMathObj.drawPoints(glyph.getPointPen())
    else:
        glyphMathObj.draw(glyph.getPen())

    if transform != (1, 0, 0, 1, 0, 0):
        for contour in glyph.contours[countBefore:]:
            contour.transformBy(transform)

    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    meta = {
        CONTOUR_SOURCE_KEY: dsRelPath,
        BLOCK_ID_KEY: blockID,
        BLOCK_COLOR_KEY: color,
        f"{LIB_PREFIX}.glyphName": glyphName,
        f"{LIB_PREFIX}.location": dict(location),
        f"{LIB_PREFIX}.normalizedLocation": dict(normalizedLocation),
        f"{LIB_PREFIX}.transform": list(transform),
    }
    if scalePercent is not None:
        meta[f"{LIB_PREFIX}.scalePercent"] = list(scalePercent)
        meta[f"{LIB_PREFIX}.stemRatioV"] = stemRatioV
        meta[f"{LIB_PREFIX}.stemRatioH"] = stemRatioH
    newIds = []
    for contour in glyph.contours[countBefore:]:
        identifier = contour.getIdentifier()
        objectLibs[identifier] = meta
        newIds.append(identifier)
    glyph.lib[OBJECT_LIBS_KEY] = objectLibs
    return newIds


# ---------------------------------------------------------------------------
# Model — DesignSpaceManager
# ---------------------------------------------------------------------------

class DesignSpaceManager:
    """Manages known designspace paths and the currently loaded UFOOperator.

    Responsible for:
    - Tracking registered paths (deduplicated, newest first)
    - Loading a designspace into a UFOOperator
    - Interpolating glyphs at a given normalised location
    """

    def __init__(self):
        self._paths = []         # ordered list of absolute, normalised paths
        self._wghtAxis = None
        self._wdthAxis = None
        self._op = None
        self._dsRelPath = None
        self._dsPath = None
        self._dsDir = None
        self._dsSources = []
        self._defaultFont = None
        self._scaler = None          # MutatorScaleEngine for compensated mode
        self._scalerDefaultStems = (100, 50)  # (vstem, hstem) from default source

    # ---- Properties ----

    @property
    def paths(self) -> list:
        """All registered designspace paths (newest first)."""
        return list(self._paths)

    @property
    def currentPath(self) -> str:
        """Absolute path of the currently loaded designspace, or None."""
        return self._dsPath

    @property
    def currentRelPath(self) -> str:
        """SCRIPT_DIR-relative path of the currently loaded designspace, or None."""
        return self._dsRelPath

    @property
    def isLoaded(self) -> bool:
        """True when a designspace has been successfully loaded."""
        return (self._op is not None
                and self._defaultFont is not None
                and self._wghtAxis is not None)

    @property
    def wghtAxis(self):
        """The wght axis descriptor, or None."""
        return self._wghtAxis

    @property
    def wdthAxis(self):
        """The wdth axis descriptor, or None."""
        return self._wdthAxis

    @property
    def hasWdth(self) -> bool:
        """True if the loaded designspace has a wdth axis."""
        return self._wdthAxis is not None

    # ---- Path management ----

    def registerPath(self, path: str) -> bool:
        """Add an absolute path to the known list. Returns True if newly added."""
        path = os.path.normpath(os.path.abspath(path))
        if path in self._paths:
            return False
        self._paths.insert(0, path)
        return True

    def indexForPath(self, path: str) -> int:
        """Return the index of path in the known list, or -1."""
        path = os.path.normpath(os.path.abspath(path))
        try:
            return self._paths.index(path)
        except ValueError:
            return -1

    def popupLabels(self) -> list:
        """Return display labels for the designspace popup."""
        if len(self._paths) > 1:
            common = os.path.commonpath(self._paths)
            return [os.path.relpath(p, common) for p in self._paths]
        elif self._paths:
            return [os.path.basename(self._paths[0])]
        return []

    # ---- Loading ----

    def load(self, path: str) -> bool:
        """Load a designspace file. Returns True on success. Skips reload if current."""
        path = os.path.normpath(os.path.abspath(path))
        if path == self._dsPath:
            return True
        try:
            self._op = UFOOperator(path, useVarlib=False, extrapolate=True)
            self._op.loadFonts()
            ds = self._op.doc
            self._wghtAxis = next((a for a in ds.axes if a.tag == "wght"), None)
            self._wdthAxis = next((a for a in ds.axes if a.tag == "wdth"), None)
            self._dsSources = ds.sources
            self._dsDir = os.path.dirname(path)
            self._dsRelPath = os.path.relpath(path, SCRIPT_DIR)
            self._dsPath = path
            self._defaultFont = self._op.findDefaultFont()
            self._scaler = None
            self._scalerDefaultStems = (100, 50)
            try:
                # Build MutatorScaleEngine from wght-axis sources only
                ds = self._op.doc
                nonWghtDefaults = {
                    axis.name: axis.map_forward(axis.default)
                    for axis in ds.axes if axis.tag != "wght"
                }
                wghtFonts = []
                for font, _ in self._op.getFonts():
                    fontPath = getattr(font, "path", None)
                    if not fontPath:
                        continue
                    fontPath = os.path.normpath(os.path.abspath(fontPath))
                    source = next(
                        (s for s in self._dsSources if s.filename and
                         os.path.normpath(os.path.join(self._dsDir, s.filename)) == fontPath),
                        None
                    )
                    if source is None:
                        continue
                    loc = source.location or {}
                    if any(loc.get(ax, dv) != dv for ax, dv in nonWghtDefaults.items()):
                        continue
                    wghtFonts.append(font)
                if len(wghtFonts) >= 2:
                    self._scaler = MutatorScaleEngine(wghtFonts)
                    self._scalerDefaultStems = getRefStems(self._defaultFont)
            except Exception as e:
                print(f"MutatorScale setup failed: {e}")
            return True
        except Exception as e:
            print(f"Failed to load designspace '{path}': {e}")
            return False

    # ---- Interpolation ----

    def interpolateGlyph(self, glyphName: str, normalizedLocation: dict):
        """Return (mathGlyph, designLocation) or None if interpolation fails.

        normalizedLocation: {axisTag: normalizedValue, ...}
        designLocation:     {axisName: designValue, ...} in design-space units
        """
        if not self.isLoaded:
            print("No designspace loaded — choose one from the popup.")
            return None
        if glyphName not in self._defaultFont:
            print(f"'{glyphName}' not found in default source.")
            return None

        designLocation = {}
        axisMap = {"wght": self._wghtAxis, "wdth": self._wdthAxis}
        for tag, axis in axisMap.items():
            if axis is not None and tag in normalizedLocation:
                designLocation[axis.name] = _normalizedToDesign(
                    axis, normalizedLocation[tag]
                )

        mathGlyph = self._op.makeOneGlyph(
            glyphName, designLocation, decomposeComponents=False
        )
        if mathGlyph is None:
            print(f"Could not interpolate '{glyphName}' at {designLocation}")
            return None
        return mathGlyph, designLocation

    def compensatedInterpolateGlyph(self, glyphName: str,
                                     normalizedLocation: dict,
                                     scaleX: float, scaleY: float,
                                     stemRatioV: float = 1.0,
                                     stemRatioH: float = 1.0):
        """Return (scaledGlyph, designLocation) using MutatorScale compensation.

        Builds the glyph at (scaleX, scaleY) with stem-weight compensation:
        MutatorScaleEngine picks the source whose stems, when scaled, match the
        reference stems of the default source multiplied by stemRatioV/stemRatioH.
        The returned glyph is already at the target scale; the caller should
        apply only the offset transform.

        Falls back to plain interpolation if the scaler is unavailable.
        """
        result = self.interpolateGlyph(glyphName, normalizedLocation)
        if result is None:
            return None
        _refGlyph, designLoc = result

        if abs(scaleX - 1.0) < 1e-6 and abs(scaleY - 1.0) < 1e-6:
            return result
        if self._scaler is None:
            return result

        upm = self._defaultFont.info.unitsPerEm or 1000
        vstem, hstem = self._scalerDefaultStems

        try:
            self._scaler.set({
                "width": scaleX / scaleY if scaleY != 0 else 1.0,
                "targetHeight": upm * scaleY,
                "referenceHeight": upm,
            })
            scaledGlyph = self._scaler.getScaledGlyph(
                glyphName, (vstem * stemRatioV, hstem * stemRatioH)
            )
        except Exception as e:
            print(f"MutatorScale compensation failed for '{glyphName}': {e}")
            return result

        if scaledGlyph is None:
            return result
        return scaledGlyph, designLoc

    def defaultNormalizedLocation(self, fontPath: str = None) -> dict:
        """Return the ideal default slider location for the given font.

        If fontPath matches a source in the loaded designspace, returns
        that source's position as normalized slider values.  Otherwise
        returns axis defaults (0.0 for each axis, since slider 0 = default).
        """
        result = {"wght": 0.0}
        if self._wdthAxis is not None:
            result["wdth"] = 0.0

        if not self.isLoaded or not fontPath:
            return result

        fontPath = os.path.normpath(os.path.abspath(fontPath))
        for source in self._dsSources:
            if not source.filename:
                continue
            srcPath = os.path.normpath(
                os.path.join(self._dsDir, source.filename)
            )
            if srcPath == fontPath:
                loc = source.location or {}
                if self._wghtAxis and self._wghtAxis.name in loc:
                    result["wght"] = _userToNormalized(
                        self._wghtAxis, loc[self._wghtAxis.name]
                    )
                if self._wdthAxis and self._wdthAxis.name in loc:
                    result["wdth"] = _userToNormalized(
                        self._wdthAxis, loc[self._wdthAxis.name]
                    )
                break

        return result


# ---------------------------------------------------------------------------
# Controller — TransmutorController (EZUI Panel)
# ---------------------------------------------------------------------------

class TransmutorController(ezui.WindowController):
    """EZUI panel for Transmutor.

    Combines View (owns the EZPanel) and Controller (mediates UI <-> model).
    The TransmutorTool registers itself via setTool() so the controller can
    request Merz redraws after block mutations.
    """

    def build(self):
        self._dsManager = DesignSpaceManager()
        self._tool = None
        self._selectedBlockID = None
        self._normLoc = {}  # normalised axis location, updated on DS load / block select

        content = """
        = TwoColumnForm
        : Designspace:
        (Choose a Designspace ...)  @designspacePopupButton
        : Glyph:
        [__]                @glyphNameField
        : V Stem:
        ---X--- [__]        @stemVSlider
        : H Stem:
        ---X--- [__]        @stemHSlider
        :
        [X] Link Stems      @linkStemsCheckbox
        : Scale X:
        ---X--- [__]        @scaleXSlider
        : Scale Y:
        ---X--- [__]        @scaleYSlider
        :
        [X] Lock Scale      @lockScaleCheckbox
        : X Y:
        * HorizontalStack   @offsetStack
        > [__]              @xField
        > [__]              @yField
        """

        footer = """
        * HorizontalStack
        > (Insert)          @insertButton
        > (Remove)          @removeButton
        """

        descriptionData = dict(
            content=dict(
                titleColumnWidth=100,
                itemColumnWidth=400,
            ),
            designspacePopupButton=dict(
                items=["Choose a Designspace ..."],
            ),
            glyphNameField=dict(value=DEFAULT_GLYPH_NAME),
            xField=dict(value="0", placeholder="X"),
            yField=dict(value="0", placeholder="Y"),
            scaleXSlider=dict(minValue=1, maxValue=200, value=100),
            scaleYSlider=dict(minValue=1, maxValue=200, value=100),
            stemVSlider=dict(minValue=0.0, maxValue=4.0, value=1.0),
            stemHSlider=dict(minValue=0.0, maxValue=4.0, value=1.0),
        )

        self.w = ezui.EZPanel(
            title="Transmutor",
            content=content,
            footer=footer,
            descriptionData=descriptionData,
            controller=self,
            size=(500, "auto"),
            autosaveName="TransmutorPanel",
            closable=False,
        )

        # Pre-populate with any open designspaces
        try:
            for ds in (AllDesignspaces() or []):
                self.addDesignSpace(ds.path)
        except Exception:
            pass

        try:
            current = CurrentDesignspace()
            if current and current.path:
                self.loadDesignSpace(current.path)
        except Exception:
            pass

        # If no current DS but we have registered ones, load the first
        if not self._dsManager.isLoaded and self._dsManager.paths:
            self.loadDesignSpace(self._dsManager.paths[0])

        self._syncAxisSliders()
        self._syncStemControls()
        # Apply initial lock-scale state
        if self.w.getItem("lockScaleCheckbox").get():
            self.w.getItem("scaleYSlider").enable(False)

    def started(self):
        self.w.open()

    # -----------------------------------------------------------------------
    # Tool registration
    # -----------------------------------------------------------------------

    def setTool(self, tool) -> None:
        """Register the active TransmutorTool for Merz redraw callbacks."""
        self._tool = tool

    # -----------------------------------------------------------------------
    # Properties
    # -----------------------------------------------------------------------

    @property
    def dsManager(self) -> DesignSpaceManager:
        """The underlying designspace model."""
        return self._dsManager

    @property
    def selectedBlockID(self) -> str:
        """The ID of the currently selected block, or None."""
        return self._selectedBlockID

    @selectedBlockID.setter
    def selectedBlockID(self, value: str):
        self._selectedBlockID = value

    @property
    def glyphName(self) -> str:
        """Current glyph name from the text field."""
        return self.w.getItem("glyphNameField").get().strip()

    @property
    def normalizedLocation(self) -> dict:
        """Normalised axis location, set when a DS loads or a block is selected.

        Returns {axisTag: normalizedValue} for all active axes.
        """
        return dict(self._normLoc)

    @normalizedLocation.setter
    def normalizedLocation(self, loc: dict):
        """Store the normalised axis location."""
        self._normLoc = dict(loc)

    @property
    def offset(self) -> tuple:
        """Current (x, y) offset as integers, defaulting to 0 on parse failure."""
        def _parse(val):
            try:
                return int(round(float(val)))
            except (ValueError, TypeError):
                return 0
        return (
            _parse(self.w.getItem("xField").get()),
            _parse(self.w.getItem("yField").get()),
        )

    @offset.setter
    def offset(self, xy: tuple):
        """Set X/Y offset fields. Accepts int, float, or pre-formatted strings."""
        self.w.getItem("xField").set(str(xy[0]))
        self.w.getItem("yField").set(str(xy[1]))

    @property
    def lockScale(self) -> bool:
        """Whether Scale X and Scale Y are locked together."""
        return bool(self.w.getItem("lockScaleCheckbox").get())

    @lockScale.setter
    def lockScale(self, value: bool):
        self.w.getItem("lockScaleCheckbox").set(1 if value else 0)
        self.w.getItem("scaleYSlider").enable(not value)

    @property
    def scalePercent(self) -> tuple:
        """Current (scaleX%, scaleY%) from the sliders."""
        return (
            self.w.getItem("scaleXSlider").get(),
            self.w.getItem("scaleYSlider").get(),
        )

    @scalePercent.setter
    def scalePercent(self, xy: tuple):
        """Set the scale sliders. Accepts int or float values."""
        self.w.getItem("scaleXSlider").set(xy[0])
        self.w.getItem("scaleYSlider").set(xy[1])

    @property
    def stemRatios(self) -> tuple:
        """Current (stemV, stemH) multiplier ratios."""
        return (
            self.w.getItem("stemVSlider").get(),
            self.w.getItem("stemHSlider").get(),
        )

    @stemRatios.setter
    def stemRatios(self, vh: tuple):
        """Set the stem sliders."""
        self.w.getItem("stemVSlider").set(vh[0])
        self.w.getItem("stemHSlider").set(vh[1])

    @property
    def linkStems(self) -> bool:
        """Whether V and H stem sliders are linked."""
        return bool(self.w.getItem("linkStemsCheckbox").get())

    @linkStems.setter
    def linkStems(self, value: bool):
        self.w.getItem("linkStemsCheckbox").set(1 if value else 0)
        if self._dsManager.isLoaded:
            self.w.getItem("stemHSlider").enable(not value)

    @property
    def transform(self) -> tuple:
        """6-tuple affine matrix (xx, xy, yx, yy, dx, dy) from current controls."""
        ox, oy = self.offset
        sx = max(1, self.w.getItem("scaleXSlider").get()) / 100.0
        sy = max(1, self.w.getItem("scaleYSlider").get()) / 100.0
        return (sx, 0, 0, sy, ox, oy)

    # -----------------------------------------------------------------------
    # Designspace management
    # -----------------------------------------------------------------------

    def addDesignSpace(self, path: str) -> None:
        """Register a designspace path and refresh the popup."""
        self._dsManager.registerPath(path)
        self._refreshPopup()

    def loadDesignSpace(self, path: str) -> None:
        """Load a designspace and sync the popup selection and axis sliders."""
        path = os.path.normpath(os.path.abspath(path))
        self._dsManager.load(path)
        idx = self._dsManager.indexForPath(path)
        if idx >= 0:
            self.w.getItem("designspacePopupButton").set(idx)
        self._syncAxisSliders()
        self._syncStemControls()

    def _syncAxisSliders(self) -> None:
        """Update the stored normalised location based on the current font."""
        fontPath = self._currentFontPath()
        defaults = self._dsManager.defaultNormalizedLocation(fontPath)
        self._normLoc = dict(defaults)

    def _syncStemControls(self) -> None:
        """Enable/disable stem sliders and link checkbox based on DS load state."""
        active = self._dsManager.isLoaded
        self.w.getItem("stemVSlider").enable(active)
        linked = active and self.w.getItem("linkStemsCheckbox").get()
        self.w.getItem("stemHSlider").enable(active and not linked)
        self.w.getItem("linkStemsCheckbox").enable(active)

    def _currentFontPath(self) -> str:
        """Return the path of the current font, or None."""
        try:
            font = CurrentFont()
            return font.path if font else None
        except Exception:
            return None

    def _refreshPopup(self) -> None:
        """Rebuild popup items from the manager's path list."""
        items = self._dsManager.popupLabels()
        items += [NSMenuItem.separatorItem(), "Choose a Designspace ..."]
        self.w.getItem("designspacePopupButton").setItems(items)

    def _openDesignSpace(self, path: str = None) -> None:
        """Prompt for a DS file if no path given, then register and load."""
        if not path:
            path = GetFile(
                message="Select a Designspace file",
                fileName=None,
                allowsMultipleSelection=False,
                fileTypes=["designspace"],
            )
        if path:
            self.addDesignSpace(path)
            self.loadDesignSpace(path)

    # -----------------------------------------------------------------------
    # Block operations
    # -----------------------------------------------------------------------

    def updateSelectedBlock(self) -> None:
        """Re-interpolate and replace the currently selected block in-place."""
        if self._selectedBlockID is None:
            return
        g = CurrentGlyph()
        if g is None:
            return
        normLoc = self.normalizedLocation
        sx, sy = self.scalePercent
        stemV, stemH = self.stemRatios
        ox, oy = self.offset

        result = self._dsManager.compensatedInterpolateGlyph(
            self.glyphName, normLoc, sx / 100.0, sy / 100.0,
            stemRatioV=stemV, stemRatioH=stemH,
        )
        if result is None:
            return
        mathGlyph, designLoc = result
        appliedTransform = (1, 0, 0, 1, ox, oy)

        # Preserve existing block color
        existingColor = None
        blocks = _getBlocksFromGlyph(g)
        blockInfo = blocks.get(self._selectedBlockID)
        if blockInfo:
            existingColor = blockInfo["meta"].get(BLOCK_COLOR_KEY)

        _removeBlockContours(g, self._selectedBlockID)
        _insertTaggedContours(
            g, mathGlyph, designLoc, normLoc,
            self._dsManager.currentRelPath,
            self.glyphName, appliedTransform,
            color=existingColor, blockID=self._selectedBlockID,
            scalePercent=(sx, sy),
            stemRatioV=stemV, stemRatioH=stemH,
        )
        self._requestRedraw(g)

    def insertNewBlock(self) -> None:
        """Insert a brand-new block with the current panel settings."""
        g = CurrentGlyph()
        if g is None:
            return
        normLoc = self.normalizedLocation
        sx, sy = self.scalePercent
        stemV, stemH = self.stemRatios
        ox, oy = self.offset

        result = self._dsManager.compensatedInterpolateGlyph(
            self.glyphName, normLoc, sx / 100.0, sy / 100.0,
            stemRatioV=stemV, stemRatioH=stemH,
        )
        if result is None:
            return
        mathGlyph, designLoc = result
        appliedTransform = (1, 0, 0, 1, ox, oy)

        g.prepareUndo("Insert Block")
        newIds = _insertTaggedContours(
            g, mathGlyph, designLoc, normLoc,
            self._dsManager.currentRelPath,
            self.glyphName, appliedTransform,
            scalePercent=(sx, sy),
            stemRatioV=stemV, stemRatioH=stemH,
        )
        g.performUndo()

        if newIds:
            objLibs = g.lib.get(OBJECT_LIBS_KEY, {})
            self._selectedBlockID = objLibs.get(newIds[0], {}).get(BLOCK_ID_KEY)
        self._requestRedraw(g)

    def removeSelectedBlock(self) -> None:
        """Remove the currently selected block."""
        if self._selectedBlockID is None:
            return
        g = CurrentGlyph()
        if g is None:
            return
        g.prepareUndo("Remove Block")
        _removeBlockContours(g, self._selectedBlockID)
        _scrubOrphanedObjectLibs(g)
        self._selectedBlockID = None
        g.performUndo()
        self._requestRedraw(g)

    # -----------------------------------------------------------------------
    # UI state
    # -----------------------------------------------------------------------

    def setDefaults(self) -> None:
        """Reset all controls to initial values and deselect."""
        self._selectedBlockID = None
        self.w.getItem("glyphNameField").set(DEFAULT_GLYPH_NAME)

        # Reset normalised location to current font's source position
        fontPath = self._currentFontPath()
        self._normLoc = dict(self._dsManager.defaultNormalizedLocation(fontPath))

        self.scalePercent = (100, 100)
        self.offset = (0, 0)
        self.stemRatios = (1.0, 1.0)
        self.w.getItem("linkStemsCheckbox").set(1)
        self._syncStemControls()
        self.lockScale = True

    def selectBlock(self, meta: dict) -> None:
        """Populate all controls from a block's stored metadata and switch DS."""
        self._selectedBlockID = meta.get(BLOCK_ID_KEY)

        # Switch designspace to match the block
        dsRelPath = meta.get(CONTOUR_SOURCE_KEY)
        if dsRelPath:
            dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, dsRelPath))
            self.addDesignSpace(dsAbsPath)
            self.loadDesignSpace(dsAbsPath)

        glyphName = meta.get(f"{LIB_PREFIX}.glyphName", DEFAULT_GLYPH_NAME)
        self.w.getItem("glyphNameField").set(glyphName)

        self._normLoc = dict(meta.get(f"{LIB_PREFIX}.normalizedLocation", {}))

        # Restore stem ratios
        stemRatioV = meta.get(f"{LIB_PREFIX}.stemRatioV", 1.0)
        stemRatioH = meta.get(f"{LIB_PREFIX}.stemRatioH", 1.0)
        linked = abs(stemRatioV - stemRatioH) < 1e-6
        self.w.getItem("stemVSlider").set(stemRatioV)
        self.w.getItem("stemHSlider").set(stemRatioH)
        self.w.getItem("linkStemsCheckbox").set(1 if linked else 0)
        self._syncStemControls()

        scalePercent = meta.get(f"{LIB_PREFIX}.scalePercent")
        if scalePercent and len(scalePercent) >= 2:
            sx, sy = scalePercent[0], scalePercent[1]
        else:
            transform = meta.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
            xx, _xy, _yx, yy, _dx, _dy = transform
            sx, sy = round(xx * 100), round(yy * 100)
        locked = abs(sx - sy) < 0.5
        self.w.getItem("scaleXSlider").set(sx)
        self.w.getItem("scaleYSlider").set(sy)
        self.w.getItem("lockScaleCheckbox").set(1 if locked else 0)
        self.w.getItem("scaleYSlider").enable(not locked)

        transform = meta.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
        _xx, _xy, _yx, _yy, dx, dy = transform
        self.offset = (int(dx), int(dy))

    def snapToIntegers(self) -> None:
        """Round scale and offset to clean integers (called after scale drag)."""
        sx, sy = self.scalePercent
        self.scalePercent = (int(round(sx)), int(round(sy)))
        # offset getter already rounds via float parse
        self.offset = self.offset

    # -----------------------------------------------------------------------
    # Redraw delegation
    # -----------------------------------------------------------------------

    def _requestRedraw(self, glyph) -> None:
        """Ask the registered tool to refresh Merz layers."""
        if self._tool is not None:
            self._tool.drawBlocks(glyph)
            self._tool.drawHandles(glyph, self._selectedBlockID)

    # -----------------------------------------------------------------------
    # EZUI callbacks
    # -----------------------------------------------------------------------

    def designspacePopupButtonCallback(self, sender):
        """Respond to popup selection: open file picker or switch DS."""
        if sender.getItem() == "Choose a Designspace ...":
            self._openDesignSpace()
        else:
            idx = sender.get()
            paths = self._dsManager.paths
            if 0 <= idx < len(paths):
                self.loadDesignSpace(paths[idx])

    def glyphNameFieldCallback(self, sender):
        self.updateSelectedBlock()

    def xFieldCallback(self, sender):
        self.updateSelectedBlock()

    def yFieldCallback(self, sender):
        self.updateSelectedBlock()

    def scaleXSliderCallback(self, sender):
        if self.lockScale:
            self.w.getItem("scaleYSlider").set(sender.get())
        self.updateSelectedBlock()

    def scaleYSliderCallback(self, sender):
        self.updateSelectedBlock()

    def lockScaleCheckboxCallback(self, sender):
        locked = bool(sender.get())
        self.w.getItem("scaleYSlider").enable(not locked)
        if locked:
            self.w.getItem("scaleYSlider").set(self.w.getItem("scaleXSlider").get())
        self.updateSelectedBlock()

    def stemVSliderCallback(self, sender):
        if self.linkStems:
            self.w.getItem("stemHSlider").set(sender.get())
        self.updateSelectedBlock()

    def stemHSliderCallback(self, sender):
        self.updateSelectedBlock()

    def linkStemsCheckboxCallback(self, sender):
        linked = bool(sender.get())
        self.w.getItem("stemHSlider").enable(not linked)
        if linked:
            self.w.getItem("stemHSlider").set(self.w.getItem("stemVSlider").get())
        self.updateSelectedBlock()

    def insertButtonCallback(self, sender):
        self.insertNewBlock()

    def removeButtonCallback(self, sender):
        self.removeSelectedBlock()


# ---------------------------------------------------------------------------
# Tool — TransmutorTool (BaseEventTool)
# ---------------------------------------------------------------------------

class TransmutorTool(BaseEventTool):
    """Glyph view event tool that drives the TransmutorController panel.

    Handles mouse interaction (block selection, drag-to-move, handle-to-scale)
    and Merz layer rendering (block outlines, selection handles).
    """

    _controller = None

    # Drag state — offset movement
    _dragging = False
    _dragStartPoint = None
    _dragStartOffset = None

    # Drag state — handle scaling
    _scalingHandle = None
    _scalingAnchor = None
    _scalingHandleXY = None
    _scalingStartScaleX = None
    _scalingStartScaleY = None
    _scalingStartOffset = None

    def getToolbarTip(self) -> str:
        return "Transmutor"

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def becomeActive(self) -> None:
        """Show/create the panel, register as the active tool, draw blocks."""
        if self._controller is None:
            self._controller = TransmutorController()
        else:
            self._controller.w.show()
        self._controller.setTool(self)
        g = CurrentGlyph()
        if g is not None:
            self._registerGlyphDesignSpaces(g)
            self.drawBlocks(g)

    def becomeInactive(self) -> None:
        """Hide the panel, scrub orphaned lib entries, and clear all Merz layers."""
        g = CurrentGlyph()
        if g is not None:
            _scrubOrphanedObjectLibs(g)
        if self._controller is not None:
            self._controller.setTool(None)
            self._controller.w.hide()
        self.extensionContainer(_MERZ_BG_ID, location="background", clear=True)
        self._clearHandles()

    def currentGlyphChanged(self) -> None:
        """Deselect and redraw when switching to a different glyph."""
        if self._controller is not None:
            self._controller.setDefaults()
        self._clearHandles()
        g = CurrentGlyph()
        if g is not None:
            _scrubOrphanedObjectLibs(g)
            self._registerGlyphDesignSpaces(g)
            self.drawBlocks(g)
        else:
            self.extensionContainer(_MERZ_BG_ID, location="background", clear=True)

    def _registerGlyphDesignSpaces(self, glyph) -> None:
        """Ensure all DS paths referenced by glyph blocks are in the popup."""
        if self._controller is None:
            return
        for info in _getBlocksFromGlyph(glyph).values():
            dsRelPath = info["meta"].get(CONTOUR_SOURCE_KEY)
            if dsRelPath:
                dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, dsRelPath))
                self._controller.addDesignSpace(dsAbsPath)

    # -----------------------------------------------------------------------
    # Merz drawing
    # -----------------------------------------------------------------------

    def drawBlocks(self, glyph) -> None:
        """Repaint background outlines for every block."""
        container = self.extensionContainer(
            _MERZ_BG_ID, location="background", clear=True
        )
        for _blockID, info in _getBlocksFromGlyph(glyph).items():
            color = info["meta"].get(BLOCK_COLOR_KEY)
            if not color:
                continue
            strokeColor = (color[0], color[1], color[2], OUTLINE_ALPHA)
            for contour in info["contours"]:
                layer = container.appendPathSublayer(
                    fillColor=None,
                    strokeColor=strokeColor,
                    strokeWidth=BLOCK_STROKE_WIDTH,
                )
                pen = layer.getPen()
                contour.draw(pen)

    def drawHandles(self, glyph, blockID: str) -> None:
        """Draw a dashed selection box and 8 handles for the given block."""
        container = self.extensionContainer(
            _MERZ_FG_ID, location="foreground", clear=True
        )
        if glyph is None or blockID is None:
            return

        blocks = _getBlocksFromGlyph(glyph)
        info = blocks.get(blockID)
        if info is None:
            return
        bounds = _unionBounds(info["contours"])
        if bounds is None:
            return
        color = info["meta"].get(BLOCK_COLOR_KEY)
        if not color:
            return

        r, g, b = color[0], color[1], color[2]
        xMin, yMin, xMax, yMax = bounds

        # Dashed bounding box
        boxLayer = container.appendPathSublayer(
            fillColor=None,
            strokeColor=(r, g, b, 1.0),
            strokeWidth=1.5,
        )
        boxLayer.setStrokeDash((5, 5))
        pen = boxLayer.getPen()
        pen.moveTo((xMin, yMin))
        pen.lineTo((xMax, yMin))
        pen.lineTo((xMax, yMax))
        pen.lineTo((xMin, yMax))
        pen.closePath()

        # 8 handles at corners and edge midpoints
        for pos in _handlePositions(bounds):
            container.appendSymbolSublayer(
                position=pos,
                imageSettings=dict(
                    name="rectangle",
                    size=(HANDLE_SIZE, HANDLE_SIZE),
                    fillColor=(r, g, b, 1.0),
                ),
            )

    def _clearHandles(self) -> None:
        """Remove all foreground selection handle layers."""
        self.extensionContainer(_MERZ_FG_ID, location="foreground", clear=True)

    # -----------------------------------------------------------------------
    # Hit testing
    # -----------------------------------------------------------------------

    def _hitTestHandles(self, point, glyph, blockID) -> str | None:
        """Return compass name of the handle at point, or None.

        Hit radius is HANDLE_SIZE screen-pixels converted to glyph units
        via the current view scale.
        """
        if glyph is None or blockID is None:
            return None
        blocks = _getBlocksFromGlyph(glyph)
        info = blocks.get(blockID)
        if info is None:
            return None
        bounds = _unionBounds(info["contours"])
        if bounds is None:
            return None

        viewScale = CurrentGlyphWindow().getGlyphViewScale()
        hitRadius = HANDLE_SIZE * (1.0 / viewScale)

        for name, (hx, hy) in zip(_HANDLE_NAMES, _handlePositions(bounds)):
            if abs(point.x - hx) <= hitRadius and abs(point.y - hy) <= hitRadius:
                return name
        return None

    def _hitTestBlocks(self, point, glyph):
        """Return (blockID, meta) for the first block hit, or None."""
        for blockID, info in _getBlocksFromGlyph(glyph).items():
            bounds = _unionBounds(info["contours"])
            if _pointInBounds(point, bounds):
                return blockID, info["meta"]
        return None

    # -----------------------------------------------------------------------
    # Mouse events
    # -----------------------------------------------------------------------

    def mouseDown(self, point, clickCount: int) -> None:
        """Hit-test handles, then blocks, then deselect on miss."""
        if self._controller is None:
            return
        g = CurrentGlyph()
        if g is None:
            return

        # Priority 1: handle hit on already-selected block
        selectedID = self._controller.selectedBlockID
        if selectedID is not None:
            handleName = self._hitTestHandles(point, g, selectedID)
            if handleName is not None:
                self._beginScaleDrag(handleName, g, selectedID)
                return

        # Priority 2: block body hit
        hit = self._hitTestBlocks(point, g)
        if hit is not None:
            blockID, meta = hit
            self._controller.selectBlock(meta)
            self.drawHandles(g, blockID)
            self._beginOffsetDrag(point, g)
            return

        # Miss — deselect
        self._dragging = False
        self._controller.setDefaults()
        self._clearHandles()

    def _beginScaleDrag(self, handleName: str, glyph, blockID: str) -> None:
        """Capture all state needed for a handle-scaling drag."""
        glyph.prepareUndo("Scale Block")
        info = _HANDLE_SCALING_INFO[handleName]
        blocks = _getBlocksFromGlyph(glyph)
        bounds = _unionBounds(blocks[blockID]["contours"])
        xMin, yMin, xMax, yMax = bounds

        hu, hv = info["handleNorm"]
        au, av = info["anchorNorm"]
        self._scalingHandle = handleName
        self._scalingHandleXY = (_lerp(xMin, xMax, hu), _lerp(yMin, yMax, hv))
        self._scalingAnchor = (_lerp(xMin, xMax, au), _lerp(yMin, yMax, av))
        sx, sy = self._controller.scalePercent
        self._scalingStartScaleX = float(sx)
        self._scalingStartScaleY = float(sy)
        ox, oy = self._controller.offset
        self._scalingStartOffset = (float(ox), float(oy))

    def _beginOffsetDrag(self, point, glyph) -> None:
        """Capture state for a block-body offset drag."""
        glyph.prepareUndo("Move Block")
        self._dragging = True
        self._dragStartPoint = (point.x, point.y)
        ox, oy = self._controller.offset
        self._dragStartOffset = (ox, oy)

    def mouseDragged(self, point, delta) -> None:
        """Route to scale or offset drag handler."""
        if self._controller is None:
            return
        if self._scalingHandle is not None:
            self._handleScaleDrag(point)
        elif self._dragging:
            self._handleOffsetDrag(point)

    def _handleScaleDrag(self, point) -> None:
        """Compute new scale + offset from handle drag and push to controller."""
        info = _HANDLE_SCALING_INFO[self._scalingHandle]
        ax, ay = self._scalingAnchor
        hx, hy = self._scalingHandleXY
        dx0, dy0 = self._scalingStartOffset
        sx0 = self._scalingStartScaleX
        sy0 = self._scalingStartScaleY

        distH0 = hx - ax
        distV0 = hy - ay
        factor_H = ((point.x - ax) / distH0) if (info["scaleH"] and distH0 != 0) else 1.0
        factor_V = ((point.y - ay) / distV0) if (info["scaleV"] and distV0 != 0) else 1.0

        # Shift constrains proportional scaling on corner handles
        if self.shiftDown and info["scaleH"] and info["scaleV"]:
            if abs(factor_H - 1.0) >= abs(factor_V - 1.0):
                factor_V = factor_H
            else:
                factor_H = factor_V

        newScaleX = max(1.0, min(200.0, sx0 * factor_H))
        newScaleY = max(1.0, min(200.0, sy0 * factor_V))

        # Keep the anchor pinned: newOffset = anchor*(1 - factor) + oldOffset*factor
        newDx = ax * (1.0 - factor_H) + dx0 * factor_H if info["scaleH"] else dx0
        newDy = ay * (1.0 - factor_V) + dy0 * factor_V if info["scaleV"] else dy0

        self._controller.scalePercent = (newScaleX, newScaleY)
        # Store precise floats so rounding doesn't accumulate across frames
        self._controller.offset = (f"{newDx:.4f}", f"{newDy:.4f}")
        self._controller.updateSelectedBlock()

    def _handleOffsetDrag(self, point) -> None:
        """Compute new offset from body drag and push to controller."""
        if self._dragStartPoint is None or self._dragStartOffset is None:
            return

        rawDx = point.x - self._dragStartPoint[0]
        rawDy = point.y - self._dragStartPoint[1]

        if self.shiftDown and (rawDx != 0 or rawDy != 0):
            magnitude = math.hypot(rawDx, rawDy)
            angle = math.atan2(rawDy, rawDx)
            snappedAngle = round(angle / (math.pi / 4)) * (math.pi / 4)
            rawDx = magnitude * math.cos(snappedAngle)
            rawDy = magnitude * math.sin(snappedAngle)

        newX = int(round(self._dragStartOffset[0] + rawDx))
        newY = int(round(self._dragStartOffset[1] + rawDy))
        self._controller.offset = (newX, newY)
        self._controller.updateSelectedBlock()

    def mouseUp(self, point) -> None:
        """End any active drag, snapping float values to integers."""
        wasScaling = self._scalingHandle is not None
        wasDragging = self._dragging
        self._clearDragState()
        if wasScaling and self._controller is not None:
            self._controller.snapToIntegers()
            self._controller.updateSelectedBlock()
        if (wasScaling or wasDragging):
            g = CurrentGlyph()
            if g is not None:
                g.performUndo()

    def _clearDragState(self) -> None:
        """Reset all transient drag state."""
        self._dragging = False
        self._dragStartPoint = None
        self._dragStartOffset = None
        self._scalingHandle = None
        self._scalingAnchor = None
        self._scalingHandleXY = None
        self._scalingStartScaleX = None
        self._scalingStartScaleY = None
        self._scalingStartOffset = None


installTool(TransmutorTool())
