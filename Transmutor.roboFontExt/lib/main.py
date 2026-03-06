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

import builtins
import colorsys
import math
import os
import random
import site
import uuid
from dataclasses import dataclass, field
from typing import Optional

import ezui
from AppKit import NSMenuItem
from mojo.events import BaseEventTool, installTool
from fontTools.designspaceLib.split import splitInterpolable
from ufoProcessor.ufoOperator import UFOOperator
from mojo.UI import CurrentGlyphWindow, GetFile, getDefault, setDefault

try:
    import mutatorScale
except ImportError:
    _mutatorScaleLib = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "submodules",
        "MutatorScale",
        "lib",
    )
    site.addsitedir(_mutatorScaleLib)
finally:
    from mutatorScale.objects.scaler import MutatorScaleEngine
    from mutatorScale.utilities.fontUtils import getRefStems


# ---------------------------------------------------------------------------
# Optional RoboFont extension builtins
# ---------------------------------------------------------------------------
# AllDesignspaces and CurrentDesignspace are injected into the global namespace
# by the DesignSpaceEditor extension.  Fall back to None when absent.
AllDesignspaces = getattr(builtins, "AllDesignspaces", None)
CurrentDesignspace = getattr(builtins, "CurrentDesignspace", None)
AllFonts = getattr(builtins, "AllFonts", None)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERBOSE = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_GLYPH_NAME = "A"
OBJECT_LIBS_KEY = "public.objectLibs"
LIB_PREFIX = "co.ohnotype.transmutor"
CONTOUR_SOURCE_KEY = f"{LIB_PREFIX}.source"
BLOCK_ID_KEY = f"{LIB_PREFIX}.blockID"
BLOCK_COLOR_KEY = f"{LIB_PREFIX}.color"
FONT_LIB_DS_KEY = f"{LIB_PREFIX}.lastDesignspace"

BLOCK_STROKE_WIDTH = 8
OUTLINE_ALPHA = 0.1
HANDLE_SIZE = 10

# macOS key codes used in keyDown
_KEY_BACKSPACE = 51
_KEY_FORWARD_DELETE = 117
_KEY_LEFT = 123
_KEY_RIGHT = 124
_KEY_DOWN = 125
_KEY_UP = 126

# Merz container IDs
_MERZ_BG_ID = "co.ohnotype.transmutor"
_MERZ_FG_ID = "co.ohnotype.transmutor.handles"

# Cardinal names for each of the 8 selection handles
_HANDLE_NAMES = ("SW", "S", "SE", "E", "NE", "N", "NW", "W")

# Per-handle scaling metadata.
# handleNorm / anchorNorm: normalised (u, v) within bounds — (0,0)=SW, (1,1)=NE.
# scaleH / scaleV: which axes the handle controls.
_HANDLE_SCALING_INFO = {
    "SW": dict(handleNorm=(0.0, 0.0), anchorNorm=(1.0, 1.0), scaleH=True, scaleV=True),
    "S": dict(handleNorm=(0.5, 0.0), anchorNorm=(0.5, 1.0), scaleH=False, scaleV=True),
    "SE": dict(handleNorm=(1.0, 0.0), anchorNorm=(0.0, 1.0), scaleH=True, scaleV=True),
    "E": dict(handleNorm=(1.0, 0.5), anchorNorm=(0.0, 0.5), scaleH=True, scaleV=False),
    "NE": dict(handleNorm=(1.0, 1.0), anchorNorm=(0.0, 0.0), scaleH=True, scaleV=True),
    "N": dict(handleNorm=(0.5, 1.0), anchorNorm=(0.5, 0.0), scaleH=False, scaleV=True),
    "NW": dict(handleNorm=(0.0, 1.0), anchorNorm=(1.0, 0.0), scaleH=True, scaleV=True),
    "W": dict(handleNorm=(0.0, 0.5), anchorNorm=(1.0, 0.5), scaleH=True, scaleV=False),
}


# ---------------------------------------------------------------------------
# State containers
# ---------------------------------------------------------------------------


@dataclass
class DragState:
    """Transient mouse-drag state for the TransmutorTool.

    Three mutually exclusive drag modes:
    - offset: dragging a block body to reposition it
    - scale: dragging a handle to resize
    - stem: option-dragging to adjust stem weights

    Use setState() for partial updates.  Call reset() on mouseUp to clear.
    """

    # Offset drag
    dragging: bool = False
    dragStartPoint: Optional[tuple] = None  # (x, y) mouse pos at drag start
    dragStartOffset: Optional[tuple] = None  # (ox, oy) controller offset snapshot

    # Scale drag
    scalingHandle: Optional[str] = None  # compass name e.g. "NE", or None
    scalingAnchor: Optional[tuple] = None  # (x, y) opposite corner
    scalingHandleXY: Optional[tuple] = None  # (x, y) handle pos at start
    scalingStartScaleX: Optional[float] = None
    scalingStartScaleY: Optional[float] = None
    scalingStartScalesEqual: bool = True
    scalingStartOffset: Optional[tuple] = None  # (ox, oy) offset snapshot
    scalingStartStemV: Optional[float] = None
    scalingStartStemH: Optional[float] = None

    # Stem drag
    stemDragging: bool = False
    stemDragStartPoint: Optional[tuple] = None  # (x, y) mouse pos at drag start
    stemDragStartV: Optional[float] = None
    stemDragStartH: Optional[float] = None

    @property
    def activeDragType(self) -> Optional[str]:
        """Return the active drag mode: 'scale', 'stem', 'offset', or None."""
        verbosePrint("DragState::activeDragType")
        if self.scalingHandle is not None:
            return "scale"
        if self.stemDragging:
            return "stem"
        if self.dragging:
            return "offset"
        return None

    def reset(self) -> None:
        """Clear all drag state back to defaults."""
        verbosePrint("DragState::reset")
        self.dragging = False
        self.dragStartPoint = None
        self.dragStartOffset = None
        self.scalingHandle = None
        self.scalingAnchor = None
        self.scalingHandleXY = None
        self.scalingStartScaleX = None
        self.scalingStartScaleY = None
        self.scalingStartScalesEqual = True
        self.scalingStartOffset = None
        self.scalingStartStemV = None
        self.scalingStartStemH = None
        self.stemDragging = False
        self.stemDragStartPoint = None
        self.stemDragStartV = None
        self.stemDragStartH = None

    def setState(self, **kwargs) -> None:
        """Apply a partial update to drag state fields."""
        verbosePrint("DragState::setState")
        for key, value in kwargs.items():
            setattr(self, key, value)


@dataclass
class BlockEditState:
    """User-facing editing state for a Transmutor block.

    Single source of truth for the values shown in the EZUI panel.  Use
    setState() for bulk updates — it applies a dict of changes and pushes
    the full state to the UI widgets in one pass (React-style).

    Individual property setters on TransmutorController perform targeted
    widget updates for efficiency during drag interactions.
    """

    selectedBlockID: Optional[str] = None
    glyphName: str = DEFAULT_GLYPH_NAME
    normalizedLocation: dict = field(default_factory=dict)
    offset: tuple = (0, 0)
    scalePercent: tuple = (100, 100)
    stemValues: tuple = (100, 50)
    lockScale: bool = True
    linkStems: bool = True

    def setState(self, controller, **kwargs) -> None:
        """Apply partial updates and push the full state to EZUI widgets.

        Only fields present in kwargs are changed; then _pushToUI() renders
        the complete state into the panel.  Offset values are coerced to
        rounded integers.
        """
        verbosePrint("BlockEditState::setState")
        for key, value in kwargs.items():
            setattr(self, key, value)
        # Coerce offset to rounded ints
        if "offset" in kwargs:
            x, y = self.offset
            self.offset = (int(round(float(x))), int(round(float(y))))
        self._pushToUI(controller)

    def _pushToUI(self, controller) -> None:
        """Render the complete state into the EZUI panel widgets.

        This is the "render" step — a pure function of state that sets every
        widget value and enable/disable flag.  Called after every setState().
        """
        verbosePrint("BlockEditState::_pushToUI")
        w = controller.w
        dsLoaded = controller._dsManager.isLoaded

        w.getItem("glyphNameField").set(self.glyphName)

        # Scale controls (Height = scaleYSlider, Width = scaleXSlider)
        w.getItem("scaleYSlider").set(self.scalePercent[1])
        w.getItem("scaleXSlider").set(self.scalePercent[0])
        w.getItem("lockScaleCheckbox").set(1 if self.lockScale else 0)
        w.getItem("scaleXSlider").enable(not self.lockScale)

        # Stem controls
        w.getItem("stemVSlider").set(self.stemValues[0])
        w.getItem("stemHSlider").set(self.stemValues[1])
        w.getItem("linkStemsCheckbox").set(1 if self.linkStems else 0)
        w.getItem("stemVSlider").enable(dsLoaded)
        w.getItem("stemHSlider").enable(dsLoaded and not self.linkStems)
        w.getItem("linkStemsCheckbox").enable(dsLoaded)


