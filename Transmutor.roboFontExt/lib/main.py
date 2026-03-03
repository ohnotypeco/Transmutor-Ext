"""
Transmutor
========
Contours added by this script are tagged in public.objectLibs so they can
always be identified and re-interpolated.
"""

import colorsys
import math
import os
import random
import uuid

import ezui
from AppKit import NSMenuItem
from fontTools.designspaceLib import DesignSpaceDocument
from mojo.events import BaseEventTool, installTool
from ufoProcessor.ufoOperator import UFOOperator
from mojo.UI import CurrentGlyphWindow, GetFile, getDefault


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

# Stroke width used when drawing block outlines in the glyph view background
BLOCK_STROKE_WIDTH = 10
# Gaussian blur radius applied to each block outline layer
# BLOCK_BLUR_RADIUS = 0
OUTLINE_ALPHA = 0.15
# Size in glyph units of each square selection handle
HANDLE_SIZE = 10
# Cardinal names for each of the 8 handles, matching the order used in _drawHandles
_HANDLE_NAMES = ("SW", "S", "SE", "E", "NE", "N", "NW", "W")

# Per-handle scaling metadata.
# handleNorm: (u, v) normalised position of the dragged handle within bounds.
# anchorNorm: (u, v) normalised position of the fixed anchor (opposite side).
# scaleH/scaleV: whether that axis is affected by dragging this handle.
# normalised coords: (0,0)=SW corner, (1,1)=NE corner.
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

