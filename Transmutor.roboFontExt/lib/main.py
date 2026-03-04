"""
Transmutor
==========
A RoboFont glyph-view tool for inserting interpolated, density-compensated
contour blocks into the current glyph.

Contours added by this script are tagged in public.objectLibs so they can
always be identified, re-selected, and re-interpolated.

Architecture (MVC, single file):
    Model:      DesignSpaceManager — owns DS paths, UFOOperator, subspaces,
                MutatorScale engines, and interpolation
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
from typing import Optional

import ezui
from AppKit import NSMenuItem
from mojo.events import BaseEventTool, installTool
from mojo.UI import CurrentDesignspace, AllDesignspaces, CurrentFont, CurrentGlyph
from mojo.UI import CurrentGlyphWindow, GetFile
from ufoProcessor.ufoOperator import UFOOperator

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

VERBOSE = True

SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GLYPH_NAME = "A"
OBJECT_LIBS_KEY    = "public.objectLibs"
LIB_PREFIX         = "co.ohnotype.transmutor"
CONTOUR_SOURCE_KEY = f"{LIB_PREFIX}.source"
BLOCK_ID_KEY       = f"{LIB_PREFIX}.blockID"
BLOCK_COLOR_KEY    = f"{LIB_PREFIX}.color"

BLOCK_STROKE_WIDTH = 10
BLOCK_BLUR_RADIUS  = 5
OUTLINE_ALPHA      = 0.15
HANDLE_SIZE        = 10

# Merz container identifiers
_MERZ_BG_ID = "co.ohnotype.transmutor"
_MERZ_FG_ID = "co.ohnotype.transmutor.handles"

# Cardinal names for the 8 selection handles (order matches _handlePositions)
_HANDLE_NAMES = ("SW", "S", "SE", "E", "NE", "N", "NW", "W")

# Per-handle scaling metadata.
# handleNorm / anchorNorm: normalised (u, v) in bounds — (0,0)=SW corner.
# scaleH / scaleV: which axes the handle controls.
_HANDLE_SCALING_INFO = {
    "SW": dict(handleNorm=(0.0, 0.0), anchorNorm=(1.0, 1.0), scaleH=True,  scaleV=True),
    "S" : dict(handleNorm=(0.5, 0.0), anchorNorm=(0.5, 1.0), scaleH=False, scaleV=True),
    "SE": dict(handleNorm=(1.0, 0.0), anchorNorm=(0.0, 1.0), scaleH=True,  scaleV=True),
    "E" : dict(handleNorm=(1.0, 0.5), anchorNorm=(0.0, 0.5), scaleH=True,  scaleV=False),
    "NE": dict(handleNorm=(1.0, 1.0), anchorNorm=(0.0, 0.0), scaleH=True,  scaleV=True),
    "N" : dict(handleNorm=(0.5, 1.0), anchorNorm=(0.5, 0.0), scaleH=False, scaleV=True),
    "NW": dict(handleNorm=(0.0, 1.0), anchorNorm=(1.0, 0.0), scaleH=True,  scaleV=True),
    "W" : dict(handleNorm=(0.0, 0.5), anchorNorm=(1.0, 0.5), scaleH=True,  scaleV=False),
}


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def verbosePrint(s: str) -> None:
    """Print s only when VERBOSE is True."""
    if VERBOSE:
        print(s)


def _lerp(a: float, b: float, t: float) -> float:
    """Return the linear interpolation a + (b − a) × t."""
    return a + (b - a) * t


def _handlePositions(bounds: tuple) -> list:
    """Return 8 (x, y) handle positions for the given bounding box.

    Order matches _HANDLE_NAMES: SW, S, SE, E, NE, N, NW, W.
    """
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


def _unionBounds(contours: list) -> Optional[tuple]:
    """Return (xMin, yMin, xMax, yMax) union of all contour bounding boxes.

    Returns None when no contour has a valid bounding box.
    """
    result = None
    for contour in contours:
        b = contour.bounds
        if b is None:
            continue
        result = b if result is None else (
            min(result[0], b[0]),
            min(result[1], b[1]),
            max(result[2], b[2]),
            max(result[3], b[3]),
        )
    return result


def _pointInBounds(point, bounds: tuple) -> bool:
    """Return True when point lies inside the (xMin, yMin, xMax, yMax) box."""
    if bounds is None:
        return False
    xMin, yMin, xMax, yMax = bounds
    return xMin <= point.x <= xMax and yMin <= point.y <= yMax


def _generateBlockColor(glyph) -> list:
    """Return a bright, unique [R, G, B] color for a new block in glyph.

    Chooses a hue from 12 evenly-spaced candidates that maximises minimum
    circular distance from existing block hues.  Saturation and value are
    fixed at 0.80 / 0.90 to stay vivid and never muddy.
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
    """Convert a normalised [−1, 1] value to design-space coordinates.

    Positive side [0 → 1] maps axis.default → axis.maximum in user space.
    Negative side [−1 → 0] maps axis.minimum → axis.default in user space.
    The result is passed through the axis map to reach design space.
    """
    if normalizedValue >= 0:
        userValue = axis.default + normalizedValue * (axis.maximum - axis.default)
    else:
        userValue = axis.default + normalizedValue * (axis.default - axis.minimum)
    return axis.map_forward(userValue)


def _userToNormalized(axis, userValue: float) -> float:
    """Convert a user-space axis value to a [0, 1] fraction of default → maximum.

    0.0 = axis.default, 1.0 = axis.maximum.  Values outside that range are
    clamped.  Used for subspace distance comparisons; not for the full
    [−1, 1] design-space conversion (see _normalizedToDesign).
    """
    denom = axis.maximum - axis.default
    if denom == 0:
        return 0.0
    return max(0.0, min(1.0, (userValue - axis.default) / denom))


# ---------------------------------------------------------------------------
# Block CRUD — pure glyph-level operations
# ---------------------------------------------------------------------------