@dataclass
class BlockData:
    """Persisted parameters for a single interpolated block.

    Serialized to / from public.objectLibs as a flat dict of
    co.ohnotype.transmutor.* keys.  Instances are always transient in-memory
    objects — they are never stored directly in font files.

    Use fromRawDict() to deserialize and toRawDict() to serialize.
    """

    blockID: str  # UUID string; generate with uuid.uuid4()
    dsRelPath: str  # SCRIPT_DIR-relative .designspace path
    glyphName: str
    normalizedLocation: dict  # {axisTag: normalizedValue}
    offset: tuple  # (dx, dy) integer pixel offset
    scalePercent: tuple  # (sx%, sy%) e.g. (100, 150)
    stemV: float
    stemH: float
    linkStems: bool
    enabledSources: list  # DS-relative source filenames
    color: Optional[list] = None  # [R, G, B]; None → auto-generated on insert

    @classmethod
    def fromRawDict(
        cls, raw: dict, stemRange: tuple = (100, 100, 50, 50)
    ) -> "BlockData":
        """Deserialize from a public.objectLibs entry.

        Handles both current keys and legacy stemRatioV/stemRatioH and
        transform-based scale/offset written by older versions.
        stemRange is only needed for the legacy stemRatio conversion.
        """
        verbosePrint("BlockData::fromRawDict")
        minV, maxV, minH, maxH = stemRange

        # Stem values
        if f"{LIB_PREFIX}.stemV" in raw:
            stemV = raw[f"{LIB_PREFIX}.stemV"]
            stemH = raw[f"{LIB_PREFIX}.stemH"]
        else:
            normV = raw.get(f"{LIB_PREFIX}.stemRatioV", 0.0)
            normH = raw.get(f"{LIB_PREFIX}.stemRatioH", 0.0)
            stemV = minV + normV * (maxV - minV) if maxV != minV else minV
            stemH = minH + normH * (maxH - minH) if maxH != minH else minH

        # Scale
        scaleData = raw.get(f"{LIB_PREFIX}.scalePercent")
        if scaleData and len(scaleData) >= 2:
            sx, sy = scaleData[0], scaleData[1]
        else:
            transform = raw.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
            xx, _xy, _yx, yy, _dx, _dy = transform
            sx, sy = round(xx * 100), round(yy * 100)

        # Offset
        offsetData = raw.get(f"{LIB_PREFIX}.offset")
        if offsetData and len(offsetData) >= 2:
            dx, dy = int(offsetData[0]), int(offsetData[1])
        else:
            t = raw.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
            dx, dy = int(t[4]), int(t[5])

        return cls(
            blockID=raw.get(BLOCK_ID_KEY, ""),
            dsRelPath=raw.get(CONTOUR_SOURCE_KEY, ""),
            glyphName=raw.get(f"{LIB_PREFIX}.glyphName", DEFAULT_GLYPH_NAME),
            color=raw.get(BLOCK_COLOR_KEY),
            normalizedLocation=dict(raw.get(f"{LIB_PREFIX}.normalizedLocation", {})),
            offset=(dx, dy),
            scalePercent=(sx, sy),
            stemV=stemV,
            stemH=stemH,
            linkStems=raw.get(f"{LIB_PREFIX}.linkStems", True),
            enabledSources=list(raw.get(f"{LIB_PREFIX}.enabledSources", [])),
        )

    def toRawDict(self) -> dict:
        """Serialize to a flat dict suitable for public.objectLibs."""
        verbosePrint("BlockData::toRawDict")
        return {
            CONTOUR_SOURCE_KEY: self.dsRelPath,
            BLOCK_ID_KEY: self.blockID,
            BLOCK_COLOR_KEY: self.color,
            f"{LIB_PREFIX}.glyphName": self.glyphName,
            f"{LIB_PREFIX}.normalizedLocation": dict(self.normalizedLocation),
            f"{LIB_PREFIX}.offset": list(self.offset),
            f"{LIB_PREFIX}.scalePercent": list(self.scalePercent),
            f"{LIB_PREFIX}.stemV": self.stemV,
            f"{LIB_PREFIX}.stemH": self.stemH,
            f"{LIB_PREFIX}.linkStems": self.linkStems,
            f"{LIB_PREFIX}.enabledSources": list(self.enabledSources),
        }


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------


