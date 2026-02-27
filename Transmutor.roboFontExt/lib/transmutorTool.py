"""
Transmutor v3 — EditingTool subclass.

The main toolbar tool that coordinates between the model and view.
Handles mouse events for interactive scaling, keyboard shortcuts,
modifier keys, and the ezui inspector panel for UI controls.
"""

from typing import Any, Dict, List, Optional, Tuple

import ezui
from mojo.events import EditingTool
from mojo.extensions import ExtensionBundle
from mojo.roboFont import AllFonts, CurrentFont, CurrentGlyph
from mojo.UI import CurrentGlyphWindow

_toolbarIcon = ExtensionBundle("Transmutor").get("LassoToolIcon")

from constants import (
    DEFAULT_SCALE,
    DEFAULT_STEM_RATIO,
    MIN_SCALE,
    MAX_SCALE,
    SLIDER_MIN_SCALE,
    SLIDER_MAX_SCALE,
    SLIDER_MIN_STEM,
    SLIDER_MAX_STEM,
    VERSION,
    verbosePrint,
)
from transmutorModel import BlockData, TransmutorModel
from transmutorView import (
    HANDLE_MAP,
    TransmutorView,
    hitTestBounds,
    hitTestHandles,
)


def _clamp(value: float, minVal: float, maxVal: float) -> float:
    """Clamp a value to a range.

    Args:
        value: The value to clamp.
        minVal: Minimum allowed value.
        maxVal: Maximum allowed value.

    Returns:
        Clamped value.
    """
    verbosePrint("_clamp")
    return max(minVal, min(maxVal, value))


def _interpolate(a: float, b: float, v: float) -> float:
    """Linear interpolation between two values."""
    verbosePrint("_interpolate")
    return a + (b - a) * v