def _getBlocksFromGlyph(glyph) -> dict:
    """Return {blockID: {"contours": [...], "meta": {...}}} for every tagged block.

    Only contours with a BLOCK_ID_KEY entry in public.objectLibs are included.
    """
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
    """Remove all contours belonging to blockID and their objectLibs entries.

    Returns the number of contours removed.
    """
    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    toRemove = [
        c for c in glyph.contours
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

    Stale entries accumulate when contours are deleted outside Transmutor
    (e.g. manual edits, undos).  Returns the count removed.
    """
    objectLibs = glyph.lib.get(OBJECT_LIBS_KEY)
    if not objectLibs:
        return 0
    liveIds = {c.identifier for c in glyph.contours if c.identifier}
    stale   = [k for k in objectLibs if k not in liveIds]
    if not stale:
        return 0
    cleaned = dict(objectLibs)
    for k in stale:
        del cleaned[k]
    glyph.lib[OBJECT_LIBS_KEY] = cleaned
    return len(stale)


def _insertTaggedContours(glyph,
                           glyphMathObj,
                           normalizedLocation: dict,
                           dsRelPath: str,
                           glyphName: str,
                           transform: tuple = (1, 0, 0, 1, 0, 0),
                           color: Optional[list] = None,
                           blockID: Optional[str] = None,
                           scalePercent: Optional[tuple] = None,
                           stemRatioV: float = 0.0,
                           stemRatioH: float = 0.0,
                           enabledSources: Optional[list] = None) -> list:
    """Draw interpolated contours into glyph, apply transform, and tag them.

    Draws glyphMathObj into glyph via its point-pen (or regular pen as
    fallback), applies the affine transform, then writes a metadata dict into
    public.objectLibs for each new contour so they can be identified and
    reconstructed later.

    Args:
        glyph:              The target fontParts glyph.
        glyphMathObj:       A MathGlyph or similar object with drawPoints/draw.
        normalizedLocation: {axisTag: normalizedValue} used for interpolation.
        dsRelPath:          SCRIPT_DIR-relative path to the source designspace.
        glyphName:          Name of the source glyph that was interpolated.
        transform:          Affine 6-tuple applied to each new contour.
        color:              [R, G, B] block color; generated if None.
        blockID:            UUID string; generated if None (new block).
        scalePercent:       (scaleX%, scaleY%) stored for later restoration.
        stemRatioV:         Normalised V-stem position used for compensation.
        stemRatioH:         Normalised H-stem position used for compensation.
        enabledSources:     DS-relative filenames of the enabled source fonts.

    Returns:
        List of contour identifier strings for the newly inserted contours.
    """
    if color is None:
        color = _generateBlockColor(glyph)
    if blockID is None:
        blockID = str(uuid.uuid4())

    countBefore = len(glyph)
    if hasattr(glyphMathObj, "drawPoints"):
        glyphMathObj.drawPoints(glyph.getPointPen())
    else:
        glyphMathObj.draw(glyph.getPen())

    if transform != (1, 0, 0, 1, 0, 0):
        for contour in glyph.contours[countBefore:]:
            contour.transformBy(transform)

    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    meta = {
        CONTOUR_SOURCE_KEY:              dsRelPath,
        BLOCK_ID_KEY:                    blockID,
        BLOCK_COLOR_KEY:                 color,
        f"{LIB_PREFIX}.glyphName":       glyphName,
        f"{LIB_PREFIX}.normalizedLocation": dict(normalizedLocation),
        f"{LIB_PREFIX}.transform":       list(transform),
        f"{LIB_PREFIX}.scalePercent":    list(scalePercent) if scalePercent else [100, 100],
        f"{LIB_PREFIX}.stemRatioV":      stemRatioV,
        f"{LIB_PREFIX}.stemRatioH":      stemRatioH,
        f"{LIB_PREFIX}.enabledSources":  list(enabledSources) if enabledSources else [],
    }

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
    """Manages designspace loading, subspace grouping, and glyph interpolation.

    Responsibilities:
    - Tracking registered designspace paths (deduplicated, newest first).
    - Loading a designspace into a UFOOperator and indexing its source fonts.
    - Grouping sources into subspaces keyed by non-wght axis location.
    - Providing source-table data (labels, V/H stem measurements, checkboxes).
    - Building and rebuilding MutatorScaleEngine from the enabled source fonts.
    - Compensated glyph interpolation via the active MutatorScaleEngine.
    """

    def __init__(self):
        verbosePrint("DesignSpaceManager::__init__")
        self._paths: list                    = []
        self._op: Optional[UFOOperator]      = None
        self._wghtAxis                       = None
        self._dsPath: Optional[str]          = None
        self._dsRelPath: Optional[str]       = None
        self._dsDir: Optional[str]           = None
        self._dsSources: list                = []
        self._defaultFont                    = None

        # Subspace data: list of (label, normNonWghtLoc, fonts, refStems)
        self._subspaceScalers: list          = []
        # All (defcon.Font, designLoc) pairs in source order
        self._allFontLocs: list              = []
        # Per-font metadata keyed by id(font)
        self._fontLabels: dict               = {}
        self._fontStems: dict                = {}
        self._fontSourceFiles: dict          = {}
        # Active scaler state — rebuilt from the currently enabled fonts
        self._activeScaler                   = None
        self._activeStemRange: tuple         = (100, 100, 50, 50)

    # ---- Properties ----

    @property
    def paths(self) -> list:
        """All registered designspace paths, newest first."""
        verbosePrint("DesignSpaceManager::paths")
        return list(self._paths)

    @property
    def currentRelPath(self) -> Optional[str]:
        """SCRIPT_DIR-relative path of the loaded designspace, or None."""
        verbosePrint("DesignSpaceManager::currentRelPath")
        return self._dsRelPath

    @property
    def isLoaded(self) -> bool:
        """True when a designspace is loaded with a valid wght axis and default font."""
        verbosePrint("DesignSpaceManager::isLoaded")
        return (self._op is not None
                and self._defaultFont is not None
                and self._wghtAxis is not None)

    @property
    def wghtAxis(self):
        """The wght AxisDescriptor from the loaded designspace, or None."""
        verbosePrint("DesignSpaceManager::wghtAxis")
        return self._wghtAxis

    @property
    def allFonts(self) -> list:
        """All loaded defcon.Font objects in source order."""
        verbosePrint("DesignSpaceManager::allFonts")
        return [f for f, _ in self._allFontLocs]

    # ---- Path management ----

    def registerPath(self, path: str) -> bool:
        """Add path to the known list if not already present.

        Normalises the path to an absolute form before comparison.
        Returns True when newly added, False when already registered.
        """
        verbosePrint("DesignSpaceManager::registerPath")
        path = os.path.normpath(os.path.abspath(path))
        if path in self._paths:
            return False
        self._paths.insert(0, path)
        return True

    def indexForPath(self, path: str) -> int:
        """Return the index of path in the known list, or -1 if absent."""
        verbosePrint("DesignSpaceManager::indexForPath")
        path = os.path.normpath(os.path.abspath(path))
        try:
            return self._paths.index(path)
        except ValueError:
            return -1

    def popupLabels(self) -> list:
        """Return display labels for the designspace dropdown.

        When more than one path is registered, labels are relative to the
        common root so long filenames stay readable.
        """
        verbosePrint("DesignSpaceManager::popupLabels")
        if len(self._paths) > 1:
            common = os.path.commonpath(self._paths)
            return [os.path.relpath(p, common) for p in self._paths]
        elif self._paths:
            return [os.path.basename(self._paths[0])]
        return []

    # ---- Loading ----

    def load(self, path: str) -> bool:
        """Load a designspace, index its fonts, and build per-subspace scalers.

        Groups sources by non-wght axis location into subspaces.  Each subspace
        with ≥2 sources varying along wght gets a label, a normalised non-wght
        location dict, a list of fonts, and reference stem measurements from
        the source closest to the wght default.

        Skips reload when path matches the already-loaded designspace.
        Returns True on success.
        """
        verbosePrint("DesignSpaceManager::load")
        path = os.path.normpath(os.path.abspath(path))
        if path == self._dsPath:
            return True
        try:
            self._op = UFOOperator(path, useVarlib=False, extrapolate=True)
            self._op.loadFonts()
            ds              = self._op.doc
            self._wghtAxis  = next((a for a in ds.axes if a.tag == "wght"), None)
            self._dsSources = ds.sources
            self._dsDir     = os.path.dirname(path)
            self._dsRelPath = os.path.relpath(path, SCRIPT_DIR)
            self._dsPath    = path
            self._defaultFont = self._op.findDefaultFont()

            # Reset all derived state
            self._subspaceScalers = []
            self._allFontLocs     = []
            self._fontLabels      = {}
            self._fontStems       = {}
            self._fontSourceFiles = {}
            self._activeScaler    = None
            self._activeStemRange = (100, 100, 50, 50)

            try:
                nonWghtAxes   = [a for a in ds.axes if a.tag != "wght"]
                nonWghtNames  = {a.name for a in nonWghtAxes}
                wghtAxisName  = self._wghtAxis.name if self._wghtAxis else None
                wghtDefault   = (
                    self._wghtAxis.map_forward(self._wghtAxis.default)
                    if self._wghtAxis else 0
                )

                allFontLocs = list(self._op.getFonts())
                self._allFontLocs = allFontLocs

                # Build per-font display labels and stem lookups.
                sourceLookup = {
                    tuple(sorted((src.location or {}).items())): src
                    for src in ds.sources
                }
                for font, designLoc in allFontLocs:
                    src = sourceLookup.get(tuple(sorted(designLoc.items())))
                    if src and src.familyName and src.styleName:
                        label = f"{src.familyName} {src.styleName}"
                    elif src and src.filename:
                        label = os.path.splitext(os.path.basename(src.filename))[0]
                    else:
                        label = "?"
                    self._fontLabels[id(font)]      = label
                    self._fontSourceFiles[id(font)] = src.filename if src else None
                    try:
                        v, h = getRefStems(font)
                        self._fontStems[id(font)] = (
                            v if v and v > 0 else None,
                            h if h and h > 0 else None,
                        )
                    except Exception:
                        self._fontStems[id(font)] = (None, None)

                # Group fonts by non-wght design-space location.
                subspaceMap: dict = {}
                for font, designLoc in allFontLocs:
                    key = tuple(sorted(
                        (name, designLoc.get(name, 0.0))
                        for name in nonWghtNames
                    ))
                    subspaceMap.setdefault(key, []).append((font, designLoc))

                for nonWghtKey, fontLocs in subspaceMap.items():
                    if len(fontLocs) < 2:
                        continue
                    fonts = [f for f, _ in fontLocs]

                    # Normalised non-wght location (used for subspace matching).
                    normNonWght = {}
                    for name, designVal in nonWghtKey:
                        axis = next((a for a in nonWghtAxes if a.name == name), None)
                        if axis is not None:
                            userVal = axis.map_backward(designVal)
                            normNonWght[axis.tag] = _userToNormalized(axis, userVal)

                    # Reference stems from the source closest to the wght default.
                    refFont, _ = min(
                        fontLocs,
                        key=lambda fl: abs(
                            fl[1].get(wghtAxisName, 0) - wghtDefault
                        ) if wghtAxisName else 0,
                    )

                    # Human-readable subspace label, e.g. "wdth 75  ital 1"
                    if nonWghtKey:
                        parts = []
                        for name, designVal in nonWghtKey:
                            axis = next((a for a in nonWghtAxes if a.name == name), None)
                            if axis is not None:
                                uVal    = axis.map_backward(designVal)
                                valStr  = str(int(uVal)) if uVal == int(uVal) else f"{uVal:.4g}"
                                parts.append(f"{axis.tag} {valStr}")
                        label = "  ".join(parts)
                    else:
                        label = "Default"

                    self._subspaceScalers.append((label, normNonWght, fonts, refFont))

                if self._subspaceScalers:
                    _, _, initFonts, _ = self._subspaceScalers[0]
                    try:
                        self._activeScaler    = MutatorScaleEngine(initFonts)
                        self._activeStemRange = self._stemRangeForFonts(initFonts)
                    except Exception as e:
                        print(f"Transmutor: MutatorScale initial build failed: {e}")
                    print(f"Transmutor: built {len(self._subspaceScalers)} subspace(s).")
                else:
                    print("Transmutor: no subspace with ≥2 sources found.")

            except Exception as e:
                print(f"Transmutor: MutatorScale setup failed: {e}")
            return True

        except Exception as e:
            print(f"Transmutor: failed to load '{path}': {e}")
            return False

    # ---- Subspace access ----

    def subspaceLabels(self) -> list:
        """Return display labels for each subspace.

        Returns an empty list when only one subspace exists (no dropdown needed).
        """
        verbosePrint("DesignSpaceManager::subspaceLabels")
        if len(self._subspaceScalers) <= 1:
            return []
        return [lbl for lbl, _nn, _fts, _rf in self._subspaceScalers]

    def subspaceNormLoc(self, index: int) -> dict:
        """Return the non-wght normalised location dict for the subspace at index."""
        verbosePrint("DesignSpaceManager::subspaceNormLoc")
        if 0 <= index < len(self._subspaceScalers):
            return dict(self._subspaceScalers[index][1])
        return {}

    def subspaceFontIdSet(self, index: int) -> set:
        """Return id(font) values for every font in the subspace at index."""
        verbosePrint("DesignSpaceManager::subspaceFontIdSet")
        if 0 <= index < len(self._subspaceScalers):
            _, _, fonts, _ = self._subspaceScalers[index]
            return {id(f) for f in fonts}
        return {id(f) for f, _ in self._allFontLocs}

    def fontsForSubspace(self, index: int) -> list:
        """Return the font list for the subspace at index."""
        verbosePrint("DesignSpaceManager::fontsForSubspace")
        if 0 <= index < len(self._subspaceScalers):
            return list(self._subspaceScalers[index][2])
        return [f for f, _ in self._allFontLocs]

    # ---- Source table helpers ----

    def allSourceRows(self, activeFontIdSet: set) -> list:
        """Return table-row dicts for every source font.

        activeFontIdSet determines which rows are pre-checked.
        """
        verbosePrint("DesignSpaceManager::allSourceRows")
        rows = []
        for font, _ in self._allFontLocs:
            v, h = self._fontStems.get(id(font), (None, None))
            rows.append({
                "selected": id(font) in activeFontIdSet,
                "name":     self._fontLabels.get(id(font), "?"),
                "vStem":    v if v is not None else "—",
                "hStem":    h if h is not None else "—",
            })
        return rows

    def fontsFromTableItems(self, items: list) -> list:
        """Return font objects whose corresponding table rows are enabled."""
        verbosePrint("DesignSpaceManager::fontsFromTableItems")
        return [
            font
            for (font, _), item in zip(self._allFontLocs, items)
            if item.get("selected", False)
        ]

    def enabledSourceFiles(self, items: list) -> list:
        """Return DS-relative filenames for the enabled rows in the source table."""
        verbosePrint("DesignSpaceManager::enabledSourceFiles")
        return [
            self._fontSourceFiles[id(font)]
            for (font, _), item in zip(self._allFontLocs, items)
            if item.get("selected", False)
            and self._fontSourceFiles.get(id(font))
        ]

    def sourceFilesFontIdSet(self, filenames: list) -> set:
        """Return id(font) values whose source filename is in filenames."""
        verbosePrint("DesignSpaceManager::sourceFilesFontIdSet")
        fnSet = set(filenames)
        return {
            id(f) for f, _ in self._allFontLocs
            if self._fontSourceFiles.get(id(f)) in fnSet
        }

    # ---- Scaler management ----

    def _stemRangeForFonts(self, fonts: list) -> tuple:
        """Return (minV, maxV, minH, maxH) for the given fonts.

        Filters out None and zero measurements; falls back to (100, 100, 50, 50)
        when no valid data exists.
        """
        verbosePrint("DesignSpaceManager::_stemRangeForFonts")
        vStems = [
            v for f in fonts
            for v, _h in [self._fontStems.get(id(f), (None, None))]
            if v is not None and v > 0
        ]
        hStems = [
            h for f in fonts
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
        """Rebuild the active MutatorScaleEngine from the given font list.

        Should be called whenever the enabled source selection changes.
        Requires ≥2 fonts; sets _activeScaler to None if fewer are provided.
        """
        verbosePrint("DesignSpaceManager::rebuildScalerFromFonts")
        if len(fonts) < 2:
            self._activeScaler = None
            return
        try:
            self._activeScaler    = MutatorScaleEngine(fonts)
            self._activeStemRange = self._stemRangeForFonts(fonts)
        except Exception as e:
            print(f"Transmutor: MutatorScale rebuild failed: {e}")
            self._activeScaler = None

    # ---- Interpolation ----

    def compensatedInterpolateGlyph(self,
                                     glyphName: str,
                                     normalizedLocation: dict,
                                     scaleX: float,
                                     scaleY: float,
                                     stemV: float = 0.0,
                                     stemH: float = 0.0):
        """Return a density-compensated scaled glyph via MutatorScaleEngine.

        The active scaler (set by subspace/source-table selection) compensates
        for weight changes that pure affine scaling would introduce.  The stem
        sliders drive which weight within the enabled sources is targeted:
            stemV/H = 0.0  → thinnest enabled source
            stemV/H = 1.0  → thickest enabled source
        Values outside [0, 1] extrapolate beyond the source range.

        The returned glyph is already at the requested scale.  The caller
        should apply only an offset transform.

        Falls back to plain UFOOperator interpolation if the scaler is
        unavailable or fails.

        Args:
            glyphName:          Name of the glyph to interpolate.
            normalizedLocation: {axisTag: normalizedValue} for all axes.
            scaleX:             Horizontal scale factor (1.0 = no change).
            scaleY:             Vertical scale factor   (1.0 = no change).
            stemV:              Normalised V-stem target position.
            stemH:              Normalised H-stem target position.

        Returns:
            (scaledGlyph, designLocation) tuple, or None on failure.
        """
        verbosePrint("DesignSpaceManager::compensatedInterpolateGlyph")
        if not self.isLoaded:
            print("Transmutor: no designspace loaded.")
            return None
        if glyphName not in self._defaultFont:
            print(f"Transmutor: '{glyphName}' not found in default source.")
            return None

        scaler = self._activeScaler
        if scaler is None:
            # Fall back: plain interpolation, no scale compensation
            return self._plainInterpolate(glyphName, normalizedLocation)

        minV, maxV, minH, maxH = self._activeStemRange
        targetV = minV + stemV * (maxV - minV) if maxV != minV else minV
        targetH = minH + stemH * (maxH - minH) if maxH != minH else minH
        upm     = self._defaultFont.info.unitsPerEm or 1000

        # Build the design location so the caller can store it in metadata.
        tagToAxis    = {a.tag: a for a in self._op.doc.axes}
        designLoc    = {}
        for tag, normVal in normalizedLocation.items():
            axis = tagToAxis.get(tag)
            if axis is not None:
                designLoc[axis.name] = _normalizedToDesign(axis, normVal)

        try:
            scaler.set({
                "width":           scaleX / scaleY if scaleY != 0 else 1.0,
                "targetHeight":    upm * scaleY,
                "referenceHeight": upm,
            })
            scaledGlyph = scaler.getScaledGlyph(glyphName, (targetV, targetH))
        except Exception as e:
            print(f"Transmutor: MutatorScale failed for '{glyphName}': {e}")
            return self._plainInterpolate(glyphName, normalizedLocation)

        if scaledGlyph is None:
            return self._plainInterpolate(glyphName, normalizedLocation)

        return scaledGlyph, designLoc

    def _plainInterpolate(self, glyphName: str, normalizedLocation: dict):
        """Interpolate glyphName at normalizedLocation without scale compensation.

        Builds a design-space location from all axes in the loaded designspace
        and delegates to UFOOperator.makeOneGlyph.

        Returns (mathGlyph, designLocation) or None on failure.
        """
        verbosePrint("DesignSpaceManager::_plainInterpolate")
        tagToAxis = {a.tag: a for a in self._op.doc.axes}
        designLoc = {}
        for tag, normVal in normalizedLocation.items():
            axis = tagToAxis.get(tag)
            if axis is not None:
                designLoc[axis.name] = _normalizedToDesign(axis, normVal)

        mathGlyph = self._op.makeOneGlyph(
            glyphName, designLoc, decomposeComponents=False
        )
        if mathGlyph is None:
            print(f"Transmutor: could not interpolate '{glyphName}' at {designLoc}")
            return None
        return mathGlyph, designLoc

    def defaultNormalizedLocation(self, fontPath: Optional[str] = None) -> dict:
        """Return the ideal default normalised location for fontPath.

        All axes default to 0.0 (the designspace default).  When fontPath
        matches a source in the loaded designspace, that source's position
        is used so the subspace and weight are pre-selected correctly.
        """
        verbosePrint("DesignSpaceManager::defaultNormalizedLocation")
        if not self.isLoaded:
            return {"wght": 0.0}

        result = {a.tag: 0.0 for a in self._op.doc.axes}

        if not fontPath:
            return result

        fontPath = os.path.normpath(os.path.abspath(fontPath))
        for source in self._dsSources:
            if not source.filename:
                continue
            srcPath = os.path.normpath(os.path.join(self._dsDir, source.filename))
            if srcPath == fontPath:
                for axis in self._op.doc.axes:
                    if axis.name in (source.location or {}):
                        userVal = axis.map_backward(source.location[axis.name])
                        result[axis.tag] = _userToNormalized(axis, userVal)
                break

        return result


# ---------------------------------------------------------------------------
# Controller — TransmutorController (EZUI Panel)
# ---------------------------------------------------------------------------

class TransmutorController(ezui.WindowController):
    """EZUI floating panel for Transmutor.

    Acts as both View (owns the EZPanel) and Controller (mediates UI ↔ model).
    TransmutorTool registers itself via setTool() so the controller can request
    Merz redraws after any block mutation.
    """

    def build(self):
        verbosePrint("TransmutorController::build")
        self._dsManager      = DesignSpaceManager()
        self._tool           = None
        self._selectedBlockID: Optional[str] = None
        # Normalised axis location — updated when a DS loads or a block is selected
        self._normLoc: dict  = {}

        content = """
        = TwoColumnForm
        : Designspace:
        (Choose a Designspace ...)  @designspacePopupButton
        : Subspace:
        (Default ...)               @subspacePopupButton
        : Sources:
        |--------------------------------------------|     @sourceFontTable
        | selected | name     | vStem    | hStem     |
        |----------|----------|----------|----------|
        |          |          |          |           |
        |--------------------------------------------|
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
            subspacePopupButton=dict(
                items=["—"],
            ),
            sourceFontTable=dict(
                columnDescriptions=[
                    dict(identifier="selected", title="",       width=20,
                         editable=True, cellDescription=dict(cellType="Checkbox")),
                    dict(identifier="name",     title="Source", editable=False),
                    dict(identifier="vStem",    title="V",      width=36, editable=False),
                    dict(identifier="hStem",    title="H",      width=36, editable=False),
                ],
                items=[],
                height=150,
                showColumnTitles=True,
                alternatingRowColors=True,
            ),
            glyphNameField=dict(value=DEFAULT_GLYPH_NAME),
            xField=dict(value="0", placeholder="X"),
            yField=dict(value="0", placeholder="Y"),
            scaleXSlider=dict(minValue=1,   maxValue=200, value=100),
            scaleYSlider=dict(minValue=1,   maxValue=200, value=100),
            stemVSlider =dict(minValue=-1.0, maxValue=2.0, value=0.0),
            stemHSlider =dict(minValue=-1.0, maxValue=2.0, value=0.0),
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

        if not self._dsManager.isLoaded and self._dsManager.paths:
            self.loadDesignSpace(self._dsManager.paths[0])

        self._syncAxisSliders()
        self._syncStemControls()
        self._refreshSubspacePopup()
        if self.w.getItem("lockScaleCheckbox").get():
            self.w.getItem("scaleYSlider").enable(False)

    def started(self):
        verbosePrint("TransmutorController::started")
        self.w.open()

    # ---- Tool registration ----

    def setTool(self, tool) -> None:
        """Register the active TransmutorTool instance for Merz redraw callbacks."""
        verbosePrint("TransmutorController::setTool")
        self._tool = tool

    # ---- Properties ----

    @property
    def dsManager(self) -> DesignSpaceManager:
        """The underlying DesignSpaceManager model."""
        verbosePrint("TransmutorController::dsManager")
        return self._dsManager

    @property
    def selectedBlockID(self) -> Optional[str]:
        """UUID of the currently selected block, or None."""
        verbosePrint("TransmutorController::selectedBlockID")
        return self._selectedBlockID

    @selectedBlockID.setter
    def selectedBlockID(self, value: Optional[str]):
        verbosePrint("TransmutorController::selectedBlockID setter")
        self._selectedBlockID = value

    @property
    def glyphName(self) -> str:
        """Current glyph name from the text field."""
        verbosePrint("TransmutorController::glyphName")
        return self.w.getItem("glyphNameField").get().strip()

    @property
    def normalizedLocation(self) -> dict:
        """Normalised axis location {axisTag: value} for the current state."""
        verbosePrint("TransmutorController::normalizedLocation")
        return dict(self._normLoc)

    @normalizedLocation.setter
    def normalizedLocation(self, loc: dict):
        verbosePrint("TransmutorController::normalizedLocation setter")
        self._normLoc = dict(loc)

    @property
    def offset(self) -> tuple:
        """Current (x, y) int offset, defaulting to 0 on parse failure."""
        verbosePrint("TransmutorController::offset")
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
        verbosePrint("TransmutorController::offset setter")
        self.w.getItem("xField").set(str(xy[0]))
        self.w.getItem("yField").set(str(xy[1]))

    @property
    def lockScale(self) -> bool:
        """Whether Scale X and Scale Y sliders are locked together."""
        verbosePrint("TransmutorController::lockScale")
        return bool(self.w.getItem("lockScaleCheckbox").get())

    @lockScale.setter
    def lockScale(self, value: bool):
        verbosePrint("TransmutorController::lockScale setter")
        self.w.getItem("lockScaleCheckbox").set(1 if value else 0)
        self.w.getItem("scaleYSlider").enable(not value)

    @property
    def scalePercent(self) -> tuple:
        """Current (scaleX%, scaleY%) from the scale sliders."""
        verbosePrint("TransmutorController::scalePercent")
        return (
            self.w.getItem("scaleXSlider").get(),
            self.w.getItem("scaleYSlider").get(),
        )

    @scalePercent.setter
    def scalePercent(self, xy: tuple):
        verbosePrint("TransmutorController::scalePercent setter")
        self.w.getItem("scaleXSlider").set(xy[0])
        self.w.getItem("scaleYSlider").set(xy[1])

    @property
    def stemRatios(self) -> tuple:
        """Current (stemV, stemH) normalised positions (0=thinnest, 1=thickest)."""
        verbosePrint("TransmutorController::stemRatios")
        return (
            self.w.getItem("stemVSlider").get(),
            self.w.getItem("stemHSlider").get(),
        )

    @stemRatios.setter
    def stemRatios(self, vh: tuple):
        verbosePrint("TransmutorController::stemRatios setter")
        self.w.getItem("stemVSlider").set(vh[0])
        self.w.getItem("stemHSlider").set(vh[1])

    @property
    def linkStems(self) -> bool:
        """Whether V and H stem sliders move together."""
        verbosePrint("TransmutorController::linkStems")
        return bool(self.w.getItem("linkStemsCheckbox").get())

    @linkStems.setter
    def linkStems(self, value: bool):
        verbosePrint("TransmutorController::linkStems setter")
        self.w.getItem("linkStemsCheckbox").set(1 if value else 0)
        if self._dsManager.isLoaded:
            self.w.getItem("stemHSlider").enable(not value)

    # ---- Designspace management ----

    def addDesignSpace(self, path: str) -> None:
        """Register path and rebuild the designspace dropdown."""
        verbosePrint("TransmutorController::addDesignSpace")
        self._dsManager.registerPath(path)
        self._refreshPopup()

    def loadDesignSpace(self, path: str) -> None:
        """Load path into the model and synchronise all dependent UI state."""
        verbosePrint("TransmutorController::loadDesignSpace")
        path = os.path.normpath(os.path.abspath(path))
        if not self._dsManager.load(path):
            return
        idx = self._dsManager.indexForPath(path)
        if idx >= 0:
            self.w.getItem("designspacePopupButton").set(idx)
        self._syncAxisSliders()
        self._syncStemControls()
        self._refreshSubspacePopup()

    def _refreshSourceTable(self, subspaceIdx: int) -> None:
        """Populate the sources table and rebuild the active scaler.

        Fonts in the chosen subspace are pre-enabled; all others are disabled.
        When no subspaces exist (wght-only DS) every font is enabled.
        """
        verbosePrint("TransmutorController::_refreshSourceTable")
        labels = self._dsManager.subspaceLabels()
        if labels:
            activeFontIdSet = self._dsManager.subspaceFontIdSet(subspaceIdx)
            fonts           = self._dsManager.fontsForSubspace(subspaceIdx)
        else:
            fonts           = self._dsManager.allFonts
            activeFontIdSet = {id(f) for f in fonts}
        rows = self._dsManager.allSourceRows(activeFontIdSet)
        self.w.getItem("sourceFontTable").set(rows)
        self._dsManager.rebuildScalerFromFonts(fonts)

    def _refreshSubspacePopup(self) -> None:
        """Populate the subspace dropdown and select the best match for _normLoc.

        Disabled when the designspace has only one subspace.
        """
        verbosePrint("TransmutorController::_refreshSubspacePopup")
        labels = self._dsManager.subspaceLabels()
        popup  = self.w.getItem("subspacePopupButton")
        if labels:
            popup.setItems(labels)
            popup.enable(True)
            def _dist(i):
                loc = self._dsManager.subspaceNormLoc(i)
                return sum(
                    (loc.get(t, 0.0) - self._normLoc.get(t, 0.0)) ** 2
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
        """Update _normLoc with the chosen subspace's non-wght axis values.

        Preserves the current wght value so the weight position is not lost
        when the user switches subspace.
        """
        verbosePrint("TransmutorController::_applySubspace")
        nonWghtLoc = self._dsManager.subspaceNormLoc(index)
        wghtVal    = self._normLoc.get("wght", 0.0)
        self._normLoc.update(nonWghtLoc)
        self._normLoc["wght"] = wghtVal
        self._refreshSourceTable(index)
        if updateBlock:
            self.updateSelectedBlock()

    def _syncAxisSliders(self) -> None:
        """Set _normLoc from the current font's source position in the loaded DS."""
        verbosePrint("TransmutorController::_syncAxisSliders")
        self._normLoc = dict(
            self._dsManager.defaultNormalizedLocation(self._currentFontPath())
        )

    def _syncStemControls(self) -> None:
        """Enable or disable stem sliders based on whether a DS is loaded."""
        verbosePrint("TransmutorController::_syncStemControls")
        active = self._dsManager.isLoaded
        self.w.getItem("stemVSlider").enable(active)
        linked = active and bool(self.w.getItem("linkStemsCheckbox").get())
        self.w.getItem("stemHSlider").enable(active and not linked)
        self.w.getItem("linkStemsCheckbox").enable(active)

    def _currentFontPath(self) -> Optional[str]:
        """Return the file path of the current RoboFont font, or None."""
        verbosePrint("TransmutorController::_currentFontPath")
        try:
            font = CurrentFont()
            return font.path if font else None
        except Exception:
            return None

    def _refreshPopup(self) -> None:
        """Rebuild the designspace dropdown from the manager's path list."""
        verbosePrint("TransmutorController::_refreshPopup")
        items = self._dsManager.popupLabels()
        items += [NSMenuItem.separatorItem(), "Choose a Designspace ..."]
        self.w.getItem("designspacePopupButton").setItems(items)

    def _openDesignSpace(self, path: Optional[str] = None) -> None:
        """Show a file picker (if no path given), then register and load."""
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

    # ---- Block operations ----

    def _enabledSourceFiles(self) -> list:
        """Return DS-relative filenames for the currently enabled source rows."""
        verbosePrint("TransmutorController::_enabledSourceFiles")
        return self._dsManager.enabledSourceFiles(
            self.w.getItem("sourceFontTable").get()
        )

    def updateSelectedBlock(self) -> None:
        """Re-interpolate and replace the currently selected block in-place.

        No-op when no block is selected or no glyph is open.
        """
        verbosePrint("TransmutorController::updateSelectedBlock")
        if self._selectedBlockID is None:
            return
        g = CurrentGlyph()
        if g is None:
            return
        normLoc        = self.normalizedLocation
        sx, sy         = self.scalePercent
        stemV, stemH   = self.stemRatios
        ox, oy         = self.offset

        result = self._dsManager.compensatedInterpolateGlyph(
            self.glyphName, normLoc, sx / 100.0, sy / 100.0,
            stemV=stemV, stemH=stemH,
        )
        if result is None:
            return
        mathGlyph, _designLoc = result

        # Preserve the existing block color when replacing
        existingColor = None
        blockInfo = _getBlocksFromGlyph(g).get(self._selectedBlockID)
        if blockInfo:
            existingColor = blockInfo["meta"].get(BLOCK_COLOR_KEY)

        _removeBlockContours(g, self._selectedBlockID)
        _insertTaggedContours(
            g, mathGlyph, normLoc,
            self._dsManager.currentRelPath, self.glyphName,
            transform=(1, 0, 0, 1, ox, oy),
            color=existingColor,
            blockID=self._selectedBlockID,
            scalePercent=(sx, sy),
            stemRatioV=stemV, stemRatioH=stemH,
            enabledSources=self._enabledSourceFiles(),
        )
        self._requestRedraw(g)

    def insertNewBlock(self) -> None:
        """Insert a new block with the current panel settings.

        Generates a fresh block ID and immediately selects the new block.
        """
        verbosePrint("TransmutorController::insertNewBlock")
        g = CurrentGlyph()
        if g is None:
            return
        normLoc       = self.normalizedLocation
        sx, sy        = self.scalePercent
        stemV, stemH  = self.stemRatios
        ox, oy        = self.offset

        result = self._dsManager.compensatedInterpolateGlyph(
            self.glyphName, normLoc, sx / 100.0, sy / 100.0,
            stemV=stemV, stemH=stemH,
        )
        if result is None:
            return
        mathGlyph, _designLoc = result

        g.prepareUndo("Insert Block")
        newIds = _insertTaggedContours(
            g, mathGlyph, normLoc,
            self._dsManager.currentRelPath, self.glyphName,
            transform=(1, 0, 0, 1, ox, oy),
            scalePercent=(sx, sy),
            stemRatioV=stemV, stemRatioH=stemH,
            enabledSources=self._enabledSourceFiles(),
        )
        g.performUndo()

        if newIds:
            objLibs = g.lib.get(OBJECT_LIBS_KEY, {})
            self._selectedBlockID = objLibs.get(newIds[0], {}).get(BLOCK_ID_KEY)
        self._requestRedraw(g)

    def removeSelectedBlock(self) -> None:
        """Remove the currently selected block and clear the selection."""
        verbosePrint("TransmutorController::removeSelectedBlock")
        if self._selectedBlockID is None:
            return
        g = CurrentGlyph()
        if g is None:
            return
        g.prepareUndo("Remove Block")
        _removeBlockContours(g, self._selectedBlockID)
        _scrubOrphanedObjectLibs(g)
        g.performUndo()
        self._selectedBlockID = None
        self._requestRedraw(g)

    # ---- UI state ----

    def setDefaults(self) -> None:
        """Reset all panel controls to initial values and deselect any block."""
        verbosePrint("TransmutorController::setDefaults")
        self._selectedBlockID = None
        self.w.getItem("glyphNameField").set(DEFAULT_GLYPH_NAME)
        self._normLoc = dict(
            self._dsManager.defaultNormalizedLocation(self._currentFontPath())
        )
        self.scalePercent = (100, 100)
        self.offset       = (0, 0)
        self.stemRatios   = (0.0, 0.0)
        self.w.getItem("linkStemsCheckbox").set(1)
        self._syncStemControls()
        self.lockScale = True

    def selectBlock(self, meta: dict) -> None:
        """Restore all panel controls from a block's stored lib metadata.

        Switches the active designspace and subspace to match the block,
        restores the enabled source selection, and sets all sliders/fields
        to the values that were in use when the block was last inserted.
        """
        verbosePrint("TransmutorController::selectBlock")
        self._selectedBlockID = meta.get(BLOCK_ID_KEY)

        # Switch to the block's designspace
        dsRelPath = meta.get(CONTOUR_SOURCE_KEY)
        if dsRelPath:
            dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, dsRelPath))
            self.addDesignSpace(dsAbsPath)
            self.loadDesignSpace(dsAbsPath)

        self.w.getItem("glyphNameField").set(
            meta.get(f"{LIB_PREFIX}.glyphName", DEFAULT_GLYPH_NAME)
        )
        self._normLoc = dict(meta.get(f"{LIB_PREFIX}.normalizedLocation", {}))

        # Sync subspace popup to the stored normalised location
        labels = self._dsManager.subspaceLabels()
        if labels:
            def _dist(i):
                loc = self._dsManager.subspaceNormLoc(i)
                return sum(
                    (loc.get(t, 0.0) - self._normLoc.get(t, 0.0)) ** 2
                    for t in loc
                )
            bestIdx = min(range(len(labels)), key=_dist)
            self.w.getItem("subspacePopupButton").set(bestIdx)

        # Restore enabled sources (overrides subspace defaults)
        storedSources = meta.get(f"{LIB_PREFIX}.enabledSources", [])
        if storedSources:
            fontIdSet = self._dsManager.sourceFilesFontIdSet(storedSources)
            if fontIdSet:
                rows = self._dsManager.allSourceRows(fontIdSet)
                self.w.getItem("sourceFontTable").set(rows)
                fonts = [
                    f for f, _ in self._dsManager._allFontLocs
                    if id(f) in fontIdSet
                ]
                self._dsManager.rebuildScalerFromFonts(fonts)

        # Restore stem slider positions
        stemRatioV = meta.get(f"{LIB_PREFIX}.stemRatioV", 0.0)
        stemRatioH = meta.get(f"{LIB_PREFIX}.stemRatioH", 0.0)
        self.w.getItem("stemVSlider").set(stemRatioV)
        self.w.getItem("stemHSlider").set(stemRatioH)
        linked = abs(stemRatioV - stemRatioH) < 1e-6
        self.w.getItem("linkStemsCheckbox").set(1 if linked else 0)
        self._syncStemControls()

        # Restore scale sliders from scalePercent metadata
        scalePercent = meta.get(f"{LIB_PREFIX}.scalePercent")
        if scalePercent and len(scalePercent) >= 2:
            sx, sy = float(scalePercent[0]), float(scalePercent[1])
        else:
            t  = meta.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
            sx = round(t[0] * 100)
            sy = round(t[3] * 100)
        locked = abs(sx - sy) < 0.5
        self.w.getItem("scaleXSlider").set(sx)
        self.w.getItem("scaleYSlider").set(sy)
        self.w.getItem("lockScaleCheckbox").set(1 if locked else 0)
        self.w.getItem("scaleYSlider").enable(not locked)

        # Restore offset from transform
        t  = meta.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
        self.offset = (int(t[4]), int(t[5]))

    def snapToIntegers(self) -> None:
        """Round scale percentages and offset to clean integers.

        Called after a handle-drag ends to avoid fractional values persisting.
        """
        verbosePrint("TransmutorController::snapToIntegers")
        sx, sy = self.scalePercent
        self.scalePercent = (int(round(sx)), int(round(sy)))
        self.offset = self.offset  # offset getter already rounds

    # ---- Redraw delegation ----

    def _requestRedraw(self, glyph) -> None:
        """Ask the registered tool to repaint Merz layers for glyph."""
        verbosePrint("TransmutorController::_requestRedraw")
        if self._tool is not None:
            self._tool.drawBlocks(glyph)
            self._tool.drawHandles(glyph, self._selectedBlockID)

    # ---- EZUI callbacks ----

    def designspacePopupButtonCallback(self, sender):
        """Open the file picker or switch to the selected designspace."""
        verbosePrint("TransmutorController::designspacePopupButtonCallback")
        if sender.getItem() == "Choose a Designspace ...":
            self._openDesignSpace()
        else:
            idx   = sender.get()
            paths = self._dsManager.paths
            if 0 <= idx < len(paths):
                self.loadDesignSpace(paths[idx])

    def subspacePopupButtonCallback(self, sender):
        """Update the non-wght axes of _normLoc to match the chosen subspace."""
        verbosePrint("TransmutorController::subspacePopupButtonCallback")
        self._applySubspace(sender.get())

    def sourceFontTableEditCallback(self, sender):
        """Rebuild the active scaler when the user toggles source checkboxes."""
        verbosePrint("TransmutorController::sourceFontTableEditCallback")
        fonts = self._dsManager.fontsFromTableItems(sender.get())
        self._dsManager.rebuildScalerFromFonts(fonts)
        self.updateSelectedBlock()

    def glyphNameFieldCallback(self, sender):
        verbosePrint("TransmutorController::glyphNameFieldCallback")
        self.updateSelectedBlock()

    def xFieldCallback(self, sender):
        verbosePrint("TransmutorController::xFieldCallback")
        self.updateSelectedBlock()

    def yFieldCallback(self, sender):
        verbosePrint("TransmutorController::yFieldCallback")
        self.updateSelectedBlock()

    def scaleXSliderCallback(self, sender):
        verbosePrint("TransmutorController::scaleXSliderCallback")
        if self.lockScale:
            self.w.getItem("scaleYSlider").set(sender.get())
        self.updateSelectedBlock()

    def scaleYSliderCallback(self, sender):
        verbosePrint("TransmutorController::scaleYSliderCallback")
        self.updateSelectedBlock()

    def lockScaleCheckboxCallback(self, sender):
        verbosePrint("TransmutorController::lockScaleCheckboxCallback")
        locked = bool(sender.get())
        self.w.getItem("scaleYSlider").enable(not locked)
        if locked:
            self.w.getItem("scaleYSlider").set(self.w.getItem("scaleXSlider").get())
        self.updateSelectedBlock()

    def stemVSliderCallback(self, sender):
        verbosePrint("TransmutorController::stemVSliderCallback")
        if self.linkStems:
            self.w.getItem("stemHSlider").set(sender.get())
        self.updateSelectedBlock()

    def stemHSliderCallback(self, sender):
        verbosePrint("TransmutorController::stemHSliderCallback")
        self.updateSelectedBlock()

    def linkStemsCheckboxCallback(self, sender):
        verbosePrint("TransmutorController::linkStemsCheckboxCallback")
        linked = bool(sender.get())
        self.w.getItem("stemHSlider").enable(not linked)
        if linked:
            self.w.getItem("stemHSlider").set(self.w.getItem("stemVSlider").get())
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
    """Glyph-view event tool that drives the TransmutorController panel.

    Handles:
    - Panel lifecycle (show on activate, hide on deactivate).
    - Mouse events: block selection, drag-to-move, handle-to-scale.
    - Merz layer rendering: block outlines (background) and handles (foreground).
    """

    _controller: Optional[TransmutorController] = None

    # Drag state — offset movement
    _dragging:          bool            = False
    _dragStartPoint:    Optional[tuple] = None
    _dragStartOffset:   Optional[tuple] = None

    # Drag state — handle scaling
    _scalingHandle:      Optional[str]   = None
    _scalingAnchor:      Optional[tuple] = None
    _scalingHandleXY:    Optional[tuple] = None
    _scalingStartScaleX: Optional[float] = None
    _scalingStartScaleY: Optional[float] = None
    _scalingStartOffset: Optional[tuple] = None

    def getToolbarTip(self) -> str:
        verbosePrint("TransmutorTool::getToolbarTip")
        return "Transmutor"

    # ---- Lifecycle ----

    def becomeActive(self) -> None:
        """Show (or create) the panel, register as the active tool, and draw blocks."""
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
        """Hide the panel, scrub stale lib entries, and clear all Merz layers."""
        verbosePrint("TransmutorTool::becomeInactive")
        g = CurrentGlyph()
        if g is not None:
            _scrubOrphanedObjectLibs(g)
        if self._controller is not None:
            self._controller.setTool(None)
            self._controller.w.hide()
        self.extensionContainer(_MERZ_BG_ID, location="background", clear=True)
        self._clearHandles()

    def currentGlyphChanged(self) -> None:
        """Deselect, scrub orphans, register DS paths, and redraw when glyph changes."""
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
        """Add any DS paths referenced by the glyph's blocks to the panel dropdown."""
        verbosePrint("TransmutorTool::_registerGlyphDesignSpaces")
        if self._controller is None:
            return
        for info in _getBlocksFromGlyph(glyph).values():
            dsRelPath = info["meta"].get(CONTOUR_SOURCE_KEY)
            if dsRelPath:
                dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, dsRelPath))
                self._controller.addDesignSpace(dsAbsPath)

    # ---- Merz drawing ----

    def drawBlocks(self, glyph) -> None:
        """Repaint background stroke outlines for every block in glyph."""
        verbosePrint("TransmutorTool::drawBlocks")
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
                layer.appendFilter(dict(
                    name="blockBlur",
                    filterType="gaussianBlur",
                    radius=BLOCK_BLUR_RADIUS,
                ))
                contour.draw(layer.getPen())

    def drawHandles(self, glyph, blockID: Optional[str]) -> None:
        """Draw a dashed bounding box and 8 square handles for the selected block."""
        verbosePrint("TransmutorTool::drawHandles")
        container = self.extensionContainer(
            _MERZ_FG_ID, location="foreground", clear=True
        )
        if glyph is None or blockID is None:
            return
        info = _getBlocksFromGlyph(glyph).get(blockID)
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

        # 8 square handles at corners and edge midpoints
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
        """Remove all foreground selection-handle layers."""
        verbosePrint("TransmutorTool::_clearHandles")
        self.extensionContainer(_MERZ_FG_ID, location="foreground", clear=True)

    # ---- Hit testing ----

    def _hitTestHandles(self, point, glyph, blockID) -> Optional[str]:
        """Return the compass name of the handle at point, or None.

        Hit radius scales with the view zoom so handles remain easy to click
        at any zoom level.
        """
        verbosePrint("TransmutorTool::_hitTestHandles")
        if glyph is None or blockID is None:
            return None
        info = _getBlocksFromGlyph(glyph).get(blockID)
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

    def _hitTestBlocks(self, point, glyph) -> Optional[tuple]:
        """Return (blockID, meta) for the first block whose bounds contain point.

        Returns None when no block is hit.
        """
        verbosePrint("TransmutorTool::_hitTestBlocks")
        for blockID, info in _getBlocksFromGlyph(glyph).items():
            bounds = _unionBounds(info["contours"])
            if _pointInBounds(point, bounds):
                return blockID, info["meta"]
        return None

    # ---- Mouse events ----

    def mouseDown(self, point, clickCount: int) -> None:
        """Dispatch to handle-drag, block-select, or deselect on miss."""
        verbosePrint("TransmutorTool::mouseDown")
        if self._controller is None:
            return
        g = CurrentGlyph()
        if g is None:
            return

        # Priority 1: handle hit on the already-selected block
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
        """Capture all state needed to scale from a handle drag."""
        verbosePrint("TransmutorTool::_beginScaleDrag")
        glyph.prepareUndo("Scale Block")
        info   = _HANDLE_SCALING_INFO[handleName]
        bounds = _unionBounds(_getBlocksFromGlyph(glyph)[blockID]["contours"])
        xMin, yMin, xMax, yMax = bounds

        hu, hv = info["handleNorm"]
        au, av = info["anchorNorm"]
        self._scalingHandle     = handleName
        self._scalingHandleXY   = (_lerp(xMin, xMax, hu), _lerp(yMin, yMax, hv))
        self._scalingAnchor     = (_lerp(xMin, xMax, au), _lerp(yMin, yMax, av))
        sx, sy = self._controller.scalePercent
        self._scalingStartScaleX = float(sx)
        self._scalingStartScaleY = float(sy)
        ox, oy = self._controller.offset
        self._scalingStartOffset = (float(ox), float(oy))

    def _beginOffsetDrag(self, point, glyph) -> None:
        """Capture state for a block-body offset drag."""
        verbosePrint("TransmutorTool::_beginOffsetDrag")
        glyph.prepareUndo("Move Block")
        self._dragging        = True
        self._dragStartPoint  = (point.x, point.y)
        ox, oy = self._controller.offset
        self._dragStartOffset = (ox, oy)

    def mouseDragged(self, point, delta) -> None:
        """Route to scale-drag or offset-drag handler."""
        verbosePrint("TransmutorTool::mouseDragged")
        if self._controller is None:
            return
        if self._scalingHandle is not None:
            self._handleScaleDrag(point)
        elif self._dragging:
            self._handleOffsetDrag(point)

    def _handleScaleDrag(self, point) -> None:
        """Recompute scale and offset from the current handle-drag position.

        Keeps the anchor handle stationary while the dragged handle moves.
        Shift constrains proportional (X=Y) scaling on corner handles.
        All values remain floating-point until mouseUp snaps to integers.
        """
        verbosePrint("TransmutorTool::_handleScaleDrag")
        info = _HANDLE_SCALING_INFO[self._scalingHandle]
        ax, ay   = self._scalingAnchor
        hx, hy   = self._scalingHandleXY
        dx0, dy0 = self._scalingStartOffset
        sx0      = self._scalingStartScaleX
        sy0      = self._scalingStartScaleY

        distH = hx - ax
        distV = hy - ay
        factorH = ((point.x - ax) / distH) if (info["scaleH"] and distH != 0) else 1.0
        factorV = ((point.y - ay) / distV) if (info["scaleV"] and distV != 0) else 1.0

        # Shift: constrain to proportional scaling on corner handles
        if self.shiftDown and info["scaleH"] and info["scaleV"]:
            if abs(factorH - 1.0) >= abs(factorV - 1.0):
                factorV = factorH
            else:
                factorH = factorV

        newScaleX = max(1.0, min(200.0, sx0 * factorH))
        newScaleY = max(1.0, min(200.0, sy0 * factorV))

        # Pin the anchor: newOffset = anchor*(1 - factor) + oldOffset*factor
        newDx = ax * (1.0 - factorH) + dx0 * factorH if info["scaleH"] else dx0
        newDy = ay * (1.0 - factorV) + dy0 * factorV if info["scaleV"] else dy0

        self._controller.scalePercent = (newScaleX, newScaleY)
        self._controller.offset       = (f"{newDx:.4f}", f"{newDy:.4f}")
        self._controller.updateSelectedBlock()

    def _handleOffsetDrag(self, point) -> None:
        """Translate the selected block by dragging its bounding-box body.

        Shift constrains movement to the nearest 45° axis.
        """
        verbosePrint("TransmutorTool::_handleOffsetDrag")
        if self._dragStartPoint is None or self._dragStartOffset is None:
            return

        rawDx = point.x - self._dragStartPoint[0]
        rawDy = point.y - self._dragStartPoint[1]

        if self.shiftDown and (rawDx != 0 or rawDy != 0):
            magnitude    = math.hypot(rawDx, rawDy)
            angle        = math.atan2(rawDy, rawDx)
            snappedAngle = round(angle / (math.pi / 4)) * (math.pi / 4)
            rawDx        = magnitude * math.cos(snappedAngle)
            rawDy        = magnitude * math.sin(snappedAngle)

        newX = int(round(self._dragStartOffset[0] + rawDx))
        newY = int(round(self._dragStartOffset[1] + rawDy))
        self._controller.offset = (newX, newY)
        self._controller.updateSelectedBlock()

    def mouseUp(self, point) -> None:
        """End the active drag, snap to integers, and close the undo item."""
        verbosePrint("TransmutorTool::mouseUp")
        wasScaling  = self._scalingHandle is not None
        wasDragging = self._dragging
        self._clearDragState()
        if wasScaling and self._controller is not None:
            self._controller.snapToIntegers()
            self._controller.updateSelectedBlock()
        if wasScaling or wasDragging:
            g = CurrentGlyph()
            if g is not None:
                g.performUndo()

    def _clearDragState(self) -> None:
        """Reset all transient drag state to None/False."""
        verbosePrint("TransmutorTool::_clearDragState")
        self._dragging           = False
        self._dragStartPoint     = None
        self._dragStartOffset    = None
        self._scalingHandle      = None
        self._scalingAnchor      = None
        self._scalingHandleXY    = None
        self._scalingStartScaleX = None
        self._scalingStartScaleY = None
        self._scalingStartOffset = None


installTool(TransmutorTool())