def verbosePrint(s):
    if VERBOSE:
        print(s)


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation: a + (b - a) * t."""
    verbosePrint("_lerp")
    return a + (b - a) * t


def _handlePositions(bounds) -> list:
    """Return 8 (x, y) handle positions for the given bounding box.

    Order matches _HANDLE_NAMES: SW, S, SE, E, NE, N, NW, W.
    """
    verbosePrint("_handlePositions")
    xMin, yMin, xMax, yMax = bounds
    midX = (xMin + xMax) / 2
    midY = (yMin + yMax) / 2
    return [
        (xMin, yMin),  # SW
        (midX, yMin),  # S
        (xMax, yMin),  # SE
        (xMax, midY),  # E
        (xMax, yMax),  # NE
        (midX, yMax),  # N
        (xMin, yMax),  # NW
        (xMin, midY),  # W
    ]


def _unionBounds(contours) -> tuple:
    """Return (xMin, yMin, xMax, yMax) union of contour bounding boxes, or None."""
    verbosePrint("_unionBounds")
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
    verbosePrint("_pointInBounds")
    if bounds is None:
        return False
    xMin, yMin, xMax, yMax = bounds
    return xMin <= point.x <= xMax and yMin <= point.y <= yMax


def _generateBlockColor(glyph) -> list:
    """Return a bright [R, G, B] color maximally distinct from existing block colors.

    Colors are chosen in HSV space (S=0.80, V=0.90) to stay vivid and clear.
    Hue is selected by maximising minimum circular distance from existing hues.
    """
    verbosePrint("_generateBlockColor")
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


def _userToNormalized(axis, userValue: float) -> float:
    """Convert a user-space axis value to a [0, 1] fraction of default→maximum.

    0.0 corresponds to axis.default, 1.0 to axis.maximum.  Values below the
    default clamp to 0; values above the maximum clamp to 1.

    This is used for subspace matching, not for full-range normalisation.
    """
    verbosePrint("_userToNormalized")
    denom = axis.maximum - axis.default
    if denom == 0:
        return 0.0
    return max(0.0, min(1.0, (userValue - axis.default) / denom))


# ---------------------------------------------------------------------------
# Block CRUD — pure glyph-level operations
# ---------------------------------------------------------------------------


def _getBlocksFromGlyph(glyph) -> dict:
    """Return {blockID: {"contours": [...], "data": BlockData}} for tagged contours."""
    verbosePrint("_getBlocksFromGlyph")
    objectLibs = glyph.lib.get(OBJECT_LIBS_KEY, {})
    blocks = {}
    for contour in glyph.contours:
        if not contour.identifier:
            continue
        raw = objectLibs.get(contour.identifier, {})
        if BLOCK_ID_KEY not in raw:
            continue
        blockID = raw[BLOCK_ID_KEY]
        if blockID not in blocks:
            blocks[blockID] = {"contours": [], "data": BlockData.fromRawDict(raw)}
        blocks[blockID]["contours"].append(contour)
    return blocks


def _removeBlockContours(glyph, blockID: str) -> int:
    """Remove contours belonging to blockID. Returns count removed."""
    verbosePrint("_removeBlockContours")
    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    toRemove = [
        c
        for c in glyph.contours
        if c.identifier
        and objectLibs.get(c.identifier, {}).get(BLOCK_ID_KEY) == blockID
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
    verbosePrint("_scrubOrphanedObjectLibs")
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


def _forceContourIdentifier(glyph, contour, newId: str) -> None:
    """Transplant newId onto contour, keeping the glyph's identifier registry in sync.

    fontParts exposes `identifier` as read-only once set, so we write directly
    to the underlying DoodleContour._identifier slot and update the glyph
    registry set (glyph.naked()._identifiers) atomically.
    """
    verbosePrint("_forceContourIdentifier")
    naked = contour.naked()
    glyphNaked = glyph.naked()
    identifiers = glyphNaked._identifiers
    # Remove the auto-generated identifier that was assigned on draw
    oldId = naked._identifier
    if oldId and oldId in identifiers:
        identifiers.discard(oldId)
    # Stamp in the preserved identifier
    naked._identifier = newId
    identifiers.add(newId)


def _insertTaggedContours(
    glyph, glyphMathObj, data: "BlockData", preserveIdentifiers: list = None
) -> list:
    """Draw interpolated contours into glyph, apply offset, and tag in objectLibs.

    If data.color is None a new color is auto-generated from existing block hues.

    preserveIdentifiers: optional list of contour identifiers (one per contour
    produced by glyphMathObj in draw order).  When provided, the newly-inserted
    contours are given those identifiers instead of auto-generated ones, so that
    public.objectLibs keys remain stable across re-interpolations and RoboFont
    undo works correctly.

    Returns list of contour identifiers (old or new).
    """
    verbosePrint("_insertTaggedContours")
    if data.color is None:
        data.color = _generateBlockColor(glyph)

    countBefore = len(glyph)
    if hasattr(glyphMathObj, "drawPoints"):
        glyphMathObj.drawPoints(glyph.getPointPen())
    else:
        glyphMathObj.draw(glyph.getPen())

    dx, dy = data.offset
    if dx or dy:
        t = (1, 0, 0, 1, dx, dy)
        for contour in glyph.contours[countBefore:]:
            contour.transformBy(t)

    # Transplant stable identifiers so objectLibs keys don't change between edits.
    newContours = glyph.contours[countBefore:]
    if preserveIdentifiers and len(preserveIdentifiers) == len(newContours):
        for contour, savedId in zip(newContours, preserveIdentifiers):
            _forceContourIdentifier(glyph, contour, savedId)
    else:
        # Ensure each contour has an identifier (getIdentifier assigns one if missing)
        for contour in newContours:
            contour.getIdentifier()

    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    rawDict = data.toRawDict()
    newIds = []
    for contour in newContours:
        identifier = contour.identifier
        objectLibs[identifier] = rawDict
        newIds.append(identifier)
    glyph.lib[OBJECT_LIBS_KEY] = objectLibs
    return newIds


# ---------------------------------------------------------------------------
# Model — DesignSpaceManager
# ---------------------------------------------------------------------------


class DesignSpaceManager:
    """Manages designspace paths, font sources, subspaces, and interpolation.

    Responsible for:
    - Tracking registered designspace paths (deduplicated, newest first)
    - Loading a designspace into a UFOOperator and indexing its fonts
    - Grouping sources into per-non-wght subspaces with MutatorScale scalers
    - Providing source table data (labels, stem values, checkboxes)
    - Interpolating and density-compensated-scaling glyphs via MutatorScaleEngine
    """

    def __init__(self):
        verbosePrint("DesignSpaceManager::__init__")
        self._paths = []  # ordered list of absolute, normalised paths
        self._wghtAxis = None
        self._op = None
        self._dsRelPath = None
        self._dsPath = None
        self._dsDir = None
        self._dsSources = []
        self._defaultFont = None
        # Per-subspace data: list of (label, normNonWghtLoc, fonts, stems)
        self._subspaceScalers = []
        # All (font, designLoc) pairs from the loaded DS, in source order
        self._allFontLocs = []
        # Per-font display label and reference stems
        self._fontLabels = {}  # id(font) -> str
        self._fontStems = {}  # id(font) -> (vstem, hstem)
        self._fontSourceFiles = {}  # id(font) -> DS-relative source filename
        # Active scaler, rebuilt from currently-enabled fonts
        self._activeScaler = None
        # (minV, maxV, minH, maxH) stem range of currently-enabled fonts
        self._activeStemRange = (100, 100, 50, 50)
        # Last error from compensatedInterpolateGlyph; "" means no error.
        self._lastError: str = ""

    # ---- Properties ----

    @property
    def paths(self) -> list:
        """All registered designspace paths (newest first)."""
        verbosePrint("DesignSpaceManager::paths")
        return list(self._paths)

    @property
    def currentRelPath(self) -> str:
        """SCRIPT_DIR-relative path of the currently loaded designspace, or None."""
        verbosePrint("DesignSpaceManager::currentRelPath")
        return self._dsRelPath

    @property
    def isLoaded(self) -> bool:
        """True when a designspace has been successfully loaded."""
        verbosePrint("DesignSpaceManager::isLoaded")
        return (
            self._op is not None
            and self._defaultFont is not None
            and self._wghtAxis is not None
        )

    @property
    def wghtAxis(self):
        """The wght axis descriptor, or None."""
        verbosePrint("DesignSpaceManager::wghtAxis")
        return self._wghtAxis

    # ---- Path management ----

    def registerPath(self, path: str) -> bool:
        """Add an absolute path to the known list. Returns True if newly added."""
        verbosePrint("DesignSpaceManager::registerPath")
        path = os.path.normpath(os.path.abspath(path))
        if path in self._paths:
            return False
        self._paths.insert(0, path)
        return True

    def indexForPath(self, path: str) -> int:
        """Return the index of path in the known list, or -1."""
        verbosePrint("DesignSpaceManager::indexForPath")
        path = os.path.normpath(os.path.abspath(path))
        try:
            return self._paths.index(path)
        except ValueError:
            return -1

    def popupLabels(self) -> list:
        """Return display labels for the designspace popup."""
        verbosePrint("DesignSpaceManager::popupLabels")
        if len(self._paths) > 1:
            common = os.path.commonpath(self._paths)
            return [os.path.relpath(p, common) for p in self._paths]
        elif self._paths:
            return [os.path.basename(self._paths[0])]
        return []

    # ---- Loading ----

    def load(self, path: str) -> bool:
        """Load a designspace file, index fonts, and build subspace scalers.

        Populates font labels, stem measurements, and groups sources by
        non-wght axis location into subspaces. Returns True on success.
        Skips reload if the path matches the currently loaded designspace.
        """
        verbosePrint("DesignSpaceManager::load")
        path = os.path.normpath(os.path.abspath(path))
        if path == self._dsPath:
            return True
        try:
            self._op = UFOOperator(path, useVarlib=False, extrapolate=True)
            self._op.loadFonts()
            ds = self._op.doc
            self._wghtAxis = next((a for a in ds.axes if a.tag == "wght"), None)
            self._dsSources = ds.sources
            self._dsDir = os.path.dirname(path)
            self._dsRelPath = os.path.relpath(path, SCRIPT_DIR)
            self._dsPath = path
            self._defaultFont = self._op.findDefaultFont()
            self._subspaceScalers = []
            self._allFontLocs = []
            self._fontLabels = {}
            self._fontStems = {}
            self._fontSourceFiles = {}
            self._activeScaler = None
            self._activeStemRange = (100, 100, 50, 50)
            try:
                nonWghtAxes = [a for a in ds.axes if a.tag != "wght"]
                wghtAxisName = self._wghtAxis.name if self._wghtAxis else None
                wghtAxisDefault = (
                    self._wghtAxis.map_forward(self._wghtAxis.default)
                    if self._wghtAxis
                    else 0
                )

                # Collect all (defcon.Font, designLoc) pairs once.
                allFontLocs = list(self._op.getFonts())
                self._allFontLocs = allFontLocs

                # Build per-font labels and stem lookups.
                # Map design locations to source descriptors for readable names.
                sourceLookup = {}
                for src in ds.sources:
                    locKey = tuple(sorted((src.location or {}).items()))
                    sourceLookup[locKey] = src

                for font, designLoc in allFontLocs:
                    locKey = tuple(sorted(designLoc.items()))
                    src = sourceLookup.get(locKey)
                    if src and src.familyName and src.styleName:
                        label = f"{src.familyName} {src.styleName}"
                    elif src and src.filename:
                        label = os.path.splitext(os.path.basename(src.filename))[0]
                    else:
                        label = "?"
                    self._fontLabels[id(font)] = label
                    self._fontSourceFiles[id(font)] = src.filename if src else None
                    try:
                        v, h = getRefStems(font)
                        # Discard implausible zero measurements
                        self._fontStems[id(font)] = (
                            v if v > 0 else None,
                            h if h > 0 else None,
                        )
                    except Exception:
                        self._fontStems[id(font)] = (None, None)

                # Separate discrete axes (have 'values') from continuous non-wght axes.
                # Discrete axes (e.g. ital) are split via splitInterpolable;
                # continuous non-wght axes (e.g. wdth) are then grouped within
                # each discrete sub-space.
                discreteAxes = [a for a in ds.axes if hasattr(a, "values")]
                contNonWghtAxes = [a for a in nonWghtAxes if not hasattr(a, "values")]
                contNonWghtAxisNames = {a.name for a in contNonWghtAxes}

                # Build a path → (font, designLoc) lookup for sub-space matching.
                pathToFontLoc = {}
                for font, designLoc in allFontLocs:
                    fontPath = getattr(font, "path", None)
                    if fontPath:
                        pathToFontLoc[os.path.normpath(fontPath)] = (font, designLoc)

                # splitInterpolable yields one sub-doc per combination of
                # discrete axis values; yields a single (empty, full doc) when
                # there are no discrete axes.
                for discreteLoc, subDoc in splitInterpolable(
                    ds, makeNames=False, expandLocations=False
                ):

                    # Match sub-doc sources to the already-loaded fonts.
                    subFontLocs = []
                    for src in subDoc.sources:
                        srcPath = src.path and os.path.normpath(src.path)
                        if srcPath and srcPath in pathToFontLoc:
                            subFontLocs.append(pathToFontLoc[srcPath])
                    if len(subFontLocs) < 2:
                        continue

                    # Build the discrete portion of normNonWght and label.
                    normDiscrete = {}
                    discreteLabelParts = []
                    for axisName, userVal in discreteLoc.items():
                        dAxis = next(
                            (a for a in discreteAxes if a.name == axisName), None
                        )
                        if dAxis is not None:
                            try:
                                idx = dAxis.values.index(userVal)
                                normDiscrete[dAxis.tag] = idx / max(
                                    1, len(dAxis.values) - 1
                                )
                            except ValueError:
                                normDiscrete[dAxis.tag] = 0.0
                            valStr = (
                                str(int(userVal))
                                if userVal == int(userVal)
                                else f"{userVal:.4g}"
                            )
                            axisLabel = dAxis.name or dAxis.tag
                            discreteLabelParts.append(f"{axisLabel} {valStr}")

                    # Further group by continuous non-wght axis values (e.g. wdth).
                    contSubspaceMap = {}
                    for font, designLoc in subFontLocs:
                        contKey = tuple(
                            sorted(
                                (name, designLoc.get(name, 0.0))
                                for name in contNonWghtAxisNames
                            )
                        )
                        contSubspaceMap.setdefault(contKey, []).append(
                            (font, designLoc)
                        )

                    for contKey, fontLocs in contSubspaceMap.items():
                        if len(fontLocs) < 2:
                            continue
                        fonts = [f for f, _ in fontLocs]

                        # Merge discrete + continuous portions of normNonWght.
                        normNonWght = dict(normDiscrete)
                        for name, designVal in contKey:
                            axis = next(
                                (a for a in contNonWghtAxes if a.name == name), None
                            )
                            if axis is not None:
                                userVal = axis.map_backward(designVal)
                                normNonWght[axis.tag] = _userToNormalized(axis, userVal)

                        refFont, _ = min(
                            fontLocs,
                            key=lambda fl: (
                                abs(fl[1].get(wghtAxisName, 0) - wghtAxisDefault)
                                if wghtAxisName
                                else 0
                            ),
                        )
                        subStems = self._fontStems.get(id(refFont), (None, None))

                        # Build human-readable label.
                        # Use full axis name (not the 4-letter tag), a
                        # thin · separator between parts, and clean integer
                        # formatting for whole-number values.
                        labelParts = list(discreteLabelParts)
                        for name, designVal in contKey:
                            axis = next(
                                (a for a in contNonWghtAxes if a.name == name), None
                            )
                            if axis is not None:
                                uVal = axis.map_backward(designVal)
                                valStr = (
                                    str(int(uVal))
                                    if uVal == int(uVal)
                                    else f"{uVal:.4g}"
                                )
                                axisLabel = axis.name or axis.tag
                                labelParts.append(f"{axisLabel} {valStr}")
                        label = " — ".join(labelParts) if labelParts else "Default"

                        # Tuple format: (label, normNonWght, fonts_list, refStems)
                        # fonts_list holds original defcon.Font objects.
                        self._subspaceScalers.append(
                            (label, normNonWght, fonts, subStems)
                        )

                # Activate the first subspace by default.
                if self._subspaceScalers:
                    _, _, initFonts, _initStems = self._subspaceScalers[0]
                    try:
                        self._activeScaler = MutatorScaleEngine(initFonts)
                        self._activeStemRange = self._stemRangeForFonts(initFonts)
                    except Exception as e:
                        print(f"MutatorScale initial build failed: {e}")
                    print(
                        f"Transmutor: built {len(self._subspaceScalers)} subspace(s)."
                    )
                else:
                    print("Transmutor: no scaled subspace with ≥2 sources found.")
            except Exception as e:
                print(f"MutatorScale setup failed: {e}")
            return True
        except Exception as e:
            print(f"Failed to load designspace '{path}': {e}")
            return False

    # ---- Interpolation ----

    def subspaceLabels(self) -> list:
        """Return display labels for each subspace, or [] when only one exists."""
        verbosePrint("DesignSpaceManager::subspaceLabels")
        if len(self._subspaceScalers) <= 1:
            return []
        return [lbl for lbl, _nn, _fts, _st in self._subspaceScalers]

    def subspaceNormLoc(self, index: int) -> dict:
        """Return the non-wght normalised location for the subspace at index."""
        verbosePrint("DesignSpaceManager::subspaceNormLoc")
        if 0 <= index < len(self._subspaceScalers):
            return dict(self._subspaceScalers[index][1])
        return {}

    def subspaceFontIdSet(self, index: int) -> set:
        """Return a set of id(font) values for all fonts in the subspace."""
        verbosePrint("DesignSpaceManager::subspaceFontIdSet")
        if 0 <= index < len(self._subspaceScalers):
            _, _, fonts, _ = self._subspaceScalers[index]
            return {id(f) for f in fonts}
        return {id(f) for f, _ in self._allFontLocs}  # fallback: all

    def fontsForSubspace(self, index: int) -> list:
        """Return the list of fonts for the subspace at index."""
        verbosePrint("DesignSpaceManager::fontsForSubspace")
        if 0 <= index < len(self._subspaceScalers):
            return list(self._subspaceScalers[index][2])
        return [f for f, _ in self._allFontLocs]

    @property
    def allFonts(self) -> list:
        """All loaded fonts in source order."""
        verbosePrint("DesignSpaceManager::allFonts")
        return [f for f, _ in self._allFontLocs]

    def allSourceRows(self, activeFontIdSet: set) -> list:
        """Return table row dicts for every source.

        activeFontIdSet: set of id(font) values that should be enabled.
        """
        verbosePrint("DesignSpaceManager::allSourceRows")
        rows = []
        for font, _ in self._allFontLocs:
            vstem, hstem = self._fontStems.get(id(font), (None, None))
            rows.append(
                {
                    "selected": id(font) in activeFontIdSet,
                    "name": self._fontLabels.get(id(font), "?"),
                    "vStem": vstem if vstem is not None else "—",
                    "hStem": hstem if hstem is not None else "—",
                }
            )
        return rows

    def fontsFromTableItems(self, items: list) -> list:
        """Return the font objects whose table rows are enabled."""
        verbosePrint("DesignSpaceManager::fontsFromTableItems")
        return [
            font
            for (font, _), item in zip(self._allFontLocs, items)
            if item.get("selected", False)
        ]

    def enabledSourceFiles(self, items: list) -> list:
        """Return DS-relative filenames for the enabled rows in the source table."""
        verbosePrint("DesignSpaceManager::enabledSourceFiles")
        result = []
        for (font, _), item in zip(self._allFontLocs, items):
            if item.get("selected", False):
                fn = self._fontSourceFiles.get(id(font))
                if fn:
                    result.append(fn)
        return result

    def sourceFilesFontIdSet(self, filenames: list) -> set:
        """Return a set of id(font) values whose source filenames are in the list."""
        verbosePrint("DesignSpaceManager::sourceFilesFontIdSet")
        fnSet = set(filenames)
        return {
            id(f)
            for f, _ in self._allFontLocs
            if self._fontSourceFiles.get(id(f)) in fnSet
        }

    def _stemRangeForFonts(self, fonts: list) -> tuple:
        """Return (minV, maxV, minH, maxH) stem range for the given fonts.

        Filters out None / zero measurements.  Falls back to (100, 100, 50, 50)
        when no valid measurements exist.
        """
        verbosePrint("DesignSpaceManager::_stemRangeForFonts")
        vStems = [
            v
            for f in fonts
            for v, _h in [self._fontStems.get(id(f), (None, None))]
            if v is not None and v > 0
        ]
        hStems = [
            h
            for f in fonts
            for _v, h in [self._fontStems.get(id(f), (None, None))]
            if h is not None and h > 0
        ]
        return (
            min(vStems) if vStems else 100,
            max(vStems) if vStems else 100,
            min(hStems) if hStems else 50,
            max(hStems) if hStems else 50,
        )

    def rebuildScalerFromFonts(self, fonts: list) -> None:
        """Rebuild _activeScaler and _activeStemRange from the enabled fonts."""
        verbosePrint("DesignSpaceManager::rebuildScalerFromFonts")
        if len(fonts) < 2:
            self._activeScaler = None
            return
        try:
            self._activeScaler = MutatorScaleEngine(fonts)
            self._activeStemRange = self._stemRangeForFonts(fonts)
        except Exception as e:
            print(f"MutatorScale rebuild failed: {e}")
            self._activeScaler = None

    def compensatedInterpolateGlyph(
        self,
        glyphName: str,
        scaleX: float,
        scaleY: float,
        targetStemV: float = 100.0,
        targetStemH: float = 50.0,
    ):
        """Return a normalised scaled glyph via MutatorScale stem compensation.

        targetStemV / targetStemH are actual stem unit values to target.

        The active MutatorScaleEngine (set by subspace / source-table checkboxes)
        interpolates between its sources to match the target stem thickness at
        the requested scale.

        The returned glyph is shifted so its SW bounding-box corner sits at
        (0, 0).  The original SW position is returned as *baseOffset* so the
        caller can place the glyph at an absolute position in the host glyph.

        Returns (scaledGlyph, baseOffset) or None when the scaler is
        unavailable or the glyph cannot be found.  *baseOffset* is an
        (x, y) tuple representing the glyph's natural SW position before
        normalisation.
        """
        self._lastError = ""
        verbosePrint("DesignSpaceManager::compensatedInterpolateGlyph")
        if not self.isLoaded:
            self._lastError = "No designspace loaded."
            return None
        if glyphName not in self._defaultFont:
            self._lastError = f"\u2018{glyphName}\u2019 not in default source."
            return None

        scaler = self._activeScaler
        if scaler is None:
            self._lastError = "Enable at least 2 sources."
            return None

        upm = self._defaultFont.info.unitsPerEm or 1000

        scaler.mutatorErrors.clear()
        try:
            scaler.set(
                {
                    "width": scaleX / scaleY if scaleY != 0 else 1.0,
                    "targetHeight": upm * scaleY,
                    "referenceHeight": upm,
                }
            )
            scaledGlyph = scaler.getScaledGlyph(glyphName, (targetStemV, targetStemH))
        except Exception:
            self._lastError = (
                f"\u2018{glyphName}\u2019 is incompatible across selected sources."
            )
            return None

        # Surface any errors the MutatorScale engine accumulated internally.
        # MutatorScale's own error formatter can raise AttributeError when it
        # tries to access `.message` on modern exceptions, so we avoid
        # rendering the raw exception object and use a plain message instead.
        if scaler.mutatorErrors:
            self._lastError = (
                f"\u2018{glyphName}\u2019 is incompatible across selected sources."
            )
            return None

        if scaledGlyph is None:
            self._lastError = f"No glyph returned for \u2018{glyphName}\u2019."
            return None

        # Normalise so SW corner sits at (0, 0).  This decouples the glyph's
        # coordinate-space position from MutatorScale's internal placement,
        # which shifts non-linearly with scale due to stem compensation.
        # The caller stores baseOffset as the block's absolute position.
        bounds = scaledGlyph.bounds
        if bounds is not None:
            xMin, yMin = bounds[0], bounds[1]
            baseOffset = (xMin, yMin)
            if xMin != 0 or yMin != 0:
                scaledGlyph.moveBy((-xMin, -yMin))
        else:
            baseOffset = (0, 0)
        return scaledGlyph, baseOffset

    def defaultNormalizedLocation(self, fontPath: str = None) -> dict:
        """Return the ideal default normalised location for the given font.

        All axes are set to their normalised default (0.0).  If fontPath matches
        a source in the loaded designspace, that source's axis position is used
        instead, so opening a condensed font pre-selects the condensed subspace.
        """
        verbosePrint("DesignSpaceManager::defaultNormalizedLocation")
        if not self.isLoaded:
            return {"wght": 0.0}

        # Start with every axis at its default (normalised = 0.0)
        result = {a.tag: 0.0 for a in self._op.doc.axes}

        if not fontPath:
            return result

        fontPath = os.path.normpath(os.path.abspath(fontPath))
        for source in self._dsSources:
            if not source.filename:
                continue
            srcPath = os.path.normpath(os.path.join(self._dsDir, source.filename))
            if srcPath == fontPath:
                loc = source.location or {}
                for axis in self._op.doc.axes:
                    if axis.name in loc:
                        # source.location is in design space; map back to user
                        # space before normalising.
                        userVal = axis.map_backward(loc[axis.name])
                        result[axis.tag] = _userToNormalized(axis, userVal)
                break

        return result

    def defaultStemValues(self, fontPath: str = None) -> tuple:
        """Return (stemV, stemH) for the source matching fontPath.

        Lookup order:
        1. The source whose path matches fontPath exactly.
        2. The default font in the designspace (wght axis default).
        3. The minimum values from the active stem range.
        """
        verbosePrint("DesignSpaceManager::defaultStemValues")
        if not self.isLoaded:
            return (100, 50)

        targetFontId = None

        if fontPath:
            fontPath = os.path.normpath(os.path.abspath(fontPath))
            for source in self._dsSources:
                if not source.filename:
                    continue
                srcPath = os.path.normpath(os.path.join(self._dsDir, source.filename))
                if srcPath == fontPath:
                    # Reverse-lookup the font object by source filename
                    for font, _ in self._allFontLocs:
                        if self._fontSourceFiles.get(id(font)) == source.filename:
                            targetFontId = id(font)
                            break
                    break

        # Fall back to the default (wght-axis-default) font
        if targetFontId is None and self._defaultFont is not None:
            targetFontId = id(self._defaultFont)

        if targetFontId is not None:
            v, h = self._fontStems.get(targetFontId, (None, None))
            if v is not None and h is not None:
                return (v, h)

        # Last resort: minimums of the active stem range
        minV, _maxV, minH, _maxH = self._activeStemRange
        return (minV, minH)


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
        verbosePrint("TransmutorController::build")
        self._dsManager = DesignSpaceManager()
        self._tool = None
        self._state = BlockEditState()

        content = """
        = TwoColumnForm
        : Glyph:
        [__]                @glyphNameField
        !*                  @errorLabel

        ----------------------
        
        : Designspace:
        (Choose a Designspace ...)  @designspacePopupButton
        : Subspace:
        (Default ...)           @subspacePopupButton
        : Sources:
        |--------------------------------------------|     @sourceFontTable
        | selected | name     | vStem    | hStem     |
        |----------|----------|----------|----------|
        |          |          |          |           |
        |--------------------------------------------|
        
        ----------------------
        
        : V Stem:
        ---X--- [__]        @stemVSlider
        : H Stem:
        ---X--- [__]        @stemHSlider
        [X] Constrain      @linkStemsCheckbox
        
        ----------------------
        
        : Height:
        ---X--- [__]        @scaleYSlider
        : Width:
        ---X--- [__]        @scaleXSlider
        [X] Constrain      @lockScaleCheckbox
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
            subspacePopupButton=dict(
                items=["—"],
            ),
            sourceFontTable=dict(
                columnDescriptions=[
                    dict(
                        identifier="selected",
                        title="",
                        width=20,
                        editable=True,
                        cellDescription=dict(cellType="Checkbox"),
                    ),
                    dict(
                        identifier="name",
                        title="Source",
                        editable=False,
                    ),
                    dict(
                        identifier="vStem",
                        title="􀄭",
                        width=30,
                        editable=False,
                    ),
                    dict(
                        identifier="hStem",
                        title="􀄬",
                        width=30,
                        editable=False,
                    ),
                ],
                items=[],
                height=150,
                showColumnTitles=True,
                alternatingRowColors=True,
                doubleClickCallback=self.sourceFontTableDoubleClickCallback,
            ),
            glyphNameField=dict(value=DEFAULT_GLYPH_NAME),
            errorLabel=dict(text="\u00a0"),
            scaleXSlider=dict(minValue=1, maxValue=200, value=100, tickMarks=3),
            scaleYSlider=dict(minValue=1, maxValue=200, value=100, tickMarks=3),
            stemVSlider=dict(minValue=1, maxValue=400, value=100, tickMarks=4),
            stemHSlider=dict(minValue=1, maxValue=400, value=50, tickMarks=4),
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
        if AllDesignspaces is not None:
            for ds in AllDesignspaces() or []:
                if ds.path:
                    self.addDesignSpace(ds.path)

        # Load the current designspace if one is open
        if CurrentDesignspace is not None:
            current = CurrentDesignspace()
            if current and current.path:
                self.loadDesignSpace(current.path)

        # Fall back to the path stored in the current font's lib
        if not self._dsManager.isLoaded:
            font = CurrentFont()
            if font is not None:
                savedPath = font.lib.get(FONT_LIB_DS_KEY)
                if savedPath and os.path.isfile(savedPath):
                    self.addDesignSpace(savedPath)
                    self.loadDesignSpace(savedPath)

        # If still no DS but we have registered ones, load the first
        if not self._dsManager.isLoaded and self._dsManager.paths:
            self.loadDesignSpace(self._dsManager.paths[0])

        self._syncAxisSliders()
        self._syncStemControls()
        self._refreshSubspacePopup()
        # Set stem sliders to the values of the current font (not the range minimum)
        stemV, stemH = self._dsManager.defaultStemValues(self._currentFontPath())
        self.stemValues = (stemV, stemH)
        # Apply initial lock-scale state
        if self.w.getItem("lockScaleCheckbox").get():
            self.w.getItem("scaleXSlider").enable(False)

    def started(self):
        verbosePrint("TransmutorController::started")
        self.w.open()

    # -----------------------------------------------------------------------
    # Tool registration
    # -----------------------------------------------------------------------

    def setTool(self, tool) -> None:
        """Register the active TransmutorTool for Merz redraw callbacks."""
        verbosePrint("TransmutorController::setTool")
        self._tool = tool

    # -----------------------------------------------------------------------
    # Properties
    # -----------------------------------------------------------------------

    @property
    def dsManager(self) -> DesignSpaceManager:
        """The underlying designspace model."""
        verbosePrint("TransmutorController::dsManager")
        return self._dsManager

    @property
    def selectedBlockID(self) -> Optional[str]:
        """The ID of the currently selected block, or None."""
        verbosePrint("TransmutorController::selectedBlockID")
        return self._state.selectedBlockID

    @selectedBlockID.setter
    def selectedBlockID(self, value: Optional[str]):
        verbosePrint("TransmutorController::selectedBlockID")
        self._state.selectedBlockID = value

    @property
    def glyphName(self) -> str:
        """Current glyph name from the state (synced by text-field callback)."""
        verbosePrint("TransmutorController::glyphName")
        return self._state.glyphName

    @property
    def normalizedLocation(self) -> dict:
        """Normalised axis location, set when a DS loads or a block is selected.

        Returns {axisTag: normalizedValue} for all active axes.
        """
        verbosePrint("TransmutorController::normalizedLocation")
        return dict(self._state.normalizedLocation)

    @normalizedLocation.setter
    def normalizedLocation(self, loc: dict):
        """Store the normalised axis location."""
        verbosePrint("TransmutorController::normalizedLocation")
        self._state.normalizedLocation = dict(loc)

    @property
    def offset(self) -> tuple:
        """Current (x, y) offset as integers."""
        verbosePrint("TransmutorController::offset")
        return self._state.offset

    @offset.setter
    def offset(self, xy: tuple):
        """Store the offset, coerced to rounded integers."""
        verbosePrint("TransmutorController::offset")
        self._state.offset = (int(round(float(xy[0]))), int(round(float(xy[1]))))

    @property
    def lockScale(self) -> bool:
        """Whether Scale X and Scale Y are locked together."""
        verbosePrint("TransmutorController::lockScale")
        return self._state.lockScale

    @lockScale.setter
    def lockScale(self, value: bool):
        """Set lock-scale flag and sync checkbox + slider enable state."""
        verbosePrint("TransmutorController::lockScale")
        self._state.lockScale = value
        self.w.getItem("lockScaleCheckbox").set(1 if value else 0)
        self.w.getItem("scaleXSlider").enable(not value)

    @property
    def scalePercent(self) -> tuple:
        """Current (scaleX%, scaleY%) from the state."""
        verbosePrint("TransmutorController::scalePercent")
        return self._state.scalePercent

    @scalePercent.setter
    def scalePercent(self, xy: tuple):
        """Set the scale and sync slider widgets."""
        verbosePrint("TransmutorController::scalePercent")
        self._state.scalePercent = xy
        self.w.getItem("scaleXSlider").set(xy[0])
        self.w.getItem("scaleYSlider").set(xy[1])

    @property
    def stemValues(self) -> tuple:
        """Current (stemV, stemH) as actual stem unit values."""
        verbosePrint("TransmutorController::stemValues")
        return self._state.stemValues

    @stemValues.setter
    def stemValues(self, vh: tuple):
        """Set the stem values and sync slider widgets."""
        verbosePrint("TransmutorController::stemValues")
        self._state.stemValues = vh
        self.w.getItem("stemVSlider").set(vh[0])
        self.w.getItem("stemHSlider").set(vh[1])

    @property
    def linkStems(self) -> bool:
        """Whether V and H stem sliders are linked."""
        verbosePrint("TransmutorController::linkStems")
        return self._state.linkStems

    @linkStems.setter
    def linkStems(self, value: bool):
        """Set link-stems flag and sync checkbox + slider enable state."""
        verbosePrint("TransmutorController::linkStems")
        self._state.linkStems = value
        self.w.getItem("linkStemsCheckbox").set(1 if value else 0)
        if self._dsManager.isLoaded:
            self.w.getItem("stemHSlider").enable(not value)

    # -----------------------------------------------------------------------
    # Designspace management
    # -----------------------------------------------------------------------

    def addDesignSpace(self, path: str) -> None:
        """Register a designspace path and refresh the popup."""
        verbosePrint("TransmutorController::addDesignSpace")
        self._dsManager.registerPath(path)
        self._refreshPopup()

    def loadDesignSpace(self, path: str, updateBlock: bool = True) -> None:
        """Load a designspace and sync the popup selection and axis sliders.

        Pass updateBlock=False when the caller will set up state (normalised
        location, stems, etc.) itself before calling updateSelectedBlock —
        this prevents a flash where the block is briefly drawn with the wrong
        (default) subspace before the correct state is applied.
        """
        verbosePrint("TransmutorController::loadDesignSpace")
        path = os.path.normpath(os.path.abspath(path))
        self._dsManager.load(path)
        idx = self._dsManager.indexForPath(path)
        if idx >= 0:
            self.w.getItem("designspacePopupButton").set(idx)
        # Persist path to the current font's lib so the tool can restore it
        # automatically next time without requiring the user to re-open the DS.
        font = CurrentFont()
        if font is not None:
            font.lib[FONT_LIB_DS_KEY] = path
        self._syncAxisSliders()
        self._syncStemControls()
        self._refreshSubspacePopup()
        if updateBlock:
            self.updateSelectedBlock()

    def _refreshSourceTable(self, subspaceIdx: int) -> None:
        """Populate the sources table and rebuild the active scaler.

        Fonts in the chosen subspace are pre-enabled; all others are disabled.
        When no subspaces exist (wght-only DS) every font is enabled.
        """
        verbosePrint("TransmutorController::_refreshSourceTable")
        labels = self._dsManager.subspaceLabels()
        if labels:
            activeFontIdSet = self._dsManager.subspaceFontIdSet(subspaceIdx)
            fonts = self._dsManager.fontsForSubspace(subspaceIdx)
        else:
            fonts = self._dsManager.allFonts
            activeFontIdSet = {id(f) for f in fonts}
        rows = self._dsManager.allSourceRows(activeFontIdSet)
        self.w.getItem("sourceFontTable").set(rows)
        self._dsManager.rebuildScalerFromFonts(fonts)
        self._updateStemSliderRanges()

    def _refreshSubspacePopup(self) -> None:
        """Populate the subspace dropdown and enable it only when >1 subspace.

        Selects the subspace whose non-wght normalised location is closest to
        _normLoc so the popup initial state matches the loaded font.
        """
        verbosePrint("TransmutorController::_refreshSubspacePopup")
        labels = self._dsManager.subspaceLabels()
        popup = self.w.getItem("subspacePopupButton")
        if labels:
            popup.setItems(labels)
            popup.enable(True)

            def _dist(i):
                loc = self._dsManager.subspaceNormLoc(i)
                return sum(
                    (loc.get(t, 0.0) - self._state.normalizedLocation.get(t, 0.0)) ** 2
                    for t in loc
                )

            bestIdx = min(range(len(labels)), key=_dist)
            popup.set(bestIdx)
            self._applySubspace(bestIdx, updateBlock=False)
        else:
            popup.setItems(["—"])
            popup.set(0)
            popup.enable(False)
            self._refreshSourceTable(0)

    def _applySubspace(self, index: int, updateBlock: bool = True) -> None:
        """Splice the chosen subspace's non-wght values into _normLoc.

        Preserves the current wght value so the weight slider position is
        unaffected when the user just switches subspace.
        """
        verbosePrint("TransmutorController::_applySubspace")
        nonWghtLoc = self._dsManager.subspaceNormLoc(index)
        wghtVal = self._state.normalizedLocation.get("wght", 0.0)
        self._state.normalizedLocation.update(nonWghtLoc)
        # restore wght in case subspaceNormLoc included it
        self._state.normalizedLocation["wght"] = wghtVal
        self._refreshSourceTable(index)
        if updateBlock:
            self.updateSelectedBlock()

    def _syncAxisSliders(self) -> None:
        """Update the stored normalised location based on the current font."""
        verbosePrint("TransmutorController::_syncAxisSliders")
        fontPath = self._currentFontPath()
        defaults = self._dsManager.defaultNormalizedLocation(fontPath)
        self._state.normalizedLocation = dict(defaults)

    def _syncStemControls(self) -> None:
        """Enable/disable stem sliders and link checkbox based on DS load state."""
        verbosePrint("TransmutorController::_syncStemControls")
        active = self._dsManager.isLoaded
        self.w.getItem("stemVSlider").enable(active)
        self.w.getItem("stemHSlider").enable(active and not self._state.linkStems)
        self.w.getItem("linkStemsCheckbox").enable(active)

    def _updateStemSliderRanges(self) -> None:
        """Update stem slider min/max based on the enabled sources' stem range.

        The source min/max define normalized positions 0 and 1.  The slider is
        extended to cover -1 … 2 in that same space so the user can push
        beyond the source extremes.  Current values are clamped into the new
        range.  Called whenever the set of enabled sources changes.
        """
        verbosePrint("TransmutorController::_updateStemSliderRanges")
        if not self._dsManager.isLoaded:
            return
        minV, maxV, minH, maxH = self._dsManager._activeStemRange
        for itemName, lo, hi in (
            ("stemVSlider", minV, maxV),
            ("stemHSlider", minH, maxH),
        ):
            item = self.w.getItem(itemName)
            cur = item.get()
            # Extend the slider to cover normalized -1 … 2:
            #   span * -1  →  lo - span  (one full range below the minimum)
            #   span *  2  →  hi + span  (one full range above the maximum)
            span = hi - lo
            extLo = lo - span
            extHi = hi + span
            item.setMinValue(extLo)
            item.setMaxValue(extHi)
            item.set(max(extLo, min(extHi, cur)))

    def _currentFontPath(self) -> str:
        """Return the path of the current font, or None."""
        verbosePrint("TransmutorController::_currentFontPath")
        try:
            font = CurrentFont()
            return font.path if font else None
        except Exception:
            return None

    def _refreshPopup(self) -> None:
        """Rebuild popup items from the manager's path list."""
        verbosePrint("TransmutorController::_refreshPopup")
        items = self._dsManager.popupLabels()
        items += [NSMenuItem.separatorItem(), "Choose a Designspace ..."]
        self.w.getItem("designspacePopupButton").setItems(items)

    def _openDesignSpace(self, path: str = None) -> None:
        """Prompt for a DS file if no path given, then register and load."""
        verbosePrint("TransmutorController::_openDesignSpace")
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

    def _enabledSourceFiles(self) -> list:
        """Return DS-relative filenames for the currently enabled source rows."""
        verbosePrint("TransmutorController::_enabledSourceFiles")
        items = self.w.getItem("sourceFontTable").get()
        return self._dsManager.enabledSourceFiles(items)

    def updateSelectedBlock(self) -> None:
        """Re-interpolate and replace the currently selected block in-place."""
        verbosePrint("TransmutorController::updateSelectedBlock")
        if self._state.selectedBlockID is None:
            return
        g = CurrentGlyph()
        if g is None:
            return
        normLoc = self.normalizedLocation
        sx, sy = self.scalePercent
        stemV, stemH = self.stemValues
        ox, oy = self.offset

        result = self._dsManager.compensatedInterpolateGlyph(
            self.glyphName,
            sx / 100.0,
            sy / 100.0,
            targetStemV=stemV,
            targetStemH=stemH,
        )
        if result is None:
            self._setError(self._dsManager._lastError)
            return
        self._clearError()
        mathGlyph, _baseOffset = result

        # Preserve existing block color and contour identifiers so that
        # objectLibs keys remain stable and RoboFont undo works correctly.
        existingColor = None
        existingIdentifiers = None
        blocks = _getBlocksFromGlyph(g)
        blockInfo = blocks.get(self._state.selectedBlockID)
        if blockInfo:
            existingColor = blockInfo["data"].color
            existingIdentifiers = [c.identifier for c in blockInfo["contours"]]

        _removeBlockContours(g, self._state.selectedBlockID)
        _insertTaggedContours(
            g,
            mathGlyph,
            BlockData(
                blockID=self._state.selectedBlockID,
                dsRelPath=self._dsManager.currentRelPath,
                glyphName=self.glyphName,
                normalizedLocation=normLoc,
                offset=(ox, oy),
                scalePercent=(sx, sy),
                stemV=stemV,
                stemH=stemH,
                linkStems=self.linkStems,
                enabledSources=self._enabledSourceFiles(),
                color=existingColor,
            ),
            preserveIdentifiers=existingIdentifiers,
        )
        self._requestRedraw(g)

    def insertNewBlock(self) -> None:
        """Insert a brand-new block with the current panel settings."""
        verbosePrint("TransmutorController::insertNewBlock")
        g = CurrentGlyph()
        if g is None:
            return
        normLoc = self.normalizedLocation
        sx, sy = self.scalePercent
        stemV, stemH = self.stemValues
        ox, oy = self.offset

        result = self._dsManager.compensatedInterpolateGlyph(
            self.glyphName,
            sx / 100.0,
            sy / 100.0,
            targetStemV=stemV,
            targetStemH=stemH,
        )
        if result is None:
            self._setError(self._dsManager._lastError)
            return
        self._clearError()
        mathGlyph, baseOffset = result

        # Bake the MutatorScale natural position into the stored offset so
        # the block appears at its natural location.  The user's panel offset
        # (ox, oy) is added on top (typically 0, 0 for a brand-new block).
        absoluteOffset = (
            int(round(baseOffset[0] + ox)),
            int(round(baseOffset[1] + oy)),
        )

        g.prepareUndo("Insert Block")
        blockData = BlockData(
            blockID=str(uuid.uuid4()),
            dsRelPath=self._dsManager.currentRelPath,
            glyphName=self.glyphName,
            normalizedLocation=normLoc,
            offset=absoluteOffset,
            scalePercent=(sx, sy),
            stemV=stemV,
            stemH=stemH,
            linkStems=self.linkStems,
            enabledSources=self._enabledSourceFiles(),
            color=None,  # auto-generated from glyph on insert
        )
        newIds = _insertTaggedContours(g, mathGlyph, blockData)
        g.performUndo()

        if newIds:
            self._state.selectedBlockID = blockData.blockID
            # Push absolute offset to UI so sliders reflect the block's
            # actual position, keeping the SW corner pinned on future
            # scale changes.
            self._state.offset = absoluteOffset
        self._requestRedraw(g)

    def removeSelectedBlock(self) -> None:
        """Remove the currently selected block."""
        verbosePrint("TransmutorController::removeSelectedBlock")
        if self._state.selectedBlockID is None:
            return
        g = CurrentGlyph()
        if g is None:
            return
        g.prepareUndo("Remove Block")
        _removeBlockContours(g, self._state.selectedBlockID)
        _scrubOrphanedObjectLibs(g)
        self._state.selectedBlockID = None
        g.performUndo()
        self._requestRedraw(g)

    # -----------------------------------------------------------------------
    # UI state
    # -----------------------------------------------------------------------

    def _clearError(self) -> None:
        """Clear the footnote error label under the glyph name field."""
        self.w.getItem("errorLabel").set("\u00a0")

    def _setError(self, message: str) -> None:
        """Show *message* in the footnote label; clear it when message is empty."""
        self.w.getItem("errorLabel").set(message if message else "\u00a0")

    def setDefaults(self) -> None:
        """Deselect and reset selection-specific controls.

        Stem values, scale, link/lock state are intentionally preserved so
        that inserting a new block in a different glyph reuses the last
        settings without the user having to re-dial them in.
        """
        verbosePrint("TransmutorController::setDefaults")
        # Sync normalised location to the current font, then re-pick the best
        # subspace so the source table and stem ranges reflect the active font.
        self._syncAxisSliders()
        self._refreshSubspacePopup()

        self._state.setState(
            self,
            selectedBlockID=None,
            glyphName=DEFAULT_GLYPH_NAME,
            offset=(0, 0),
        )

    def selectBlock(self, data: "BlockData") -> None:
        """Populate all controls from a block's data and switch DS."""
        verbosePrint("TransmutorController::selectBlock")

        # Switch designspace to match the block (suppress the premature
        # updateSelectedBlock — we fire it once at the end with correct state).
        if data.dsRelPath:
            dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, data.dsRelPath))
            self.addDesignSpace(dsAbsPath)
            self.loadDesignSpace(dsAbsPath, updateBlock=False)

        # Sync subspace popup to the stored normalised location and activate
        # the matching subspace so the scaler is set correctly before we
        # rebuild from enabledSources below.
        labels = self._dsManager.subspaceLabels()
        if labels:
            subspaceLocs = [
                self._dsManager.subspaceNormLoc(i) for i in range(len(labels))
            ]

            def _dist(loc):
                return sum(
                    (loc.get(t, 0.0) - data.normalizedLocation.get(t, 0.0)) ** 2
                    for t in loc
                )

            bestIdx = min(
                range(len(subspaceLocs)), key=lambda i: _dist(subspaceLocs[i])
            )
            self.w.getItem("subspacePopupButton").set(bestIdx)
            self._applySubspace(bestIdx, updateBlock=False)

        # Restore enabled sources from the block data (overrides subspace
        # defaults that were set by loadDesignSpace → _refreshSourceTable).
        if data.enabledSources:
            fontIdSet = self._dsManager.sourceFilesFontIdSet(data.enabledSources)
            if fontIdSet:
                rows = self._dsManager.allSourceRows(fontIdSet)
                self.w.getItem("sourceFontTable").set(rows)
                fonts = [
                    f for f, _ in self._dsManager._allFontLocs if id(f) in fontIdSet
                ]
                self._dsManager.rebuildScalerFromFonts(fonts)
                self._updateStemSliderRanges()

        # Single setState push writes all widgets at once
        self._state.setState(
            self,
            selectedBlockID=data.blockID,
            glyphName=data.glyphName,
            normalizedLocation=data.normalizedLocation,
            stemValues=(data.stemV, data.stemH),
            linkStems=data.linkStems,
            scalePercent=data.scalePercent,
            lockScale=abs(data.scalePercent[0] - data.scalePercent[1]) < 0.5,
            offset=data.offset,
        )
        # Now that state, subspace, and scaler are all correct, draw once.
        self.updateSelectedBlock()

    def snapToIntegers(self) -> None:
        """Round scale and offset to clean integers (called after scale drag)."""
        verbosePrint("TransmutorController::snapToIntegers")
        sx, sy = self.scalePercent
        self.scalePercent = (int(round(sx)), int(round(sy)))
        # offset getter already rounds via float parse
        self.offset = self.offset

    # -----------------------------------------------------------------------
    # Redraw delegation
    # -----------------------------------------------------------------------

    def _requestRedraw(self, glyph) -> None:
        """Ask the registered tool to refresh Merz layers."""
        verbosePrint("TransmutorController::_requestRedraw")
        if self._tool is not None:
            self._tool.drawBlocks(glyph)
            self._tool.drawHandles(glyph, self._state.selectedBlockID)

    # -----------------------------------------------------------------------
    # EZUI callbacks
    # -----------------------------------------------------------------------

    def designspacePopupButtonCallback(self, sender):
        """Respond to popup selection: open file picker or switch DS."""
        verbosePrint("TransmutorController::designspacePopupButtonCallback")
        if sender.getItem() == "Choose a Designspace ...":
            self._openDesignSpace()
        else:
            idx = sender.get()
            paths = self._dsManager.paths
            if 0 <= idx < len(paths):
                self.loadDesignSpace(paths[idx])

    def subspacePopupButtonCallback(self, sender):
        """Respond to subspace selection: update the non-wght axes of _normLoc."""
        verbosePrint("TransmutorController::subspacePopupButtonCallback")
        self._applySubspace(sender.get())

    def sourceFontTableEditCallback(self, sender):
        """Rebuild the active scaler when the user toggles source checkboxes."""
        verbosePrint("TransmutorController::sourceFontTableEditCallback")
        items = sender.get()
        fonts = self._dsManager.fontsFromTableItems(items)
        self._dsManager.rebuildScalerFromFonts(fonts)
        self._updateStemSliderRanges()
        self.updateSelectedBlock()

    def sourceFontTableDoubleClickCallback(self, sender):
        """Set stem sliders to the double-clicked row's stem values.

        Does nothing if the row does not have numeric stems (shown as '—').
        """
        verbosePrint("TransmutorController::sourceFontTableDoubleClickCallback")
        selection = sender.getSelectedIndexes()
        if not selection:
            return
        row = sender.get()[selection[0]]
        vStem = row.get("vStem")
        hStem = row.get("hStem")
        if not isinstance(vStem, (int, float)) or not isinstance(hStem, (int, float)):
            return
        self.stemValues = (float(vStem), float(hStem))
        self.updateSelectedBlock()

    def glyphNameFieldCallback(self, sender):
        verbosePrint("TransmutorController::glyphNameFieldCallback")
        self._state.glyphName = sender.get().strip()
        self.updateSelectedBlock()

    def scaleXSliderCallback(self, sender):
        verbosePrint("TransmutorController::scaleXSliderCallback")
        self._state.scalePercent = (sender.get(), self._state.scalePercent[1])
        self.updateSelectedBlock()

    def scaleYSliderCallback(self, sender):
        verbosePrint("TransmutorController::scaleYSliderCallback")
        sy = sender.get()
        if self._state.lockScale:
            sx = sy
            self.w.getItem("scaleXSlider").set(sx)
        else:
            sx = self._state.scalePercent[0]
        self._state.scalePercent = (sx, sy)
        self.updateSelectedBlock()

    def lockScaleCheckboxCallback(self, sender):
        verbosePrint("TransmutorController::lockScaleCheckboxCallback")
        locked = bool(sender.get())
        self._state.lockScale = locked
        self.w.getItem("scaleXSlider").enable(not locked)
        if locked:
            sy = self._state.scalePercent[1]
            self._state.scalePercent = (sy, sy)
            self.w.getItem("scaleXSlider").set(sy)
        self.updateSelectedBlock()

    def stemVSliderCallback(self, sender):
        verbosePrint("TransmutorController::stemVSliderCallback")
        v = sender.get()
        if self._state.linkStems:
            # Mirror by normalised position so V and H track proportionally
            minV, maxV, minH, maxH = self._dsManager._activeStemRange
            t = (v - minV) / (maxV - minV) if maxV != minV else 0.0
            h = minH + t * (maxH - minH)
            self.w.getItem("stemHSlider").set(h)
        else:
            h = self._state.stemValues[1]
        self._state.stemValues = (v, h)
        self.updateSelectedBlock()

    def stemHSliderCallback(self, sender):
        verbosePrint("TransmutorController::stemHSliderCallback")
        self._state.stemValues = (self._state.stemValues[0], sender.get())
        self.updateSelectedBlock()

    def linkStemsCheckboxCallback(self, sender):
        verbosePrint("TransmutorController::linkStemsCheckboxCallback")
        linked = bool(sender.get())
        self._state.linkStems = linked
        self.w.getItem("stemHSlider").enable(not linked)
        if linked:
            # Sync H to the same normalised position as V
            minV, maxV, minH, maxH = self._dsManager._activeStemRange
            v = self._state.stemValues[0]
            t = (v - minV) / (maxV - minV) if maxV != minV else 0.0
            h = minH + t * (maxH - minH)
            self._state.stemValues = (v, h)
            self.w.getItem("stemHSlider").set(h)
        self.updateSelectedBlock()

    def insertButtonCallback(self, sender):
        verbosePrint("TransmutorController::insertButtonCallback")
        self.insertNewBlock()

    def removeButtonCallback(self, sender):
        verbosePrint("TransmutorController::removeButtonCallback")
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
    _drag = DragState()

    def getToolbarTip(self) -> str:
        verbosePrint("TransmutorTool::getToolbarTip")
        return "Transmutor"

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def becomeActive(self) -> None:
        """Show/create the panel, register as the active tool, draw blocks."""
        verbosePrint("TransmutorTool::becomeActive")
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
        verbosePrint("TransmutorTool::becomeInactive")
        g = CurrentGlyph()
        if g is not None:
            _scrubOrphanedObjectLibs(g)
        self._hidePanel()

    def fontWillClose(self, font) -> None:
        """Hide the panel when the last font is closed.

        fontWillClose fires before the font is removed, so AllFonts() still
        includes it.  A count of 1 means this is the last open font.
        """
        verbosePrint("TransmutorTool::fontWillClose")
        if AllFonts is not None and len(AllFonts()) <= 1:
            self._hidePanel()

    def _hidePanel(self) -> None:
        """Hide the panel and clear all Merz layers."""
        verbosePrint("TransmutorTool::_hidePanel")
        if self._controller is not None:
            self._controller.setTool(None)
            self._controller.w.hide()
        self.extensionContainer(_MERZ_BG_ID, location="background", clear=True)
        self._clearHandles()

    def currentGlyphChanged(self) -> None:
        """Deselect and redraw when switching to a different glyph."""
        verbosePrint("TransmutorTool::currentGlyphChanged")
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
        verbosePrint("TransmutorTool::_registerGlyphDesignSpaces")
        if self._controller is None:
            return
        for info in _getBlocksFromGlyph(glyph).values():
            dsRelPath = info["data"].dsRelPath
            if dsRelPath:
                dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, dsRelPath))
                self._controller.addDesignSpace(dsAbsPath)

    # -----------------------------------------------------------------------
    # Merz drawing
    # -----------------------------------------------------------------------

    def drawBlocks(self, glyph) -> None:
        """Repaint background outlines for every block."""
        verbosePrint("TransmutorTool::drawBlocks")
        container = self.extensionContainer(
            _MERZ_BG_ID, location="background", clear=True
        )
        for _blockID, info in _getBlocksFromGlyph(glyph).items():
            color = info["data"].color
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
        verbosePrint("TransmutorTool::drawHandles")
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
        color = info["data"].color
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
        verbosePrint("TransmutorTool::_clearHandles")
        self.extensionContainer(_MERZ_FG_ID, location="foreground", clear=True)

    # -----------------------------------------------------------------------
    # Hit testing
    # -----------------------------------------------------------------------

    def _hitTestHandles(self, point, glyph, blockID) -> str | None:
        """Return compass name of the handle at point, or None.

        Hit radius is HANDLE_SIZE screen-pixels converted to glyph units
        via the current view scale.
        """
        verbosePrint("TransmutorTool::_hitTestHandles")
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
        """Return (blockID, BlockData) for the first block hit, or None."""
        verbosePrint("TransmutorTool::_hitTestBlocks")
        for blockID, info in _getBlocksFromGlyph(glyph).items():
            bounds = _unionBounds(info["contours"])
            if _pointInBounds(point, bounds):
                return blockID, info["data"]
        return None

    # -----------------------------------------------------------------------
    # Mouse events
    # -----------------------------------------------------------------------

    def mouseDown(self, point, clickCount: int) -> None:
        """Hit-test handles, then blocks, then deselect on miss."""
        verbosePrint("TransmutorTool::mouseDown")
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
            blockID, data = hit
            self._controller.selectBlock(data)
            self.drawHandles(g, blockID)
            if self.optionDown:
                self._beginStemDrag(point)
            else:
                self._beginOffsetDrag(point, g)
            return

        # Miss — deselect (or insert on double-click)
        self._drag.dragging = False
        self._controller.setDefaults()
        self._clearHandles()
        if clickCount == 2:
            self._controller.insertNewBlock()

    def _beginScaleDrag(self, handleName: str, glyph, blockID: str) -> None:
        """Capture all state needed for a handle-scaling drag."""
        verbosePrint("TransmutorTool::_beginScaleDrag")
        glyph.prepareUndo("Scale Block")
        info = _HANDLE_SCALING_INFO[handleName]
        blocks = _getBlocksFromGlyph(glyph)
        bounds = _unionBounds(blocks[blockID]["contours"])
        xMin, yMin, xMax, yMax = bounds

        hu, hv = info["handleNorm"]
        au, av = info["anchorNorm"]
        sx, sy = self._controller.scalePercent
        ox, oy = self._controller.offset
        stemV, stemH = self._controller.stemValues
        self._drag.setState(
            scalingHandle=handleName,
            scalingHandleXY=(_lerp(xMin, xMax, hu), _lerp(yMin, yMax, hv)),
            scalingAnchor=(_lerp(xMin, xMax, au), _lerp(yMin, yMax, av)),
            scalingStartScaleX=float(sx),
            scalingStartScaleY=float(sy),
            scalingStartScalesEqual=(float(sx) == float(sy)),
            scalingStartOffset=(float(ox), float(oy)),
            scalingStartStemV=float(stemV),
            scalingStartStemH=float(stemH),
        )

    def _beginOffsetDrag(self, point, glyph) -> None:
        """Capture state for a block-body offset drag."""
        verbosePrint("TransmutorTool::_beginOffsetDrag")
        glyph.prepareUndo("Move Block")
        ox, oy = self._controller.offset
        self._drag.setState(
            dragging=True,
            dragStartPoint=(point.x, point.y),
            dragStartOffset=(ox, oy),
        )

    def _beginStemDrag(self, point) -> None:
        """Capture state for an option-drag stem-control operation.

        Without Shift: horizontal movement controls both V and H stems
        proportionally (isotropic). With Shift: X → H stem, Y → V stem
        independently (anisotropic).
        """
        verbosePrint("TransmutorTool::_beginStemDrag")
        g = CurrentGlyph()
        if g is not None:
            g.prepareUndo("Adjust Stems")
        stemV, stemH = self._controller.stemValues
        self._drag.setState(
            stemDragging=True,
            stemDragStartPoint=(point.x, point.y),
            stemDragStartV=float(stemV),
            stemDragStartH=float(stemH),
        )

    def mouseDragged(self, point, delta) -> None:
        """Route to scale, stem, or offset drag handler."""
        verbosePrint("TransmutorTool::mouseDragged")
        if self._controller is None:
            return
        dragType = self._drag.activeDragType
        if dragType == "scale":
            self._handleScaleDrag(point)
        elif dragType == "stem":
            self._handleStemDrag(point)
        elif dragType == "offset":
            self._handleOffsetDrag(point)

    def _handleScaleDrag(self, point) -> None:
        """Compute new scale + offset from handle drag and push to controller."""
        verbosePrint("TransmutorTool::_handleScaleDrag")
        info = _HANDLE_SCALING_INFO[self._drag.scalingHandle]
        ax, ay = self._drag.scalingAnchor
        hx, hy = self._drag.scalingHandleXY
        dx0, dy0 = self._drag.scalingStartOffset
        sx0 = self._drag.scalingStartScaleX
        sy0 = self._drag.scalingStartScaleY

        distH0 = hx - ax
        distV0 = hy - ay
        factor_H = (
            ((point.x - ax) / distH0) if (info["scaleH"] and distH0 != 0) else 1.0
        )
        factor_V = (
            ((point.y - ay) / distV0) if (info["scaleV"] and distV0 != 0) else 1.0
        )

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
        # Sync lock-scale checkbox: locked only when dragging a corner handle
        # with Shift held AND the scales were already equal at drag start.
        # If the block started non-square, or an edge handle is used, uncheck.
        isCorner = info["scaleH"] and info["scaleV"]
        self._controller.lockScale = (
            isCorner and self.shiftDown and self._drag.scalingStartScalesEqual
        )
        # Option held: scale stems proportionally with the block so MutatorScale
        # compensation is temporarily suspended.  V stems (vertical strokes,
        # measured horizontally) scale with factor_H; H stems (horizontal
        # strokes, measured vertically) scale with factor_V.  Releasing Option
        # restores the original stem values.
        if (
            self.optionDown
            and self._drag.scalingStartStemV is not None
            and self._drag.scalingStartStemH is not None
        ):
            newStemV = max(1.0, self._drag.scalingStartStemV * factor_H)
            newStemH = max(1.0, self._drag.scalingStartStemH * factor_V)
            self._controller.stemValues = (newStemV, newStemH)
        elif (
            not self.optionDown
            and self._drag.scalingStartStemV is not None
            and self._drag.scalingStartStemH is not None
        ):
            # Option released mid-drag: restore original stems.
            self._controller.stemValues = (
                self._drag.scalingStartStemV,
                self._drag.scalingStartStemH,
            )
        # Store precise floats so rounding doesn't accumulate across frames
        self._controller.offset = (f"{newDx:.4f}", f"{newDy:.4f}")
        self._controller.updateSelectedBlock()

    def _handleStemDrag(self, point) -> None:
        """Adjust stem sliders based on mouse delta from drag start.

        Without Shift (isotropic): horizontal distance drives V stem; H stem
        mirrors V at the same normalised position within its own range.
        With Shift (anisotropic): X → V stem, Y → H stem independently.
        """
        verbosePrint("TransmutorTool::_handleStemDrag")
        if self._drag.stemDragStartPoint is None:
            return
        minV, maxV, minH, maxH = self._controller._dsManager._activeStemRange
        spanV = maxV - minV
        spanH = maxH - minH
        extLoV, extHiV = minV - spanV, maxV + spanV
        extLoH, extHiH = minH - spanH, maxH + spanH
        dx = point.x - self._drag.stemDragStartPoint[0]
        dy = point.y - self._drag.stemDragStartPoint[1]
        if not self.shiftDown:
            # Isotropic: X movement drives V; H mirrors at same norm position.
            newV = max(extLoV, min(extHiV, self._drag.stemDragStartV + dx))
            t = (newV - minV) / spanV if spanV != 0 else 0.0
            newH = minH + t * spanH
        else:
            # Anisotropic: X → V, Y → H.
            newV = max(extLoV, min(extHiV, self._drag.stemDragStartV + dx))
            newH = max(extLoH, min(extHiH, self._drag.stemDragStartH + dy))
        # Keep the link checkbox and H-slider-enable in sync with shift state.
        linked = not self.shiftDown
        self._controller.linkStems = linked
        self._controller.stemValues = (newV, newH)
        self._controller.updateSelectedBlock()

    def _handleOffsetDrag(self, point) -> None:
        """Compute new offset from body drag and push to controller."""
        verbosePrint("TransmutorTool::_handleOffsetDrag")
        if self._drag.dragStartPoint is None or self._drag.dragStartOffset is None:
            return

        rawDx = point.x - self._drag.dragStartPoint[0]
        rawDy = point.y - self._drag.dragStartPoint[1]

        if self.shiftDown and (rawDx != 0 or rawDy != 0):
            magnitude = math.hypot(rawDx, rawDy)
            angle = math.atan2(rawDy, rawDx)
            snappedAngle = round(angle / (math.pi / 4)) * (math.pi / 4)
            rawDx = magnitude * math.cos(snappedAngle)
            rawDy = magnitude * math.sin(snappedAngle)

        newX = int(round(self._drag.dragStartOffset[0] + rawDx))
        newY = int(round(self._drag.dragStartOffset[1] + rawDy))
        self._controller.offset = (newX, newY)
        self._controller.updateSelectedBlock()

    def mouseUp(self, point) -> None:
        """End any active drag, snapping float values to integers."""
        verbosePrint("TransmutorTool::mouseUp")
        wasScaling = self._drag.scalingHandle is not None
        wasDragging = self._drag.dragging
        wasStemDragging = self._drag.stemDragging
        self._clearDragState()
        if wasScaling and self._controller is not None:
            self._controller.snapToIntegers()
            self._controller.updateSelectedBlock()
        if wasScaling or wasDragging or wasStemDragging:
            g = CurrentGlyph()
            if g is not None:
                g.performUndo()

    def keyDown(self, event) -> None:
        """Handle keyboard shortcuts for the selected block.

        - Backspace / Delete: remove block.
        - Arrow keys: nudge block offset.
            No modifier    → 1 unit
            Shift          → glyphViewShiftIncrement (default 10)
            Cmd+Shift      → glyphViewCommandShiftIncrement (default 100)
        - Option + arrow keys: adjust stem weights (no block movement).
            Left / Right: isotropic (V drives H via norm formula) when
            linkStems is True; V-only when linkStems is False.
            Up / Down: H only, unchecks linkStems.
            Shift multiplies the increment from 1 to 10.
        """
        verbosePrint("TransmutorTool::keyDown")
        if self._controller is None:
            return

        keyCode = event.keyCode()

        # keyCode 51 = backspace/delete, 117 = forward delete
        if keyCode in (_KEY_BACKSPACE, _KEY_FORWARD_DELETE):
            if self._controller.selectedBlockID is not None:
                self._controller.removeSelectedBlock()
            return

        # Arrow keys (123=left, 124=right, 125=down, 126=up)
        if keyCode not in (_KEY_LEFT, _KEY_RIGHT, _KEY_DOWN, _KEY_UP):
            return
        if self._controller.selectedBlockID is None:
            return

        g = CurrentGlyph()
        if g is None:
            return

        # Option: adjust stems rather than move block.
        # Shift only controls the increment (1 vs 10) — it never changes the
        # isotropic/anisotropic mode.  That is determined purely by which axis
        # the arrow key moves:
        #   Left/Right — isotropic when linkStems is True (V drives H via the
        #                same normalised-position formula as option-drag);
        #                V-only when linkStems is False (already anisotropic).
        #   Up/Down    — H only (anisotropic), unchecks linkStems.
        if self.optionDown:
            stemIncrement = 10 if self.shiftDown else 1
            stemV, stemH = self._controller.stemValues
            minV, maxV, minH, maxH = self._controller._dsManager._activeStemRange
            spanV = maxV - minV
            spanH = maxH - minH
            extLoV, extHiV = minV - spanV, maxV + spanV
            extLoH, extHiH = minH - spanH, maxH + spanH

            if keyCode in (_KEY_LEFT, _KEY_RIGHT):
                # Left/Right — V moves; H mirrors if linked.
                delta = stemIncrement if keyCode == _KEY_RIGHT else -stemIncrement
                newV = max(extLoV, min(extHiV, stemV + delta))
                if self._controller.linkStems:
                    t = (newV - minV) / spanV if spanV != 0 else 0.0
                    newH = max(extLoH, min(extHiH, minH + t * spanH))
                else:
                    newH = stemH
                self._controller.stemValues = (newV, newH)
            else:
                # Up/Down — H only; unlink.
                delta = stemIncrement if keyCode == _KEY_UP else -stemIncrement
                newH = max(extLoH, min(extHiH, stemH + delta))
                self._controller.linkStems = False
                self._controller.stemValues = (stemV, newH)

            self._controller.updateSelectedBlock()
            self.drawHandles(g, self._controller.selectedBlockID)
            return

        # Plain / Shift / Cmd+Shift: nudge block offset.
        if self.commandDown and self.shiftDown:
            increment = getDefault("glyphViewCommandShiftIncrement", defaultValue=100)
        elif self.shiftDown:
            increment = getDefault("glyphViewShiftIncrement", defaultValue=10)
        else:
            increment = 1

        dx = {_KEY_LEFT: -increment, _KEY_RIGHT: increment}.get(keyCode, 0)
        dy = {_KEY_DOWN: -increment, _KEY_UP: increment}.get(keyCode, 0)

        ox, oy = self._controller.offset
        g.prepareUndo("Nudge Block")
        self._controller.offset = (ox + dx, oy + dy)
        self._controller.updateSelectedBlock()
        g.performUndo()
        self.drawHandles(g, self._controller.selectedBlockID)

    def didUndo(self, sender) -> None:
        """Resync Merz drawing and panel state after an undo or redo."""
        verbosePrint("TransmutorTool::didUndo")
        self._syncAfterHistoryChange()

    def _syncAfterHistoryChange(self) -> None:
        """Redraw blocks and resync the full controller state after undo/redo.

        Reads BlockData back from the glyph (which now reflects the undone/
        redone state) and pushes it through selectBlock() so all panel
        widgets, sliders, offsets and stems reflect what's actually in the
        glyph.  If the selected block no longer exists, deselects cleanly.
        """
        verbosePrint("TransmutorTool::_syncAfterHistoryChange")
        g = CurrentGlyph()
        if g is None:
            return
        self.drawBlocks(g)
        if self._controller is None:
            return
        selectedID = self._controller.selectedBlockID
        if selectedID is None:
            return
        blocks = _getBlocksFromGlyph(g)
        if selectedID in blocks:
            # Re-read the authoritative BlockData from the glyph and push
            # it to the controller so every panel widget reflects the
            # undone/redone state.
            self._controller.selectBlock(blocks[selectedID]["data"])
            self.drawHandles(g, selectedID)
        else:
            # Block was removed by undo — deselect cleanly
            self._controller.setDefaults()
            self._clearHandles()

    def _clearDragState(self) -> None:
        """Reset all transient drag state."""
        verbosePrint("TransmutorTool::_clearDragState")
        self._drag.reset()


installTool(TransmutorTool())