class TransmutorInspector(ezui.WindowController):
    """Floating panel for Transmutor controls.

    Owns the ezui.EZPanel window and all its UI callbacks.
    Communicates state changes back to TransmutorTool via _tool.
    """

    def __init__(self, tool: "TransmutorTool") -> None:
        verbosePrint("TransmutorInspector::__init__")
        self._tool = tool
        super().__init__()

    def build(self) -> None:
        """Build the floating panel UI."""
        verbosePrint("TransmutorInspector::build")
        content = """
        = TwoColumnForm

        : Glyph:
        [__]                        @glyphNameField

        ----------------------

        : Sources:
        |--------------------------------------------|     @sourceFontTable
        | selected | font     | vStem    | hStem     |
        |----------|----------|----------| ----------|
        |          |          |          |           |
        |--------------------------------------------|

        ----------------------

        : V Stem:
        ---X--- [__]                @stemRatioVSlider
        : H Stem:
        ---X--- [__]                @stemRatioHSlider
        :
        [X] Constrain               @constrainStemsSwitch

        -----------------------

        : Height:
        ---X--- [__]                @scaleVSlider
        : Width:
        ---X--- [__]                @scaleHSlider
        :
        [X] Constrain               @constrainScaleSwitch

        =======================
        (Insert into Glyph)      @insertButton
        (Remove Block)           @removeButton
        """

        descriptionData = dict(
            content=dict(
                titleColumnWidth=60,
                itemColumnWidth=280,
            ),
            glyphNameField=dict(
                placeholder="Source glyph name",
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
                        identifier="font",
                        title="Font",
                        editable=False,
                    ),
                    dict(
                        identifier="vStem",
                        title="V",
                        width=35,
                        editable=False,
                    ),
                    dict(
                        identifier="hStem",
                        title="H",
                        width=35,
                        editable=False,
                    ),
                ],
                items=[],
                height=120,
                showColumnTitles=True,
                alternatingRowColors=True,
            ),
            stemRatioVSlider=dict(
                valueType="float",
                value=DEFAULT_STEM_RATIO,
                minValue=SLIDER_MIN_STEM,
                maxValue=SLIDER_MAX_STEM,
                tickMarks=5,
            ),
            stemRatioHSlider=dict(
                valueType="float",
                value=DEFAULT_STEM_RATIO,
                minValue=SLIDER_MIN_STEM,
                maxValue=SLIDER_MAX_STEM,
                tickMarks=5,
            ),
            scaleVSlider=dict(
                valueType="float",
                value=DEFAULT_SCALE,
                minValue=SLIDER_MIN_SCALE,
                maxValue=SLIDER_MAX_SCALE,
                tickMarks=5,
            ),
            scaleHSlider=dict(
                valueType="float",
                value=DEFAULT_SCALE,
                minValue=SLIDER_MIN_SCALE,
                maxValue=SLIDER_MAX_SCALE,
                tickMarks=5,
            ),
        )

        self.w = ezui.EZPanel(
            content=content,
            descriptionData=descriptionData,
            controller=self,
            title=f"Transmutor v{VERSION}",
            autosaveName="TransmutorPanel",
            size=(300, "auto"),
        )
        self.w.getNSWindow().setTitlebarAppearsTransparent_(True)

    def started(self) -> None:
        """Open the panel and populate the font table."""
        verbosePrint("TransmutorInspector::started")
        self.w.open()
        self.refreshFontTable()

    # ──────────────────────────────────────────────
    #  Sync helpers (called from the tool)
    # ──────────────────────────────────────────────

    def syncFromBlock(self, block: Any) -> None:
        """Update all sliders to reflect the given block's state.

        Args:
            block: BlockData to sync from, or None to skip.
        """
        verbosePrint("TransmutorInspector::syncFromBlock")
        if block is None:
            return
        try:
            self.w.getItem("stemRatioVSlider").set(block.stemRatioV)
            self.w.getItem("stemRatioHSlider").set(block.stemRatioH)
            self.w.getItem("scaleVSlider").set(block.scaleV)
            self.w.getItem("scaleHSlider").set(block.scaleH)
        except Exception:
            pass

    def refreshFontTable(self) -> None:
        """Repopulate the source font table from the model."""
        verbosePrint("TransmutorInspector::refreshFontTable")
        try:
            stemData = self._tool._model.getFontStemData()
            items = [
                {
                    "selected": data["active"],
                    "font": data["font"],
                    "vStem": data["vStem"] if data["vStem"] is not None else "—",
                    "hStem": data["hStem"] if data["hStem"] is not None else "—",
                }
                for data in stemData
            ]
            self.w.getItem("sourceFontTable").set(items)
        except Exception:
            pass

    # ──────────────────────────────────────────────
    #  Panel callbacks
    # ──────────────────────────────────────────────

    def glyphNameFieldCallback(self, sender: Any) -> None:
        """Handle glyph name text field changes."""
        verbosePrint("TransmutorInspector::glyphNameFieldCallback")
        glyphName = sender.get().strip()
        verbosePrint(f"glyphNameFieldCallback: '{glyphName}'")
        model = self._tool._model

        if not glyphName:
            model.activeBlock = None
            self._tool._layers.clearAll()
            return

        if model.activeBlock is None:
            model.activeBlock = BlockData(sourceGlyph=glyphName)
        else:
            model.activeBlock.sourceGlyph = glyphName

        # Initial offset is (0, 0) — the scaled preview appears at the
        # source glyph's natural position so the user can immediately
        # see it and drag it into position.
        self._tool._redraw()

    def sourceFontTableEditCallback(self, sender: Any) -> None:
        """Handle source font table checkbox changes."""
        verbosePrint("TransmutorInspector::sourceFontTableEditCallback")
        model = self._tool._model
        items = sender.get()
        activeFonts = [
            model.allFonts[i]
            for i, item in enumerate(items)
            if i < len(model.allFonts) and item.get("selected")
        ]
        model.setActiveFonts(activeFonts)
        self._tool._redraw()

    def stemRatioVSliderCallback(self, sender: Any) -> None:
        """Handle vertical stem ratio slider changes."""
        verbosePrint("TransmutorInspector::stemRatioVSliderCallback")
        value = float(sender.get())
        model = self._tool._model
        if model.activeBlock is None:
            return
        model.activeBlock.stemRatioV = value
        if model.constrainStems:
            model.activeBlock.stemRatioH = value
            self.syncFromBlock(model.activeBlock)
        self._tool._redraw()

    def stemRatioVSliderTextFieldCallback(self, sender: Any) -> None:
        verbosePrint("TransmutorInspector::stemRatioVSliderTextFieldCallback")
        self.stemRatioVSliderCallback(sender)

    def stemRatioHSliderCallback(self, sender: Any) -> None:
        """Handle horizontal stem ratio slider changes."""
        verbosePrint("TransmutorInspector::stemRatioHSliderCallback")
        model = self._tool._model
        if model.activeBlock is None:
            return
        model.activeBlock.stemRatioH = float(sender.get())
        self._tool._redraw()

    def stemRatioHSliderTextFieldCallback(self, sender: Any) -> None:
        verbosePrint("TransmutorInspector::stemRatioHSliderTextFieldCallback")
        self.stemRatioHSliderCallback(sender)

    def constrainStemsSwitchCallback(self, sender: Any) -> None:
        """Handle constrain stems toggle."""
        verbosePrint("TransmutorInspector::constrainStemsSwitchCallback")
        model = self._tool._model
        model.constrainStems = bool(sender.get())
        if model.constrainStems and model.activeBlock is not None:
            model.activeBlock.stemRatioH = model.activeBlock.stemRatioV
            self.syncFromBlock(model.activeBlock)
        self._tool._redraw()

    def scaleVSliderCallback(self, sender: Any) -> None:
        """Handle vertical scale slider changes."""
        verbosePrint("TransmutorInspector::scaleVSliderCallback")
        value = max(float(sender.get()), MIN_SCALE)
        model = self._tool._model
        if model.activeBlock is None:
            return
        model.activeBlock.scaleV = value
        if model.constrainScale:
            model.activeBlock.scaleH = value
            self.syncFromBlock(model.activeBlock)
        self._tool._redraw()

    def scaleVSliderTextFieldCallback(self, sender: Any) -> None:
        verbosePrint("TransmutorInspector::scaleVSliderTextFieldCallback")
        self.scaleVSliderCallback(sender)

    def scaleHSliderCallback(self, sender: Any) -> None:
        """Handle horizontal scale slider changes."""
        verbosePrint("TransmutorInspector::scaleHSliderCallback")
        model = self._tool._model
        if model.activeBlock is None:
            return
        model.activeBlock.scaleH = max(float(sender.get()), MIN_SCALE)
        self._tool._redraw()

    def scaleHSliderTextFieldCallback(self, sender: Any) -> None:
        verbosePrint("TransmutorInspector::scaleHSliderTextFieldCallback")
        self.scaleHSliderCallback(sender)

    def constrainScaleSwitchCallback(self, sender: Any) -> None:
        """Handle constrain scale toggle."""
        verbosePrint("TransmutorInspector::constrainScaleSwitchCallback")
        model = self._tool._model
        model.constrainScale = bool(sender.get())
        if model.constrainScale and model.activeBlock is not None:
            model.activeBlock.scaleH = model.activeBlock.scaleV
            self.syncFromBlock(model.activeBlock)
        self._tool._redraw()

    def insertButtonCallback(self, sender: Any) -> None:
        """Insert the current scaled glyph into the current glyph as real contours."""
        verbosePrint("TransmutorInspector::insertButtonCallback")
        verbosePrint("insertButtonCallback")
        model = self._tool._model
        glyph = model.currentGlyph
        block = model.activeBlock

        if glyph is None or block is None or not block.sourceGlyph:
            return

        with glyph.undo("Transmutor Insert"):
            blockId = model.insertBlock(glyph, block)

        if blockId is not None:
            model.activeBlock = None
            self._tool._layers.clearAll()
            verbosePrint(f"Inserted block {blockId}")

    def removeButtonCallback(self, sender: Any) -> None:
        """Remove the active block's contours from the glyph."""
        verbosePrint("TransmutorInspector::removeButtonCallback")
        verbosePrint("removeButtonCallback")
        model = self._tool._model
        glyph = model.currentGlyph
        block = model.activeBlock

        if glyph is None or block is None:
            return

        with glyph.undo("Transmutor Remove"):
            model.removeBlock(glyph, block.blockId)

        model.activeBlock = None
        self._tool._layers.clearAll()