# Module-level reference to the active InterpTool instance for cross-object callbacks
_TOOL_INSTANCE = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generateBlockColor(glyph) -> list:
    """Return a bright, non-muddy [R, G, B, 0.8] color not already used by blocks in glyph.

    Colors are chosen in HSV space (S=0.80, V=0.90) to stay vivid and clear.
    Hue is selected by maximising minimum circular distance from existing block hues,
    with a small random jitter so repeated inserts into an empty glyph still vary.
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

    # Candidate hues at every 30° step (12 divisions)
    candidates = [i / 12.0 for i in range(12)]
    if existingHues:
        def _minCircularDist(h: float) -> float:
            return min(min(abs(h - e), 1.0 - abs(h - e)) for e in existingHues)
        hue = max(candidates, key=_minCircularDist)
        # Add a small jitter so identical-hue ties still vary slightly
        hue = (hue + random.uniform(-0.02, 0.02)) % 1.0
    else:
        hue = random.random()

    r, g, b = colorsys.hsv_to_rgb(hue, 0.80, 0.90)
    return [r, g, b]


def _normalizedToDesign(axis, normalizedValue: float) -> float:
    """Convert a normalized [−1, 1] value to design-space coordinates.

    Uses trilinear denormalization:
      positive side [0 → 1] maps default → maximum in user space.
      negative side [−1 → 0] maps minimum → default in user space.
    Then applies the axis <map> to reach design space.
    """
    if normalizedValue >= 0:
        userValue = axis.default + normalizedValue * (axis.maximum - axis.default)
    else:
        userValue = axis.default + normalizedValue * (axis.default - axis.minimum)
    return axis.map_forward(userValue)


def _removeTaggedContours(glyph) -> int:
    """Remove all contours previously tagged by this script. Returns count removed."""
    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    toRemove = [
        c for c in glyph.contours
        if c.identifier and CONTOUR_SOURCE_KEY in objectLibs.get(c.identifier, {})
    ]
    for contour in toRemove:
        del objectLibs[contour.identifier]
        glyph.removeContour(contour)
    glyph.lib[OBJECT_LIBS_KEY] = objectLibs
    return len(toRemove)


def _removeBlockContours(glyph, blockID: str) -> int:
    """Remove only the contours belonging to the given blockID. Returns count removed."""
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


def _insertTaggedContours(glyph, glyphMathObj, axis, designValue: float,
                          normalizedValue: float, dsRelPath: str,
                          glyphName: str,
                          transform: tuple = (1, 0, 0, 1, 0, 0),
                          color: list = None,
                          blockID: str = None) -> list:
    """Draw glyphMathObj contours into glyph, apply affine transform, and tag in public.objectLibs.

    transform: 6-tuple (xx, xy, yx, yy, dx, dy) affine matrix.
    color:     [R, G, B] list. Auto-generated (unique per glyph) when None.
    blockID:   Reuse an existing block ID (for in-place updates) or None to mint a new one.
    """
    if color is None:
        color = _generateBlockColor(glyph)
    if blockID is None:
        blockID = str(uuid.uuid4())

    countBefore = len(glyph)
    glyphMathObj.drawPoints(glyph.getPointPen())

    # Apply affine transform to newly added contours
    identity = (1, 0, 0, 1, 0, 0)
    if transform != identity:
        for contour in glyph.contours[countBefore:]:
            contour.transformBy(transform)

    objectLibs = dict(glyph.lib.get(OBJECT_LIBS_KEY, {}))
    meta = {
        CONTOUR_SOURCE_KEY: dsRelPath,
        BLOCK_ID_KEY: blockID,
        BLOCK_COLOR_KEY: color,
        f"{LIB_PREFIX}.glyphName": glyphName,
        f"{LIB_PREFIX}.location": {axis.name: designValue},
        f"{LIB_PREFIX}.normalizedLocation": {axis.tag: normalizedValue},
        f"{LIB_PREFIX}.transform": list(transform),
    }
    newIds = []
    for contour in glyph.contours[countBefore:]:
        identifier = contour.getIdentifier()
        objectLibs[identifier] = meta
        newIds.append(identifier)
    glyph.lib[OBJECT_LIBS_KEY] = objectLibs
    return newIds


# Controls that are enabled/disabled based on block hit-test
_PANEL_CONTROLS = [
    "glyphNameField",
    "wghtSlider",
    "scaleXSlider",
    "scaleYSlider",
    "xField",
    "yField",
]


def _getBlocksFromGlyph(glyph) -> dict:
    """Group tagged contours by blockID.

    Returns a dict mapping blockID → {"contours": [...], "meta": {...}}.
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
    """Return True if point is inside the given (xMin, yMin, xMax, yMax) bounds."""
    if bounds is None:
        return False
    xMin, yMin, xMax, yMax = bounds
    return xMin <= point.x <= xMax and yMin <= point.y <= yMax


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class TransmutorController(ezui.WindowController):

    def build(self):
        # Designspace loading is deferred until one is chosen via the popup.
        # These are set by loadDesignSpace() once a file is selected.
        self.designspacePaths = []  # ordered list of known absolute paths, deduplicated
        self._wghtAxis = None
        self._op = None
        self._dsRelPath = None
        self._dsPath = None      # absolute path of the currently loaded designspace
        self._defaultFont = None

        content = """
        = TwoColumnForm
        : Designspace:
        (Choose a Designspace ...)  @designspacePopupButton
        : Glyph:
        [__]                @glyphNameField
        : wght:
        ---X--- [__]        @wghtSlider
        : Scale X:
        ---X--- [__]        @scaleXSlider
        : Scale Y:
        ---X--- [__]        @scaleYSlider
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
            glyphNameField=dict(
                value=DEFAULT_GLYPH_NAME,
            ),
            xField=dict(
                value="0",
                placeholder="X",
            ),
            yField=dict(
                value="0",
                placeholder="Y",
            ),
            wghtSlider=dict(
                minValue=0.0,
                maxValue=1.0,
                value=0.6,
            ),
            scaleXSlider=dict(
                minValue=1,
                maxValue=200,
                value=100,
            ),
            scaleYSlider=dict(
                minValue=1,
                maxValue=200,
                value=100,
            ),
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
            allDesignSpaces = AllDesignspaces()
        except Exception:
            allDesignSpaces = []
        for ds in (allDesignSpaces or []):
            self.addDesignSpace(ds.path)

        try:
            current = CurrentDesignspace()
        except Exception:
            current = None
        if current and current.path:
            self.loadDesignSpace(current.path)
        elif self.designspacePaths:
            # No current designspace, but we have registered paths — load the
            # first one so the popup selection matches the active data model.
            self.loadDesignSpace(self.designspacePaths[0])

    def started(self):
        self.w.open()
        # No block selected initially; controls show defaults but are always enabled
        self._selectedBlockID = None

    # -----------------------------------------------------------------------
    # Designspace management
    # -----------------------------------------------------------------------

    def designspacePopupButtonCallback(self, sender):
        """Respond to popup selection: open file picker or switch to chosen DS."""
        if sender.getItem() == "Choose a Designspace ...":
            self.openDesignSpace()
        else:
            idx = sender.get()
            if 0 <= idx < len(self.designspacePaths):
                self.loadDesignSpace(self.designspacePaths[idx])

    def openDesignSpace(self, path: str = None) -> None:
        """Open a designspace, prompting with a file picker when no path is given."""
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

    def addDesignSpace(self, path: str) -> None:
        """Register a designspace path and refresh the popup items."""
        path = os.path.normpath(os.path.abspath(path))
        if path not in self.designspacePaths:
            self.designspacePaths.insert(0, path)

        if len(self.designspacePaths) > 1:
            commonPath = os.path.commonpath(self.designspacePaths)
            items = [os.path.relpath(p, commonPath) for p in self.designspacePaths]
        elif self.designspacePaths:
            items = [os.path.basename(self.designspacePaths[0])]
        else:
            items = []

        items += [NSMenuItem.separatorItem(), "Choose a Designspace ..."]
        self.w.getItem("designspacePopupButton").setItems(items)

    def loadDesignSpace(self, path: str) -> None:
        """Load the given designspace into the operator and update the popup selection."""
        path = os.path.normpath(os.path.abspath(path))
        if path != self._dsPath:
            try:
                ds = DesignSpaceDocument.fromfile(path)
                self._wghtAxis = next((a for a in ds.axes if a.tag == "wght"), None)
                self._op = UFOOperator(path, useVarlib=False)
                self._op.loadFonts()
                self._dsRelPath = os.path.relpath(path, SCRIPT_DIR)
                self._dsPath = path
                self._defaultFont = self._op.findDefaultFont()
            except Exception as e:
                print(f"Failed to load designspace '{path}': {e}")
                return

        # Always sync the popup selection, even if the DS was already loaded
        if path in self.designspacePaths:
            self.w.getItem("designspacePopupButton").set(self.designspacePaths.index(path))

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _glyphName(self) -> str:
        return self.w.getItem("glyphNameField").get().strip()

    def _currentNormalized(self) -> float:
        return self.w.getItem("wghtSlider").get()

    def _currentOffset(self) -> tuple:
        """Return (x, y) offset as integers, defaulting to 0 on invalid input.

        Accepts both integer and float strings so the field can hold precise
        float values during a scale drag without losing information.
        """
        def _parseNum(val: str) -> int:
            try:
                return int(round(float(val)))
            except (ValueError, TypeError):
                return 0
        x = _parseNum(self.w.getItem("xField").get())
        y = _parseNum(self.w.getItem("yField").get())
        return x, y

    def _currentTransform(self) -> tuple:
        """Return a 6-tuple affine matrix (xx, xy, yx, yy, dx, dy).

        Scale values are read from the sliders as percentages (1–200) and
        converted to scale factors. A zero scale is guarded against by
        clamping to a minimum of 1% (0.01). Offset values supply dx/dy.
        """
        offsetX, offsetY = self._currentOffset()
        scaleX = max(1, self.w.getItem("scaleXSlider").get()) / 100.0
        scaleY = max(1, self.w.getItem("scaleYSlider").get()) / 100.0
        # Affine matrix: scale around origin then translate
        return (scaleX, 0, 0, scaleY, offsetX, offsetY)

    def _resolveGlyph(self, glyphName: str = None):
        """Return (glyphName, g, mathGlyph, designValue, normalizedValue) or None on failure."""
        if self._op is None or self._defaultFont is None or self._wghtAxis is None:
            print("No designspace loaded — choose one from the popup.")
            return None
        if glyphName is None:
            glyphName = self._glyphName()
        if glyphName not in self._defaultFont:
            print(f"'{glyphName}' not found in default source — no change.")
            return None
        g = CurrentGlyph()
        if g is None:
            print("No current glyph selected.")
            return None
        normalizedValue = self._currentNormalized()
        designValue = _normalizedToDesign(self._wghtAxis, normalizedValue)
        location = {self._wghtAxis.name: designValue}
        mathGlyph = self._op.makeOneGlyph(glyphName, location, decomposeComponents=False)
        if mathGlyph is None:
            print(f"Could not interpolate '{glyphName}' at {location}")
            return None
        return glyphName, g, mathGlyph, designValue, normalizedValue

    def _updateSelectedBlock(self) -> None:
        """Replace the contours of the currently selected block in-place.

        If no block is selected this is a no-op — sliders are just staging for
        the next Insert.
        """
        if self._selectedBlockID is None:
            return
        result = self._resolveGlyph()
        if result is None:
            return
        glyphName, g, mathGlyph, designValue, normalizedValue = result
        transform = self._currentTransform()

        # Retrieve the color stored for the selected block
        existingColor = None
        objLibs = g.lib.get(OBJECT_LIBS_KEY, {})
        for c in g.contours:
            if c.identifier:
                m = objLibs.get(c.identifier, {})
                if m.get(BLOCK_ID_KEY) == self._selectedBlockID:
                    existingColor = m.get(BLOCK_COLOR_KEY)
                    break

        g.prepareUndo("Update Block")
        _removeBlockContours(g, self._selectedBlockID)
        _insertTaggedContours(
            g, mathGlyph, self._wghtAxis, designValue, normalizedValue,
            self._dsRelPath, glyphName, transform,
            color=existingColor,
            blockID=self._selectedBlockID,
        )
        g.performUndo()

        if _TOOL_INSTANCE is not None:
            _TOOL_INSTANCE._drawBlocks(g)
            _TOOL_INSTANCE._drawHandles(g, self._selectedBlockID)

    def _insertNewBlock(self) -> None:
        """Insert a brand-new block with the current panel settings.

        Never removes or touches existing blocks.
        """
        result = self._resolveGlyph()
        if result is None:
            return
        glyphName, g, mathGlyph, designValue, normalizedValue = result
        transform = self._currentTransform()

        g.prepareUndo("Insert Block")
        newIds = _insertTaggedContours(
            g, mathGlyph, self._wghtAxis, designValue, normalizedValue,
            self._dsRelPath, glyphName, transform,
        )
        g.performUndo()

        # Select the freshly created block so sliders now update it live
        if newIds:
            objLibs = g.lib.get(OBJECT_LIBS_KEY, {})
            self._selectedBlockID = objLibs.get(newIds[0], {}).get(BLOCK_ID_KEY)

        if _TOOL_INSTANCE is not None:
            _TOOL_INSTANCE._drawBlocks(g)
            _TOOL_INSTANCE._drawHandles(g, self._selectedBlockID)

    # -----------------------------------------------------------------------
    # Callbacks
    # -----------------------------------------------------------------------

    def glyphNameFieldCallback(self, sender):
        """Live-update the selected block when the glyph name is committed."""
        self._updateSelectedBlock()

    def xFieldCallback(self, sender):
        """Live-update the selected block at the new X offset."""
        self._updateSelectedBlock()

    def yFieldCallback(self, sender):
        """Live-update the selected block at the new Y offset."""
        self._updateSelectedBlock()

    def scaleXSliderCallback(self, sender):
        """Live-update the selected block at the new horizontal scale."""
        self._updateSelectedBlock()

    def scaleYSliderCallback(self, sender):
        """Live-update the selected block at the new vertical scale."""
        self._updateSelectedBlock()

    def wghtSliderCallback(self, sender):
        """Live-update the selected block as the slider is dragged."""
        self._updateSelectedBlock()

    def insertButtonCallback(self, sender):
        """Insert a new independent block with the current panel settings."""
        self._insertNewBlock()

    def removeButtonCallback(self, sender):
        g = CurrentGlyph()
        if g is None:
            return
        g.prepareUndo("Remove Block")
        if self._selectedBlockID is not None:
            removed = _removeBlockContours(g, self._selectedBlockID)
            self._selectedBlockID = None
        else:
            removed = _removeTaggedContours(g)
        g.performUndo()
        print(f"Removed {removed} tagged contour(s)")
        if _TOOL_INSTANCE is not None:
            _TOOL_INSTANCE._drawBlocks(g)
            _TOOL_INSTANCE._clearHandles()

    # -----------------------------------------------------------------------
    # UI state helpers (called by InterpTool on hit-test)
    # -----------------------------------------------------------------------

    def setEnabled(self, enabled: bool) -> None:
        """Enable or disable all editing controls."""
        for name in _PANEL_CONTROLS:
            self.w.getItem(name).enable(enabled)

    def setDefaults(self) -> None:
        """Clear selection and reset all controls to their initial default values."""
        self._selectedBlockID = None
        self.w.getItem("glyphNameField").set(DEFAULT_GLYPH_NAME)
        self.w.getItem("wghtSlider").set(0.6)
        self.w.getItem("scaleXSlider").set(100)
        self.w.getItem("scaleYSlider").set(100)
        self.w.getItem("xField").set("0")
        self.w.getItem("yField").set("0")

    def setFromBlock(self, meta: dict) -> None:
        """Select this block and populate all controls from its stored metadata.

        Also switches the active designspace to the one the block was created
        with, so that subsequent slider updates re-interpolate from the correct
        source.
        """
        self._selectedBlockID = meta.get(BLOCK_ID_KEY)

        # Switch designspace if the block was created from a different one
        dsRelPath = meta.get(CONTOUR_SOURCE_KEY)
        if dsRelPath:
            dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, dsRelPath))
            self.addDesignSpace(dsAbsPath)
            self.loadDesignSpace(dsAbsPath)

        glyphName = meta.get(f"{LIB_PREFIX}.glyphName", DEFAULT_GLYPH_NAME)
        self.w.getItem("glyphNameField").set(glyphName)

        # Normalized wght value (first — and only — axis tag in the dict)
        normalizedLoc = meta.get(f"{LIB_PREFIX}.normalizedLocation", {})
        wght = next(iter(normalizedLoc.values()), 0.6)
        self.w.getItem("wghtSlider").set(wght)

        # Affine transform: [xx, xy, yx, yy, dx, dy]
        transform = meta.get(f"{LIB_PREFIX}.transform", [1, 0, 0, 1, 0, 0])
        xx, _xy, _yx, yy, dx, dy = transform
        self.w.getItem("scaleXSlider").set(round(xx * 100))
        self.w.getItem("scaleYSlider").set(round(yy * 100))
        self.w.getItem("xField").set(str(int(dx)))
        self.w.getItem("yField").set(str(int(dy)))


# ---------------------------------------------------------------------------
# Glyph view event tool
# ---------------------------------------------------------------------------

class TransmutorTool(BaseEventTool):
    """BaseEventTool that drives the TransmutorController panel.

    The panel is created lazily on first activation. Subsequent activate /
    deactivate calls simply show / hide the existing window so the current
    slider values and glyph name are preserved.
    """

    _controller = None
    _dragging = False
    _dragStartPoint = None
    _dragStartOffset = None
    # Scaling state — populated on mouseDown when a handle is hit
    _scalingHandle = None        # compass name of the active handle (or None)
    _scalingAnchor = None        # (ax, ay) fixed anchor point in glyph coords
    _scalingHandleXY = None      # (hx, hy) handle position at drag start
    _scalingStartScaleX = None   # scaleX percentage at drag start
    _scalingStartScaleY = None   # scaleY percentage at drag start
    _scalingStartOffset = None   # (dx, dy) at drag start

    def getToolbarTip(self) -> str:
        return "Transmutor"

    def becomeActive(self) -> None:
        """Register as the active tool instance, show/create the panel, draw existing blocks."""
        global _TOOL_INSTANCE
        _TOOL_INSTANCE = self
        if self._controller is None:
            self._controller = InterpController()
        else:
            self._controller.w.show()
        g = CurrentGlyph()
        if g is not None:
            self._registerGlyphDesignSpaces(g)
            self._drawBlocks(g)

    def becomeInactive(self) -> None:
        """Deregister, hide panel, and clear all Merz layers."""
        global _TOOL_INSTANCE
        _TOOL_INSTANCE = None
        if self._controller is not None:
            self._controller.w.hide()
        self.extensionContainer(
            "co.ohnotype.transmutor", location="background", clear=True
        )
        self._clearHandles()

    def currentGlyphChanged(self) -> None:
        """Redraw block outlines when the user switches to a different glyph."""
        # Deselect any block — it belongs to the previous glyph
        if self._controller is not None:
            self._controller.setDefaults()
        self._clearHandles()
        g = CurrentGlyph()
        if g is not None:
            self._registerGlyphDesignSpaces(g)
            self._drawBlocks(g)
        else:
            self.extensionContainer(
                "co.ohnotype.transmutor", location="background", clear=True
            )

    def _registerGlyphDesignSpaces(self, glyph) -> None:
        """Add any designspace paths referenced by blocks in glyph to the popup.

        Called on tool activation and on glyph change so the dropdown always
        reflects every DS that is represented in the current glyph's blocks.
        """
        if self._controller is None:
            return
        for info in _getBlocksFromGlyph(glyph).values():
            dsRelPath = info["meta"].get(CONTOUR_SOURCE_KEY)
            if dsRelPath:
                dsAbsPath = os.path.normpath(os.path.join(SCRIPT_DIR, dsRelPath))
                self._controller.addDesignSpace(dsAbsPath)

    def _drawBlocks(self, glyph) -> None:
        """Repaint the background Merz layer with colored outlines for every block."""
        container = self.extensionContainer(
            "co.ohnotype.transmutor", location="background", clear=True
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
                # layer.appendFilter(dict(
                #     name="blockBlur",
                #     filterType="gaussianBlur",
                #     radius=BLOCK_BLUR_RADIUS,
                # ))
                pen = layer.getPen()
                contour.draw(pen)

    def _drawHandles(self, glyph, blockID: str) -> None:
        """Draw a dashed selection box and 8 corner/midpoint handles for blockID.

        Uses a separate foreground container ("co.ohnotype.transmutor.handles") so
        handles sit on top of all glyph content.  The box is stroked with the
        block's color at OUTLINE_ALPHA; handles are filled at full opacity.
        """
        container = self.extensionContainer(
            "co.ohnotype.transmutor.handles", location="foreground", clear=True
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
        strokeColor = (r, g, b, 1.0)
        handleColor = (r, g, b, 1.0)

        xMin, yMin, xMax, yMax = bounds
        midX = (xMin + xMax) / 2
        midY = (yMin + yMax) / 2

        # Dashed bounding box
        boxLayer = container.appendPathSublayer(
            fillColor=None,
            strokeColor=strokeColor,
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
        handlePositions = [
            (xMin, yMin),   # SW
            (midX, yMin),   # S
            (xMax, yMin),   # SE
            (xMax, midY),   # E
            (xMax, yMax),   # NE
            (midX, yMax),   # N
            (xMin, yMax),   # NW
            (xMin, midY),   # W
        ]
        for pos in handlePositions:
            container.appendSymbolSublayer(
                position=pos,
                imageSettings=dict(
                    name="rectangle",
                    size=(HANDLE_SIZE, HANDLE_SIZE),
                    fillColor=handleColor,
                ),
            )

    def _clearHandles(self) -> None:
        """Remove all foreground selection handle layers."""
        self.extensionContainer(
            "co.ohnotype.transmutor.handles", location="foreground", clear=True
        )

    def _hitTestHandles(self, point, glyph, blockID) -> str | None:
        """Return the compass name of the handle nearest to point, or None.

        The hit radius is HANDLE_SIZE screen-pixels converted to glyph units
        via the current view scale, matching the backup's _ptOnHandle approach
        so the target is always ~HANDLE_SIZE pixels wide regardless of zoom.
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

        xMin, yMin, xMax, yMax = bounds
        midX = (xMin + xMax) / 2
        midY = (yMin + yMax) / 2

        handlePositions = [
            (xMin, yMin),   # SW
            (midX, yMin),   # S
            (xMax, yMin),   # SE
            (xMax, midY),   # E
            (xMax, yMax),   # NE
            (midX, yMax),   # N
            (xMin, yMax),   # NW
            (xMin, midY),   # W
        ]

        viewScale = CurrentGlyphWindow().getGlyphViewScale()
        hitRadius = HANDLE_SIZE * (1.0 / viewScale)

        for name, (hx, hy) in zip(_HANDLE_NAMES, handlePositions):
            if abs(point.x - hx) <= hitRadius and abs(point.y - hy) <= hitRadius:
                return name
        return None

    def mouseDown(self, point, clickCount: int) -> None:
        """Hit-test tagged blocks and update the panel accordingly.

        If the click lands inside the bounding box of a block's contours the
        panel controls are enabled and populated from that block's lib data.
        Any click outside every block resets controls to defaults and disables
        them.
        """
        if self._controller is None:
            return
        g = CurrentGlyph()
        if g is None:
            return

        # Handle hit-test takes priority when a block is already selected
        selectedID = self._controller._selectedBlockID
        if selectedID is not None:
            handleName = self._hitTestHandles(point, g, selectedID)
            if handleName is not None:
                info = _HANDLE_SCALING_INFO[handleName]
                blocks = _getBlocksFromGlyph(g)
                bounds = _unionBounds(blocks[selectedID]["contours"])
                xMin, yMin, xMax, yMax = bounds

                def _lerp(a: float, b: float, t: float) -> float:
                    return a + (b - a) * t

                hu, hv = info["handleNorm"]
                au, av = info["anchorNorm"]
                self._scalingHandle = handleName
                self._scalingHandleXY = (_lerp(xMin, xMax, hu), _lerp(yMin, yMax, hv))
                self._scalingAnchor = (_lerp(xMin, xMax, au), _lerp(yMin, yMax, av))
                self._scalingStartScaleX = float(self._controller.w.getItem("scaleXSlider").get())
                self._scalingStartScaleY = float(self._controller.w.getItem("scaleYSlider").get())
                # Store offset as floats so rounding doesn't compound during drag
                ox, oy = self._controller._currentOffset()
                self._scalingStartOffset = (float(ox), float(oy))
                print(f"mouseDown — handle '{handleName}'  block {selectedID[:8]}…  anchor: ({self._scalingAnchor[0]:.1f}, {self._scalingAnchor[1]:.1f})")
                return

        blocks = _getBlocksFromGlyph(g)
        for blockID, info in blocks.items():
            bounds = _unionBounds(info["contours"])
            if _pointInBounds(point, bounds):
                self._controller.setFromBlock(info["meta"])
                self._drawHandles(g, blockID)
                self._dragging = True
                self._dragStartPoint = (point.x, point.y)
                # Capture the current offset as the drag origin
                ox, oy = self._controller._currentOffset()
                self._dragStartOffset = (ox, oy)
                print(f"mouseDown — block {blockID[:8]}…  x: {point.x:.2f}  y: {point.y:.2f}")
                return

        # Click outside all blocks — deselect, reset staging values
        self._dragging = False
        self._controller.setDefaults()
        self._clearHandles()
        print(f"mouseDown — no block  x: {point.x:.2f}  y: {point.y:.2f}")

    def mouseDragged(self, point, delta) -> None:
        """Handle dragging.

        - Scale drag: active when a handle was hit on mouseDown.
          The anchor point stays fixed; the dragged handle follows the mouse,
          updating scaleX/Y (and offset to keep the anchor pinned).
        - Offset drag: active when the block body was hit on mouseDown.
          Movement is clamped to the nearest 45° axis when Shift is held.
        """
        if self._controller is None:
            return

        # ---- scale drag ----
        if self._scalingHandle is not None:
            info = _HANDLE_SCALING_INFO[self._scalingHandle]
            ax, ay = self._scalingAnchor
            hx, hy = self._scalingHandleXY
            dx0, dy0 = self._scalingStartOffset
            sx0 = self._scalingStartScaleX
            sy0 = self._scalingStartScaleY

            # Compute scale factors from anchor-relative distances
            distH0 = hx - ax
            distV0 = hy - ay
            distHNow = point.x - ax
            distVNow = point.y - ay

            factor_H = (distHNow / distH0) if (info["scaleH"] and distH0 != 0) else 1.0
            factor_V = (distVNow / distV0) if (info["scaleV"] and distV0 != 0) else 1.0

            # Shift constrains X = Y: the axis with the larger deviation leads
            if self.shiftDown and info["scaleH"] and info["scaleV"]:
                if abs(factor_H - 1.0) >= abs(factor_V - 1.0):
                    factor_V = factor_H
                else:
                    factor_H = factor_V

            # No rounding during drag — keep full float precision for smooth movement
            newScaleX = max(1.0, min(200.0, sx0 * factor_H))
            newScaleY = max(1.0, min(200.0, sy0 * factor_V))

            # Keep the anchor pinned: newOffset = anchor*(1 - factor) + oldOffset*factor
            newDx = ax * (1.0 - factor_H) + dx0 * factor_H if info["scaleH"] else dx0
            newDy = ay * (1.0 - factor_V) + dy0 * factor_V if info["scaleV"] else dy0

            w = self._controller.w
            w.getItem("scaleXSlider").set(newScaleX)
            w.getItem("scaleYSlider").set(newScaleY)
            # Store precise floats in the fields so _currentTransform() is exact
            w.getItem("xField").set(f"{newDx:.4f}")
            w.getItem("yField").set(f"{newDy:.4f}")
            self._controller._updateSelectedBlock()
            return

        # ---- offset drag ----
        if not self._dragging:
            return
        if self._dragStartPoint is None or self._dragStartOffset is None:
            return

        # Total displacement from where the drag began
        rawDx = point.x - self._dragStartPoint[0]
        rawDy = point.y - self._dragStartPoint[1]

        if self.shiftDown and (rawDx != 0 or rawDy != 0):
            # Snap displacement to the nearest 45° direction
            magnitude = math.hypot(rawDx, rawDy)
            angle = math.atan2(rawDy, rawDx)
            snappedAngle = round(angle / (math.pi / 4)) * (math.pi / 4)
            rawDx = magnitude * math.cos(snappedAngle)
            rawDy = magnitude * math.sin(snappedAngle)

        newX = int(round(self._dragStartOffset[0] + rawDx))
        newY = int(round(self._dragStartOffset[1] + rawDy))

        w = self._controller.w
        w.getItem("xField").set(str(newX))
        w.getItem("yField").set(str(newY))
        self._controller._updateSelectedBlock()

    def mouseUp(self, point) -> None:
        """End any active drag (offset or scale), snapping float values to integers."""
        wasScaling = self._scalingHandle is not None

        self._dragging = False
        self._dragStartPoint = None
        self._dragStartOffset = None
        self._scalingHandle = None
        self._scalingAnchor = None
        self._scalingHandleXY = None
        self._scalingStartScaleX = None
        self._scalingStartScaleY = None
        self._scalingStartOffset = None

        # Round float drag values to clean integers once the user releases
        if wasScaling and self._controller is not None:
            w = self._controller.w
            sx = int(round(w.getItem("scaleXSlider").get()))
            sy = int(round(w.getItem("scaleYSlider").get()))
            dx, dy = self._controller._currentOffset()  # already rounds via float parse
            w.getItem("scaleXSlider").set(sx)
            w.getItem("scaleYSlider").set(sy)
            w.getItem("xField").set(str(dx))
            w.getItem("yField").set(str(dy))
            self._controller._updateSelectedBlock()


installTool(InterpTool())
