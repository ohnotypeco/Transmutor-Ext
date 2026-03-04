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
VERBOSE = True

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
    """Return {blockID: {"contours": [...], "meta": {...}}} for tagged contours."""
    verbosePrint("_getBlocksFromGlyph")
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
    verbosePrint("_removeBlockContours")
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


def _insertTaggedContours(glyph, glyphMathObj,
                          normalizedLocation: dict, dsRelPath: str,
                          glyphName: str,
                          offset: tuple = (0, 0),
                          color: list = None,
                          blockID: str = None,
                          scalePercent: tuple = None,
                          stemV: float = 100.0,
                          stemH: float = 50.0,
                          linkStems: bool = True,
                          enabledSources: list = None) -> list:
    """Draw interpolated contours into glyph, apply offset, tag in objectLibs.

    normalizedLocation: {axisTag: normalizedValue, ...}
    Returns list of new contour identifiers.
    """
    verbosePrint("_insertTaggedContours")
    if color is None:
        color = _generateBlockColor(glyph)
    if blockID is None:
        blockID = str(uuid.uuid4())

    countBefore = len(glyph)
    if hasattr(glyphMathObj, 'drawPoints'):
        glyphMathObj.drawPoints(glyph.getPointPen())
    else:
        glyphMathObj.draw(glyph.getPen())

    dx, dy = offset
    if dx or dy:
        t = (1, 0, 0, 1, dx, dy)
        for contour in glyph.contours[countBefore:]:
            contour.transformBy(t)

    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    meta = {
        CONTOUR_SOURCE_KEY: dsRelPath,
        BLOCK_ID_KEY: blockID,
        BLOCK_COLOR_KEY: color,
        f"{LIB_PREFIX}.glyphName": glyphName,
        f"{LIB_PREFIX}.normalizedLocation": dict(normalizedLocation),
        f"{LIB_PREFIX}.offset": list(offset),
    }
    if scalePercent is not None:
        meta[f"{LIB_PREFIX}.scalePercent"] = list(scalePercent)
        meta[f"{LIB_PREFIX}.stemV"] = stemV
        meta[f"{LIB_PREFIX}.stemH"] = stemH
        meta[f"{LIB_PREFIX}.linkStems"] = linkStems
    if enabledSources is not None:
        meta[f"{LIB_PREFIX}.enabledSources"] = list(enabledSources)
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
        self._paths = []         # ordered list of absolute, normalised paths
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
        self._fontLabels = {}   # id(font) -> str
        self._fontStems = {}    # id(font) -> (vstem, hstem)
        self._fontSourceFiles = {}  # id(font) -> DS-relative source filename
        # Active scaler, rebuilt from currently-enabled fonts
        self._activeScaler = None
        # (minV, maxV, minH, maxH) stem range of currently-enabled fonts
        self._activeStemRange = (100, 100, 50, 50)

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
        return (self._op is not None
                and self._defaultFont is not None
                and self._wghtAxis is not None)

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
            self._wghtAxis = next(
                (a for a in ds.axes if a.tag == "wght"), None)
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
                nonWghtAxisNames = {a.name for a in nonWghtAxes}
                wghtAxisName = self._wghtAxis.name if self._wghtAxis else None
                wghtAxisDefault = (
                    self._wghtAxis.map_forward(self._wghtAxis.default)
                    if self._wghtAxis else 0
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
                        label = os.path.splitext(
                            os.path.basename(src.filename))[0]
                    else:
                        label = "?"
                    self._fontLabels[id(font)] = label
                    self._fontSourceFiles[id(
                        font)] = src.filename if src else None
                    try:
                        v, h = getRefStems(font)
                        # Discard implausible zero measurements
                        self._fontStems[id(font)] = (
                            v if v > 0 else None,
                            h if h > 0 else None,
                        )
                    except Exception:
                        self._fontStems[id(font)] = (None, None)

                # Group fonts by non-wght design location.
                subspaceMap = {}
                for font, designLoc in allFontLocs:
                    nonWghtKey = tuple(sorted(
                        (name, designLoc.get(name, 0.0))
                        for name in nonWghtAxisNames
                    ))
                    subspaceMap.setdefault(
                        nonWghtKey, []).append((font, designLoc))

                for nonWghtKey, fontLocs in subspaceMap.items():
                    if len(fontLocs) < 2:
                        continue
                    fonts = [f for f, _ in fontLocs]

                    normNonWght = {}
                    for name, designVal in nonWghtKey:
                        axis = next(
                            (a for a in nonWghtAxes if a.name == name), None)
                        if axis is not None:
                            userVal = axis.map_backward(designVal)
                            normNonWght[axis.tag] = _userToNormalized(
                                axis, userVal)

                    refFont, _ = min(
                        fontLocs,
                        key=lambda fl: abs(
                            fl[1].get(wghtAxisName, 0) - wghtAxisDefault)
                        if wghtAxisName else 0,
                    )
                    subStems = self._fontStems.get(id(refFont), (None, None))

                    if nonWghtKey:
                        parts = []
                        for name, designVal in nonWghtKey:
                            axis = next(
                                (a for a in nonWghtAxes if a.name == name), None)
                            if axis is not None:
                                uVal = axis.map_backward(designVal)
                                valStr = str(int(uVal)) if uVal == int(
                                    uVal) else f"{uVal:.4g}"
                                parts.append(f"{axis.tag} {valStr}")
                        label = "  ".join(parts)
                    else:
                        label = "Default"
                    # Tuple format: (label, normNonWght, fonts_list, refStems)
                    # fonts_list holds original defcon.Font objects — no pre-built scaler.
                    self._subspaceScalers.append(
                        (label, normNonWght, fonts, subStems))

                # Activate the first subspace by default.
                if self._subspaceScalers:
                    _, _, initFonts, _initStems = self._subspaceScalers[0]
                    try:
                        self._activeScaler = MutatorScaleEngine(initFonts)
                        self._activeStemRange = self._stemRangeForFonts(
                            initFonts)
                    except Exception as e:
                        print(f"MutatorScale initial build failed: {e}")
                    print(
                        f"Transmutor: built {len(self._subspaceScalers)} subspace(s).")
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
            rows.append({
                "selected": id(font) in activeFontIdSet,
                "name": self._fontLabels.get(id(font), "?"),
                "vStem": vstem if vstem is not None else "—",
                "hStem": hstem if hstem is not None else "—",
            })
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
            id(f) for f, _ in self._allFontLocs
            if self._fontSourceFiles.get(id(f)) in fnSet
        }

    def _stemRangeForFonts(self, fonts: list) -> tuple:
        """Return (minV, maxV, minH, maxH) stem range for the given fonts.

        Filters out None / zero measurements.  Falls back to (100, 100, 50, 50)
        when no valid measurements exist.
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

    def compensatedInterpolateGlyph(self, glyphName: str,
                                    normalizedLocation: dict,
                                    scaleX: float, scaleY: float,
                                    targetStemV: float = 100.0,
                                    targetStemH: float = 50.0):
        """Return a scaled glyph using MutatorScale stem compensation, or None.

        targetStemV / targetStemH are actual stem unit values to target.

        The active MutatorScaleEngine (set by subspace / source-table checkboxes)
        interpolates between its sources to match the target stem thickness at
        the requested scale.  The returned glyph is already at the target scale;
        the caller should apply only the offset transform.

        Returns None when the scaler is unavailable or the glyph cannot be found.
        """
        verbosePrint("DesignSpaceManager::compensatedInterpolateGlyph")
        if not self.isLoaded:
            print("No designspace loaded — choose one from the popup.")
            return None
        if glyphName not in self._defaultFont:
            print(f"'{glyphName}' not found in default source.")
            return None

        scaler = self._activeScaler
        if scaler is None:
            print("No MutatorScale scaler available — enable at least 2 sources.")
            return None

        upm = self._defaultFont.info.unitsPerEm or 1000

        try:
            scaler.set({
                "width": scaleX / scaleY if scaleY != 0 else 1.0,
                "targetHeight": upm * scaleY,
                "referenceHeight": upm,
            })
            scaledGlyph = scaler.getScaledGlyph(
                glyphName, (targetStemV, targetStemH)
            )
        except Exception as e:
            print(f"MutatorScale compensation failed for '{glyphName}': {e}")
            return None

        if scaledGlyph is None:
            print(f"MutatorScale returned no glyph for '{glyphName}'.")
            return None
        return scaledGlyph

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
            srcPath = os.path.normpath(
                os.path.join(self._dsDir, source.filename)
            )
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
        self._selectedBlockID = None
        self._normLoc = {}  # normalised axis location, updated on DS load / block select
        self._offset = (0, 0)  # (dx, dy) offset, draggable in the glyph view

        content = """
        = TwoColumnForm
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
                        title="V",
                        width=36,
                        editable=False,
                    ),
                    dict(
                        identifier="hStem",
                        title="H",
                        width=36,
                        editable=False,
                    ),
                ],
                items=[],
                height=150,
                showColumnTitles=True,
                alternatingRowColors=True,
            ),
            glyphNameField=dict(value=DEFAULT_GLYPH_NAME),
            scaleXSlider=dict(minValue=1, maxValue=200, value=100),
            scaleYSlider=dict(minValue=1, maxValue=200, value=100),
            stemVSlider=dict(minValue=1, maxValue=400, value=100),
            stemHSlider=dict(minValue=1, maxValue=400, value=50),
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
        self._refreshSubspacePopup()
        # Apply initial lock-scale state
        if self.w.getItem("lockScaleCheckbox").get():
            self.w.getItem("scaleYSlider").enable(False)

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
    def selectedBlockID(self) -> str:
        """The ID of the currently selected block, or None."""
        verbosePrint("TransmutorController::selectedBlockID")
        return self._selectedBlockID

    @selectedBlockID.setter
    def selectedBlockID(self, value: str):
        verbosePrint("TransmutorController::selectedBlockID")
        self._selectedBlockID = value

    @property
    def glyphName(self) -> str:
        """Current glyph name from the text field."""
        verbosePrint("TransmutorController::glyphName")
        return self.w.getItem("glyphNameField").get().strip()

    @property
    def normalizedLocation(self) -> dict:
        """Normalised axis location, set when a DS loads or a block is selected.

        Returns {axisTag: normalizedValue} for all active axes.
        """
        verbosePrint("TransmutorController::normalizedLocation")
        return dict(self._normLoc)

    @normalizedLocation.setter
    def normalizedLocation(self, loc: dict):
        """Store the normalised axis location."""
        verbosePrint("TransmutorController::normalizedLocation")
        self._normLoc = dict(loc)

    @property
    def offset(self) -> tuple:
        """Current (x, y) offset as integers."""
        verbosePrint("TransmutorController::offset")
        return self._offset

    @offset.setter
    def offset(self, xy: tuple):
        """Store the offset. The block can be repositioned by dragging."""
        verbosePrint("TransmutorController::offset")
        self._offset = (int(round(float(xy[0]))), int(round(float(xy[1]))))

    @property
    def lockScale(self) -> bool:
        """Whether Scale X and Scale Y are locked together."""
        verbosePrint("TransmutorController::lockScale")
        return bool(self.w.getItem("lockScaleCheckbox").get())

    @lockScale.setter
    def lockScale(self, value: bool):
        verbosePrint("TransmutorController::lockScale")
        self.w.getItem("lockScaleCheckbox").set(1 if value else 0)
        self.w.getItem("scaleYSlider").enable(not value)

    @property
    def scalePercent(self) -> tuple:
        """Current (scaleX%, scaleY%) from the sliders."""
        verbosePrint("TransmutorController::scalePercent")
        return (
            self.w.getItem("scaleXSlider").get(),
            self.w.getItem("scaleYSlider").get(),
        )

    @scalePercent.setter
    def scalePercent(self, xy: tuple):
        """Set the scale sliders. Accepts int or float values."""
        verbosePrint("TransmutorController::scalePercent")
        self.w.getItem("scaleXSlider").set(xy[0])
        self.w.getItem("scaleYSlider").set(xy[1])

    @property
    def stemValues(self) -> tuple:
        """Current (stemV, stemH) as actual stem unit values."""
        verbosePrint("TransmutorController::stemValues")
        return (
            self.w.getItem("stemVSlider").get(),
            self.w.getItem("stemHSlider").get(),
        )

    @stemValues.setter
    def stemValues(self, vh: tuple):
        """Set the stem sliders to actual unit values."""
        verbosePrint("TransmutorController::stemValues")
        self.w.getItem("stemVSlider").set(vh[0])
        self.w.getItem("stemHSlider").set(vh[1])

    @property
    def linkStems(self) -> bool:
        """Whether V and H stem sliders are linked."""
        verbosePrint("TransmutorController::linkStems")
        return bool(self.w.getItem("linkStemsCheckbox").get())

    @linkStems.setter
    def linkStems(self, value: bool):
        verbosePrint("TransmutorController::linkStems")
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

    def loadDesignSpace(self, path: str) -> None:
        """Load a designspace and sync the popup selection and axis sliders."""
        verbosePrint("TransmutorController::loadDesignSpace")
        path = os.path.normpath(os.path.abspath(path))
        self._dsManager.load(path)
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
        """Splice the chosen subspace's non-wght values into _normLoc.

        Preserves the current wght value so the weight slider position is
        unaffected when the user just switches subspace.
        """
        verbosePrint("TransmutorController::_applySubspace")
        nonWghtLoc = self._dsManager.subspaceNormLoc(index)
        wghtVal = self._normLoc.get("wght", 0.0)
        self._normLoc.update(nonWghtLoc)
        # restore wght in case subspaceNormLoc included it
        self._normLoc["wght"] = wghtVal
        self._refreshSourceTable(index)
        if updateBlock:
            self.updateSelectedBlock()

    def _syncAxisSliders(self) -> None:
        """Update the stored normalised location based on the current font."""
        verbosePrint("TransmutorController::_syncAxisSliders")
        fontPath = self._currentFontPath()
        defaults = self._dsManager.defaultNormalizedLocation(fontPath)
        self._normLoc = dict(defaults)

    def _syncStemControls(self) -> None:
        """Enable/disable stem sliders and link checkbox based on DS load state."""
        verbosePrint("TransmutorController::_syncStemControls")
        active = self._dsManager.isLoaded
        self.w.getItem("stemVSlider").enable(active)
        linked = active and self.w.getItem("linkStemsCheckbox").get()
        self.w.getItem("stemHSlider").enable(active and not linked)
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
        if self._selectedBlockID is None:
            return
        g = CurrentGlyph()
        if g is None:
            return
        normLoc = self.normalizedLocation
        sx, sy = self.scalePercent
        stemV, stemH = self.stemValues
        ox, oy = self.offset

        result = self._dsManager.compensatedInterpolateGlyph(
            self.glyphName, normLoc, sx / 100.0, sy / 100.0,
            targetStemV=stemV, targetStemH=stemH,
        )
        if result is None:
            return
        mathGlyph = result

        # Preserve existing block color
        existingColor = None
        blocks = _getBlocksFromGlyph(g)
        blockInfo = blocks.get(self._selectedBlockID)
        if blockInfo:
            existingColor = blockInfo["meta"].get(BLOCK_COLOR_KEY)

        _removeBlockContours(g, self._selectedBlockID)
        _insertTaggedContours(
            g, mathGlyph, normLoc,
            self._dsManager.currentRelPath,
            self.glyphName, offset=(ox, oy),
            color=existingColor, blockID=self._selectedBlockID,
            scalePercent=(sx, sy),
            stemV=stemV, stemH=stemH,
            linkStems=self.linkStems,
            enabledSources=self._enabledSourceFiles(),
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
            self.glyphName, normLoc, sx / 100.0, sy / 100.0,
            targetStemV=stemV, targetStemH=stemH,
        )
        if result is None:
            return
        mathGlyph = result

        g.prepareUndo("Insert Block")
        newIds = _insertTaggedContours(
            g, mathGlyph, normLoc,
            self._dsManager.currentRelPath,
            self.glyphName, offset=(ox, oy),
            scalePercent=(sx, sy),
            stemV=stemV, stemH=stemH,
            linkStems=self.linkStems,
            enabledSources=self._enabledSourceFiles(),
        )
        g.performUndo()

        if newIds:
            objLibs = g.lib.get(OBJECT_LIBS_KEY, {})
            self._selectedBlockID = objLibs.get(
                newIds[0], {}).get(BLOCK_ID_KEY)
        self._requestRedraw(g)

    def removeSelectedBlock(self) -> None:
        """Remove the currently selected block."""
        verbosePrint("TransmutorController::removeSelectedBlock")
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
        verbosePrint("TransmutorController::setDefaults")
        self._selectedBlockID = None
        self.w.getItem("glyphNameField").set(DEFAULT_GLYPH_NAME)

        # Reset normalised location to current font's source position
        fontPath = self._currentFontPath()
        self._normLoc = dict(
            self._dsManager.defaultNormalizedLocation(fontPath))

        self.scalePercent = (100, 100)
        self.offset = (0, 0)
        minV, _maxV, minH, _maxH = self._dsManager._activeStemRange
        self.stemValues = (minV, minH)
        self.w.getItem("linkStemsCheckbox").set(1)
        self._syncStemControls()
        self.lockScale = True

    def selectBlock(self, meta: dict) -> None:
        """Populate all controls from a block's stored metadata and switch DS."""
        verbosePrint("TransmutorController::selectBlock")
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

        # Sync subspace popup to the stored normalised location
        labels = self._dsManager.subspaceLabels()
        if labels:
            subspaceLocs = [
                self._dsManager.subspaceNormLoc(i) for i in range(len(labels))
            ]

            def _dist(loc):
                return sum(
                    (loc.get(t, 0.0) - self._normLoc.get(t, 0.0)) ** 2
                    for t in loc
                )
            bestIdx = min(range(len(subspaceLocs)),
                          key=lambda i: _dist(subspaceLocs[i]))
            self.w.getItem("subspacePopupButton").set(bestIdx)

        # Restore enabled sources from the block metadata (overrides subspace
        # defaults that were set by loadDesignSpace → _refreshSourceTable).
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
                self._updateStemSliderRanges()

        # Restore stem positions as actual unit values
        minV, maxV, minH, maxH = self._dsManager._activeStemRange
        # New key: actual stem values.  Old key: normalised ratio — convert on the fly.
        if f"{LIB_PREFIX}.stemV" in meta:
            stemV = meta[f"{LIB_PREFIX}.stemV"]
            stemH = meta[f"{LIB_PREFIX}.stemH"]
        else:
            normV = meta.get(f"{LIB_PREFIX}.stemRatioV", 0.0)
            normH = meta.get(f"{LIB_PREFIX}.stemRatioH", 0.0)
            stemV = minV + normV * (maxV - minV) if maxV != minV else minV
            stemH = minH + normH * (maxH - minH) if maxH != minH else minH
        linked = meta.get(f"{LIB_PREFIX}.linkStems", True)
        self.stemValues = (stemV, stemH)
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

        storedOffset = meta.get(f"{LIB_PREFIX}.offset")
        if storedOffset and len(storedOffset) >= 2:
            dx, dy = storedOffset[0], storedOffset[1]
        else:
            # Backward compat: read offset from legacy transform key
            t = meta.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
            dx, dy = t[4], t[5]
        self.offset = (int(dx), int(dy))

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
            self._tool.drawHandles(glyph, self._selectedBlockID)

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

    def glyphNameFieldCallback(self, sender):
        verbosePrint("TransmutorController::glyphNameFieldCallback")
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
            self.w.getItem("scaleYSlider").set(
                self.w.getItem("scaleXSlider").get())
        self.updateSelectedBlock()

    def stemVSliderCallback(self, sender):
        verbosePrint("TransmutorController::stemVSliderCallback")
        if self.linkStems:
            # Mirror by normalised position so V and H track proportionally
            minV, maxV, minH, maxH = self._dsManager._activeStemRange
            t = (sender.get() - minV) / (maxV - minV) if maxV != minV else 0.0
            self.w.getItem("stemHSlider").set(minH + t * (maxH - minH))
        self.updateSelectedBlock()

    def stemHSliderCallback(self, sender):
        verbosePrint("TransmutorController::stemHSliderCallback")
        self.updateSelectedBlock()

    def linkStemsCheckboxCallback(self, sender):
        verbosePrint("TransmutorController::linkStemsCheckboxCallback")
        linked = bool(sender.get())
        self.w.getItem("stemHSlider").enable(not linked)
        if linked:
            # Sync H to the same normalised position as V
            minV, maxV, minH, maxH = self._dsManager._activeStemRange
            curV = self.w.getItem("stemVSlider").get()
            t = (curV - minV) / (maxV - minV) if maxV != minV else 0.0
            self.w.getItem("stemHSlider").set(minH + t * (maxH - minH))
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

    # Drag state — option-drag stem control
    _stemDragging = False
    _stemDragStartPoint = None
    _stemDragStartV = None
    _stemDragStartH = None

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
            self.extensionContainer(
                _MERZ_BG_ID, location="background", clear=True)

    def _registerGlyphDesignSpaces(self, glyph) -> None:
        """Ensure all DS paths referenced by glyph blocks are in the popup."""
        verbosePrint("TransmutorTool::_registerGlyphDesignSpaces")
        if self._controller is None:
            return
        for info in _getBlocksFromGlyph(glyph).values():
            dsRelPath = info["meta"].get(CONTOUR_SOURCE_KEY)
            if dsRelPath:
                dsAbsPath = os.path.normpath(
                    os.path.join(SCRIPT_DIR, dsRelPath))
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
        """Return (blockID, meta) for the first block hit, or None."""
        verbosePrint("TransmutorTool::_hitTestBlocks")
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
            blockID, meta = hit
            self._controller.selectBlock(meta)
            self.drawHandles(g, blockID)
            if self.optionDown:
                self._beginStemDrag(point)
            else:
                self._beginOffsetDrag(point, g)
            return

        # Miss — deselect
        self._dragging = False
        self._controller.setDefaults()
        self._clearHandles()

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
        verbosePrint("TransmutorTool::_beginOffsetDrag")
        glyph.prepareUndo("Move Block")
        self._dragging = True
        self._dragStartPoint = (point.x, point.y)
        ox, oy = self._controller.offset
        self._dragStartOffset = (ox, oy)

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
        self._stemDragging = True
        self._stemDragStartPoint = (point.x, point.y)
        stemV, stemH = self._controller.stemValues
        self._stemDragStartV = float(stemV)
        self._stemDragStartH = float(stemH)

    def mouseDragged(self, point, delta) -> None:
        """Route to scale, stem, or offset drag handler."""
        verbosePrint("TransmutorTool::mouseDragged")
        if self._controller is None:
            return
        if self._scalingHandle is not None:
            self._handleScaleDrag(point)
        elif self._stemDragging:
            self._handleStemDrag(point)
        elif self._dragging:
            self._handleOffsetDrag(point)

    def _handleScaleDrag(self, point) -> None:
        """Compute new scale + offset from handle drag and push to controller."""
        verbosePrint("TransmutorTool::_handleScaleDrag")
        info = _HANDLE_SCALING_INFO[self._scalingHandle]
        ax, ay = self._scalingAnchor
        hx, hy = self._scalingHandleXY
        dx0, dy0 = self._scalingStartOffset
        sx0 = self._scalingStartScaleX
        sy0 = self._scalingStartScaleY

        distH0 = hx - ax
        distV0 = hy - ay
        factor_H = ((point.x - ax) /
                    distH0) if (info["scaleH"] and distH0 != 0) else 1.0
        factor_V = ((point.y - ay) /
                    distV0) if (info["scaleV"] and distV0 != 0) else 1.0

        # Shift constrains proportional scaling on corner handles
        if self.shiftDown and info["scaleH"] and info["scaleV"]:
            if abs(factor_H - 1.0) >= abs(factor_V - 1.0):
                factor_V = factor_H
            else:
                factor_H = factor_V

        newScaleX = max(1.0, min(200.0, sx0 * factor_H))
        newScaleY = max(1.0, min(200.0, sy0 * factor_V))

        # Keep the anchor pinned: newOffset = anchor*(1 - factor) + oldOffset*factor
        newDx = ax * (1.0 - factor_H) + dx0 * \
            factor_H if info["scaleH"] else dx0
        newDy = ay * (1.0 - factor_V) + dy0 * \
            factor_V if info["scaleV"] else dy0

        self._controller.scalePercent = (newScaleX, newScaleY)
        # Store precise floats so rounding doesn't accumulate across frames
        self._controller.offset = (f"{newDx:.4f}", f"{newDy:.4f}")
        self._controller.updateSelectedBlock()

    def _handleStemDrag(self, point) -> None:
        """Adjust stem sliders based on mouse delta from drag start.

        Without Shift (isotropic): horizontal distance drives V stem; H stem
        mirrors V at the same normalised position within its own range.
        With Shift (anisotropic): X → H stem, Y → V stem independently.
        """
        verbosePrint("TransmutorTool::_handleStemDrag")
        if self._stemDragStartPoint is None:
            return
        minV, maxV, minH, maxH = self._controller._dsManager._activeStemRange
        dx = point.x - self._stemDragStartPoint[0]
        dy = point.y - self._stemDragStartPoint[1]
        if not self.shiftDown:
            # Isotropic: X movement drives V; H mirrors at same norm position.
            newV = max(minV, min(maxV, self._stemDragStartV + dx))
            t = (newV - minV) / (maxV - minV) if maxV != minV else 0.0
            newH = minH + t * (maxH - minH)
        else:
            # Anisotropic: X → H, Y → V.
            newV = max(minV, min(maxV, self._stemDragStartV + dy))
            newH = max(minH, min(maxH, self._stemDragStartH + dx))
        # Keep the link checkbox and H-slider-enable in sync with shift state.
        linked = not self.shiftDown
        self._controller.w.getItem("linkStemsCheckbox").set(1 if linked else 0)
        self._controller.w.getItem("stemHSlider").enable(not linked)
        self._controller.stemValues = (newV, newH)
        self._controller.updateSelectedBlock()

    def _handleOffsetDrag(self, point) -> None:
        """Compute new offset from body drag and push to controller."""
        verbosePrint("TransmutorTool::_handleOffsetDrag")
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
        verbosePrint("TransmutorTool::mouseUp")
        wasScaling = self._scalingHandle is not None
        wasDragging = self._dragging
        wasStemDragging = self._stemDragging
        self._clearDragState()
        if wasScaling and self._controller is not None:
            self._controller.snapToIntegers()
            self._controller.updateSelectedBlock()
        if wasScaling or wasDragging or wasStemDragging:
            g = CurrentGlyph()
            if g is not None:
                g.performUndo()

    def _clearDragState(self) -> None:
        """Reset all transient drag state."""
        verbosePrint("TransmutorTool::_clearDragState")
        self._dragging = False
        self._dragStartPoint = None
        self._dragStartOffset = None
        self._scalingHandle = None
        self._scalingAnchor = None
        self._scalingHandleXY = None
        self._scalingStartScaleX = None
        self._scalingStartScaleY = None
        self._scalingStartOffset = None
        self._stemDragging = False
        self._stemDragStartPoint = None
        self._stemDragStartV = None
        self._stemDragStartH = None


installTool(TransmutorTool())