class TransmutorTool(EditingTool):
    """RoboFont toolbar tool for Transmutor live contour scaling.

    Allows users to:
    - Type a source glyph name to insert scaled contours
    - Drag bounding box handles to interactively scale
    - Use sliders to control interpolation and scale
    - Option-drag to adjust stem weight ratios
    - Re-select and re-edit previously inserted blocks
    """

    # ──────────────────────────────────────────────
    #  Tool registration
    # ──────────────────────────────────────────────

    def getToolbarIcon(self):
        """Return the toolbar icon NSImage."""
        verbosePrint("TransmutorTool::getToolbarIcon")
        return _toolbarIcon

    def getToolbarTip(self) -> str:
        """Return toolbar tooltip text."""
        verbosePrint("TransmutorTool::getToolbarTip")
        return f"Transmutor v{VERSION}"

    # ──────────────────────────────────────────────
    #  Lifecycle
    # ──────────────────────────────────────────────

    def setup(self) -> None:
        """Called once when the tool class is first initialized."""
        verbosePrint("TransmutorTool::setup")
        self._model: TransmutorModel = TransmutorModel()
        self._layers: TransmutorView = TransmutorView()
        self._inspector: Optional[TransmutorInspector] = None

        # Drag state
        self._dragAction: Optional[str] = None    # 'scaling', 'moving', 'interpolating'
        self._dragHandle: Optional[str] = None     # handle key from HANDLE_MAP
        self._dragOrigin: Tuple[float, float] = (0.5, 0.5)  # transform origin
        self._dragStartPt: Optional[Tuple[float, float]] = None
        self._clickOffsetX: float = 0
        self._clickOffsetY: float = 0
        self._undoOpen: bool = False

    def becomeActive(self) -> None:
        """Called when the tool becomes the active editing tool."""
        verbosePrint("TransmutorTool::becomeActive")
        verbosePrint("TransmutorTool: becomeActive")

        glyphWindow = CurrentGlyphWindow()
        if glyphWindow is not None:
            self._layers.setupContainers(glyphWindow)

        self._model.currentFont = CurrentFont()
        self._model.currentGlyph = CurrentGlyph()
        self._resetFontList()

        if self._inspector is None:
            self._inspector = TransmutorInspector(self)
        else:
            self._inspector.w.open()
            self._refreshFontTable()

        self._redraw()

    def becomeInactive(self) -> None:
        """Called when switching away from this tool."""
        verbosePrint("TransmutorTool::becomeInactive")
        verbosePrint("TransmutorTool: becomeInactive")
        self._layers.teardownContainers()
        self._model.activeBlock = None

    def currentGlyphChanged(self) -> None:
        """Called when the current glyph changes."""
        verbosePrint("TransmutorTool::currentGlyphChanged")
        self._model.currentFont = CurrentFont()
        self._model.currentGlyph = CurrentGlyph()
        self._model.activeBlock = None
        self._resetFontList()
        self._redraw()

    # ──────────────────────────────────────────────
    #  Mouse events
    # ──────────────────────────────────────────────

    def mouseDown(self, point: Tuple[float, float], clickCount: int) -> None:
        """Handle mouse down in the glyph editor.

        Args:
            point: Click position in glyph coordinates.
            clickCount: Number of rapid clicks (double-click, etc.).
        """
        verbosePrint("TransmutorTool::mouseDown")
        verbosePrint(f"mouseDown: {point}")
        x, y = point
        self._dragAction = None
        self._dragHandle = None
        self._dragStartPt = (x, y)

        block = self._model.activeBlock
        glyph = self._model.currentGlyph

        if block is not None and block.sourceGlyph:
            # We have an active block being previewed — check handle/bounds hit
            scaledGlyph = self._model.getScaledGlyph(block)
            if scaledGlyph is not None and scaledGlyph.bounds is not None:
                # Offset the bounds for hit testing
                b = scaledGlyph.bounds
                offsetBounds = (
                    b[0] + block.offsetX,
                    b[1] + block.offsetY,
                    b[2] + block.offsetX,
                    b[3] + block.offsetY,
                )

                viewScale = self._getViewScale()

                # Check handles first
                handle = hitTestHandles((x, y), offsetBounds, viewScale)
                if handle is not None:
                    self._dragAction = "scaling"
                    self._dragHandle = handle
                    handleInfo = HANDLE_MAP[handle]
                    self._dragOrigin = handleInfo["origin"]
                    return

                # Check inside bounds
                if hitTestBounds((x, y), offsetBounds):
                    modifiers = self.modifiers()
                    if modifiers.get("option", False):
                        self._dragAction = "interpolating"
                    else:
                        self._dragAction = "moving"
                        self._clickOffsetX = x - block.offsetX
                        self._clickOffsetY = y - block.offsetY
                    return

        # Click outside active block — check if we hit an existing block
        if glyph is not None:
            hitBlock = self._model.findBlockAtPoint(glyph, (x, y))
            if hitBlock is not None:
                self._model.activeBlock = hitBlock
                self._syncSlidersFromBlock()
                self._redraw()
                return

        # Click on nothing — deselect
        if block is not None and block.sourceGlyph:
            # Don't deselect a preview that hasn't been inserted yet
            pass
        else:
            self._model.activeBlock = None
            self._layers.clearAll()

    def mouseDragged(self, point: Tuple[float, float], delta: Tuple[float, float]) -> None:
        """Handle mouse drag in the glyph editor.

        Args:
            point: Current mouse position in glyph coordinates.
            delta: Movement delta since last event.
        """
        verbosePrint("TransmutorTool::mouseDragged")
        if self._dragAction is None or self._model.activeBlock is None:
            return

        x, y = point
        block = self._model.activeBlock
        modifiers = self.modifiers()
        shiftDown = modifiers.get("shift", False)

        if self._dragAction == "scaling" and self._dragHandle is not None:
            self._handleScaleDrag(x, y, block, shiftDown)
        elif self._dragAction == "moving":
            block.offsetX = x - self._clickOffsetX
            block.offsetY = y - self._clickOffsetY
        elif self._dragAction == "interpolating":
            self._handleInterpolateDrag(x, y, block, shiftDown)

        self._syncSlidersFromBlock()
        self._redraw()

    def mouseUp(self, point: Tuple[float, float]) -> None:
        """Handle mouse up in the glyph editor.

        Args:
            point: Release position in glyph coordinates.
        """
        verbosePrint("TransmutorTool::mouseUp")
        verbosePrint(f"mouseUp: {point}")

        # Reset drag state
        self._dragAction = None
        self._dragHandle = None
        self._dragStartPt = None
        self._dragOrigin = (0.5, 0.5)

    def keyDown(self, event: Any) -> None:
        """Handle key presses.

        Args:
            event: The NSEvent for the key press.
        """
        verbosePrint("TransmutorTool::keyDown")
        keyCode = event.keyCode()

        # Escape — deselect active block
        if keyCode == 53:
            self._model.activeBlock = None
            self._layers.clearAll()
            return

        # Delete/Backspace — remove active block if it's been inserted
        if keyCode in (51, 117):
            glyph = self._model.currentGlyph
            block = self._model.activeBlock
            if glyph is not None and block is not None:
                # Check if this block exists in the glyph lib (i.e., was inserted)
                existingBlock = self._model.getBlockData(glyph, block.blockId)
                if existingBlock is not None:
                    with glyph.undo("Transmutor Remove"):
                        self._model.removeBlock(glyph, block.blockId)
                    self._model.activeBlock = None
                    self._layers.clearAll()

    # ──────────────────────────────────────────────
    #  Private helpers
    # ──────────────────────────────────────────────

    def _handleScaleDrag(
        self,
        x: float,
        y: float,
        block: BlockData,
        shiftDown: bool,
    ) -> None:
        """Process a scaling drag operation.

        Computes new scaleH/scaleV from the drag distance relative to
        the transform origin, with safety clamping.

        Args:
            x, y: Current mouse position.
            block: The active block being scaled.
            shiftDown: Whether Shift is held (constrain proportions).
        """
        verbosePrint("TransmutorTool::_handleScaleDrag")
        scaledGlyph = self._model.getScaledGlyph(block)
        if scaledGlyph is None or scaledGlyph.bounds is None:
            return

        b = scaledGlyph.bounds
        handleInfo = HANDLE_MAP[self._dragHandle]
        corner = handleInfo["corner"]
        origin = handleInfo["origin"]

        # Compute origin point (in glyph coordinates with offset)
        originPt = (
            _interpolate(b[0], b[2], origin[0]) + block.offsetX,
            _interpolate(b[1], b[3], origin[1]) + block.offsetY,
        )

        # Compute the unscaled corner point at scale=1
        # We need a reference distance to compute scale ratio
        unscaledGlyph = self._model.engine.getScaledGlyph(
            glyphName=block.sourceGlyph,
            scaleH=1.0,
            scaleV=1.0,
            stemRatioV=block.stemRatioV,
            stemRatioH=block.stemRatioH,
            referenceHeight=self._model.currentFont.info.unitsPerEm if self._model.currentFont else 1000,
        )
        if unscaledGlyph is None or unscaledGlyph.bounds is None:
            return

        ub = unscaledGlyph.bounds
        unscaledCornerPt = (
            _interpolate(ub[0], ub[2], corner[0]) + block.offsetX,
            _interpolate(ub[1], ub[3], corner[1]) + block.offsetY,
        )

        # Distance from origin to unscaled corner (reference)
        totalDistH = unscaledCornerPt[0] - originPt[0]
        totalDistV = unscaledCornerPt[1] - originPt[1]

        # Distance from origin to current mouse
        currentDistH = x - originPt[0]
        currentDistV = y - originPt[1]

        # Calculate new scale values
        if abs(totalDistV) > 0.001:
            newScaleV = currentDistV / totalDistV
        else:
            newScaleV = block.scaleV

        if abs(totalDistH) > 0.001:
            if shiftDown:
                newScaleH = newScaleV  # Constrain proportions
            else:
                newScaleH = currentDistH / totalDistH
        else:
            newScaleH = block.scaleH

        # Clamp to safe bounds
        block.scaleV = _clamp(newScaleV, MIN_SCALE, MAX_SCALE)
        block.scaleH = _clamp(newScaleH, MIN_SCALE, MAX_SCALE)

    def _handleInterpolateDrag(
        self,
        x: float,
        y: float,
        block: BlockData,
        shiftDown: bool,
    ) -> None:
        """Process an interpolation (Option+drag) operation.

        Adjusts stem weight ratios based on drag distance.

        Args:
            x, y: Current mouse position.
            block: The active block.
            shiftDown: Whether Shift is held (independent H/V control).
        """
        verbosePrint("TransmutorTool::_handleInterpolateDrag")
        if self._dragStartPt is None:
            return

        viewScale = self._getViewScale()
        dampener = 200 * (1 / max(viewScale, 0.001))

        dragDeltaX = (x - self._dragStartPt[0]) / max(dampener, 1)
        dragDeltaY = (y - self._dragStartPt[1]) / max(dampener, 1)

        # Clamp drag to [-1, 3] range around base of 1
        newRatioX = _clamp(dragDeltaX + 1, 0.0, 4.0)
        newRatioY = _clamp(dragDeltaY + 1, 0.0, 4.0)

        if shiftDown:
            block.stemRatioH = newRatioY
            block.stemRatioV = newRatioX
        else:
            block.stemRatioH = newRatioX
            block.stemRatioV = newRatioX

    def _resetFontList(self) -> None:
        """Refresh the font list from all open fonts."""
        verbosePrint("TransmutorTool::_resetFontList")
        allFonts = list(AllFonts(sortOptions=["magic"]))
        self._model.setFonts(allFonts)

    def _getViewScale(self) -> float:
        """Get the current glyph view scale factor."""
        verbosePrint("TransmutorTool::_getViewScale")
        try:
            glyphWindow = CurrentGlyphWindow()
            if glyphWindow is not None:
                return glyphWindow.getGlyphViewScale()
        except Exception:
            pass
        return 1.0

    def _redraw(self) -> None:
        """Redraw the Transmutor visual elements."""
        verbosePrint("TransmutorTool::_redraw")
        if not self._layers.hasContainers:
            return

        block = self._model.activeBlock
        if block is None or not block.sourceGlyph:
            self._layers.clearAll()
            return

        scaledGlyph = self._model.getScaledGlyph(block)
        if scaledGlyph is None or scaledGlyph.bounds is None:
            self._layers.clearAll()
            return

        offset = (block.offsetX, block.offsetY)

        # Get measurements from the current glyph editor if available
        measurements = None
        try:
            if self._model.currentGlyph is not None:
                measurements = self._model.currentGlyph.naked().measurements
        except Exception:
            pass

        self._layers.drawBlock(
            bounds=scaledGlyph.bounds,
            scaledGlyph=scaledGlyph,
            offset=offset,
            measurements=measurements,
        )

    def _syncSlidersFromBlock(self) -> None:
        """Sync the inspector sliders to match the active block's values."""
        verbosePrint("TransmutorTool::_syncSlidersFromBlock")
        if self._inspector is not None:
            self._inspector.syncFromBlock(self._model.activeBlock)

    def _refreshFontTable(self) -> None:
        """Refresh the source font table in the inspector panel."""
        verbosePrint("TransmutorTool::_refreshFontTable")
        if self._inspector is not None:
            self._inspector.refreshFontTable()
