"""
Transmutor v3 — Model.

Owns the interpolation state, font/stem data, scale parameters,
and glyph lib read/write. Each Transmutor "block" is a set of
contours inserted from an interpolation source, tagged with a
unique ID for later re-editing.
"""

import uuid
from typing import Any, Dict, List, Optional, Tuple

from fontParts.fontshell import RGlyph

from constants import (
    DEFAULT_SCALE,
    DEFAULT_STEM_RATIO,
    LIB_KEY_BLOCKS,
    LIB_KEY_BLOCK_ID,
    verbosePrint,
)
from interpolationEngine import InterpolationEngine
from stemMeasurer import StemMeasurer, makeFontDisplayName


class BlockData:
    """Data describing a single Transmutor block in a glyph.

    A block represents a set of contours inserted by Transmutor
    from a specific source glyph with specific scale/stem parameters.

    Attributes:
        blockId: Unique identifier for this block.
        sourceGlyph: Name of the source glyph.
        scaleH: Horizontal scale factor.
        scaleV: Vertical scale factor.
        stemRatioV: Vertical stem weight ratio.
        stemRatioH: Horizontal stem weight ratio.
        offsetX: Horizontal offset position.
        offsetY: Vertical offset position.
    """

    def __init__(
        self,
        blockId: Optional[str] = None,
        sourceGlyph: str = "",
        scaleH: float = DEFAULT_SCALE,
        scaleV: float = DEFAULT_SCALE,
        stemRatioV: float = DEFAULT_STEM_RATIO,
        stemRatioH: float = DEFAULT_STEM_RATIO,
        offsetX: float = 0,
        offsetY: float = 0,
    ) -> None:
        verbosePrint("BlockData::__init__")
        self.blockId: str = blockId or str(uuid.uuid4())
        self.sourceGlyph: str = sourceGlyph
        self.scaleH: float = scaleH
        self.scaleV: float = scaleV
        self.stemRatioV: float = stemRatioV
        self.stemRatioH: float = stemRatioH
        self.offsetX: float = offsetX
        self.offsetY: float = offsetY

    def toDict(self) -> Dict[str, Any]:
        """Serialize this block to a dictionary for glyph lib storage."""
        verbosePrint("BlockData::toDict")
        return {
            "id": self.blockId,
            "sourceGlyph": self.sourceGlyph,
            "scaleH": self.scaleH,
            "scaleV": self.scaleV,
            "stemRatioV": self.stemRatioV,
            "stemRatioH": self.stemRatioH,
            "offset": (self.offsetX, self.offsetY),
        }

    @classmethod
    def fromDict(cls, data: Dict[str, Any]) -> "BlockData":
        """Deserialize a block from a glyph lib dictionary.

        Args:
            data: Dictionary from glyph.lib storage.

        Returns:
            A new BlockData instance.
        """
        verbosePrint("BlockData::fromDict")
        offset = data.get("offset", (0, 0))
        return cls(
            blockId=data.get("id"),
            sourceGlyph=data.get("sourceGlyph", ""),
            scaleH=data.get("scaleH", DEFAULT_SCALE),
            scaleV=data.get("scaleV", DEFAULT_SCALE),
            stemRatioV=data.get("stemRatioV", DEFAULT_STEM_RATIO),
            stemRatioH=data.get("stemRatioH", DEFAULT_STEM_RATIO),
            offsetX=offset[0] if len(offset) > 0 else 0,
            offsetY=offset[1] if len(offset) > 1 else 0,
        )


class TransmutorModel:
    """Core data model for the Transmutor tool.

    Manages the interpolation engine, active fonts, scale/stem parameters,
    and the read/write of Transmutor blocks to/from glyph libs.
    """

    def __init__(self) -> None:
        verbosePrint("TransmutorModel::__init__")
        self._engine: InterpolationEngine = InterpolationEngine()
        self._stemMeasurer: StemMeasurer = StemMeasurer()

        # Current state
        self.allFonts: List[Any] = []
        self.activeFonts: List[Any] = []
        self.currentFont: Optional[Any] = None
        self.currentGlyph: Optional[Any] = None

        # Active block being edited (or None)
        self.activeBlock: Optional[BlockData] = None

        # Constrain toggles
        self.constrainScale: bool = True
        self.constrainStems: bool = True

    @property
    def engine(self) -> InterpolationEngine:
        """The interpolation engine."""
        return self._engine

    @property
    def stemMeasurer(self) -> StemMeasurer:
        """The stem measurement utility."""
        return self._stemMeasurer

    # ──────────────────────────────────────────────
    #  Font management
    # ──────────────────────────────────────────────

    def setFonts(self, allFonts: List[Any]) -> None:
        """Set the available fonts and default all as active.

        Args:
            allFonts: List of all open RoboFont font objects.
        """
        verbosePrint("TransmutorModel::setFonts")
        self.allFonts = list(allFonts)
        self.activeFonts = [f for f in self.allFonts if f.info.familyName]
        self._rebuildEngine()

    def setActiveFonts(self, activeFonts: List[Any]) -> None:
        """Update which fonts are active for interpolation.

        Args:
            activeFonts: List of fonts to use as masters.
        """
        verbosePrint("TransmutorModel::setActiveFonts")
        self.activeFonts = list(activeFonts)
        self._rebuildEngine()

    def _rebuildEngine(self) -> None:
        """Rebuild the interpolation engine with current active fonts."""
        verbosePrint("TransmutorModel::_rebuildEngine")
        if self.activeFonts:
            self._engine.setMasters(self.activeFonts)
        else:
            self._engine.clearMasters()

    def getFontStemData(self) -> List[Dict[str, Any]]:
        """Return stem data for all fonts, for display in the UI.

        Returns:
            List of dicts with 'font', 'vStem', 'hStem', 'active' keys.
        """
        verbosePrint("TransmutorModel::getFontStemData")
        result = []
        for font in self.allFonts:
            vStem, hStem = self._stemMeasurer.getStems(font)
            result.append({
                "font": makeFontDisplayName(font),
                "vStem": vStem,
                "hStem": hStem,
                "active": font in self.activeFonts,
            })
        return result

    # ──────────────────────────────────────────────
    #  Scaling / Interpolation
    # ──────────────────────────────────────────────

    def getScaledGlyph(self, block: Optional[BlockData] = None) -> Optional[RGlyph]:
        """Get a scaled/interpolated glyph for the given or active block.

        Args:
            block: The block data to use. If None, uses self.activeBlock.

        Returns:
            An RGlyph with the result, or None.
        """
        verbosePrint("TransmutorModel::getScaledGlyph")
        block = block or self.activeBlock
        if block is None:
            return None

        if not block.sourceGlyph:
            return None

        referenceHeight = None
        if self.currentFont is not None:
            referenceHeight = self.currentFont.info.unitsPerEm

        return self._engine.getScaledGlyph(
            glyphName=block.sourceGlyph,
            scaleH=block.scaleH,
            scaleV=block.scaleV,
            stemRatioV=block.stemRatioV,
            stemRatioH=block.stemRatioH,
            referenceHeight=referenceHeight,
        )

    # ──────────────────────────────────────────────
    #  Block management (glyph lib read/write)
    # ──────────────────────────────────────────────

    def insertBlock(self, glyph: Any, block: Optional[BlockData] = None) -> Optional[str]:
        """Insert a new Transmutor block into a glyph.

        Creates scaled contours and writes them into the glyph,
        tagging each contour with the block ID via public.objectLibs.

        Args:
            glyph: The target glyph to insert contours into.
            block: The block data. If None, uses self.activeBlock.

        Returns:
            The block ID if successful, or None.
        """
        verbosePrint("TransmutorModel::insertBlock")
        block = block or self.activeBlock
        if block is None:
            verbosePrint("insertBlock: no block data")
            return None

        scaledGlyph = self.getScaledGlyph(block)
        if scaledGlyph is None:
            verbosePrint("insertBlock: scaling failed")
            return None

        # Apply offset
        if block.offsetX or block.offsetY:
            scaledGlyph.moveBy((block.offsetX, block.offsetY))

        scaledGlyph.round()

        # Track which contour indices we're about to add
        startIndex = len(glyph.contours)

        # Append the scaled contours
        glyph.appendGlyph(scaledGlyph)

        # Tag the new contours with the block ID
        objectLibs = dict(glyph.lib.get("public.objectLibs", {}))
        for i in range(startIndex, len(glyph.contours)):
            contour = glyph.contours[i]
            identifier = contour.getIdentifier()
            objectLibs[identifier] = {
                LIB_KEY_BLOCK_ID: block.blockId,
            }
        glyph.lib["public.objectLibs"] = objectLibs

        # Write block data to glyph lib
        blocks = list(glyph.lib.get(LIB_KEY_BLOCKS, []))
        blocks.append(block.toDict())
        glyph.lib[LIB_KEY_BLOCKS] = blocks

        verbosePrint(f"insertBlock: inserted block {block.blockId} with {len(glyph.contours) - startIndex} contours")
        return block.blockId

    def updateBlock(self, glyph: Any, blockId: str, block: Optional[BlockData] = None) -> bool:
        """Update an existing block's contours in place.

        Removes old contours for this block, then re-inserts with
        the updated parameters.

        Args:
            glyph: The target glyph.
            blockId: The block ID to update.
            block: New block data. If None, uses self.activeBlock.

        Returns:
            True if the update succeeded.
        """
        verbosePrint("TransmutorModel::updateBlock")
        block = block or self.activeBlock
        if block is None:
            return False

        # Ensure block ID matches
        block.blockId = blockId

        # Remove old contours for this block
        self._removeBlockContours(glyph, blockId)

        # Update the block data in the lib
        self._updateBlockLibEntry(glyph, block)

        # Re-insert contours
        scaledGlyph = self.getScaledGlyph(block)
        if scaledGlyph is None:
            return False

        if block.offsetX or block.offsetY:
            scaledGlyph.moveBy((block.offsetX, block.offsetY))

        scaledGlyph.round()

        startIndex = len(glyph.contours)
        glyph.appendGlyph(scaledGlyph)

        # Tag new contours
        objectLibs = dict(glyph.lib.get("public.objectLibs", {}))
        for i in range(startIndex, len(glyph.contours)):
            contour = glyph.contours[i]
            identifier = contour.getIdentifier()
            objectLibs[identifier] = {
                LIB_KEY_BLOCK_ID: blockId,
            }
        glyph.lib["public.objectLibs"] = objectLibs

        return True

    def removeBlock(self, glyph: Any, blockId: str) -> bool:
        """Remove a Transmutor block and its contours from a glyph.

        Args:
            glyph: The target glyph.
            blockId: The block ID to remove.

        Returns:
            True if the block was found and removed.
        """
        verbosePrint("TransmutorModel::removeBlock")
        removed = self._removeBlockContours(glyph, blockId)

        # Remove from blocks lib
        blocks = list(glyph.lib.get(LIB_KEY_BLOCKS, []))
        blocks = [b for b in blocks if b.get("id") != blockId]
        if blocks:
            glyph.lib[LIB_KEY_BLOCKS] = blocks
        else:
            # Clean up empty lib key
            if LIB_KEY_BLOCKS in glyph.lib:
                del glyph.lib[LIB_KEY_BLOCKS]

        return removed

    def findBlockAtPoint(self, glyph: Any, point: Tuple[float, float]) -> Optional[BlockData]:
        """Find a Transmutor block whose contours contain the given point.

        Hit-tests the glyph's contours, and if a matching contour belongs
        to a Transmutor block, returns that block's data.

        Args:
            glyph: The glyph to search.
            point: The (x, y) coordinate to test.

        Returns:
            BlockData for the hit block, or None.
        """
        verbosePrint("TransmutorModel::findBlockAtPoint")
        objectLibs = glyph.lib.get("public.objectLibs", {})

        for contour in glyph.contours:
            bounds = contour.bounds
            if bounds is None:
                continue

            xMin, yMin, xMax, yMax = bounds
            px, py = point

            if xMin <= px <= xMax and yMin <= py <= yMax:
                # Point is within contour bounds; check if it's a Transmutor contour
                identifier = contour.getIdentifier()
                contourLib = objectLibs.get(identifier, {})
                blockId = contourLib.get(LIB_KEY_BLOCK_ID)

                if blockId is not None:
                    return self._getBlockData(glyph, blockId)

        return None

    def getBlockData(self, glyph: Any, blockId: str) -> Optional[BlockData]:
        """Public accessor for block data by ID.

        Args:
            glyph: The glyph containing the block.
            blockId: The block ID to look up.

        Returns:
            BlockData or None.
        """
        verbosePrint("TransmutorModel::getBlockData")
        return self._getBlockData(glyph, blockId)

    def getBlockBounds(self, glyph: Any, blockId: str) -> Optional[Tuple[float, float, float, float]]:
        """Get the bounding box of a block's contours.

        Args:
            glyph: The glyph containing the block.
            blockId: The block ID.

        Returns:
            (xMin, yMin, xMax, yMax) or None.
        """
        verbosePrint("TransmutorModel::getBlockBounds")
        contours = self._getBlockContours(glyph, blockId)
        if not contours:
            return None

        xMin = yMin = float("inf")
        xMax = yMax = float("-inf")

        for contour in contours:
            bounds = contour.bounds
            if bounds is None:
                continue
            xMin = min(xMin, bounds[0])
            yMin = min(yMin, bounds[1])
            xMax = max(xMax, bounds[2])
            yMax = max(yMax, bounds[3])

        if xMin == float("inf"):
            return None

        return (xMin, yMin, xMax, yMax)

    def getAllBlocks(self, glyph: Any) -> List[BlockData]:
        """Get all Transmutor blocks in a glyph.

        Args:
            glyph: The glyph to inspect.

        Returns:
            List of BlockData instances.
        """
        verbosePrint("TransmutorModel::getAllBlocks")
        blocks = glyph.lib.get(LIB_KEY_BLOCKS, [])
        return [BlockData.fromDict(b) for b in blocks]

    # ──────────────────────────────────────────────
    #  Private helpers
    # ──────────────────────────────────────────────

    def _getBlockData(self, glyph: Any, blockId: str) -> Optional[BlockData]:
        """Look up block data in the glyph lib by ID.

        Args:
            glyph: The glyph containing the block.
            blockId: The block ID to find.

        Returns:
            BlockData or None.
        """
        verbosePrint("TransmutorModel::_getBlockData")
        blocks = glyph.lib.get(LIB_KEY_BLOCKS, [])
        for b in blocks:
            if b.get("id") == blockId:
                return BlockData.fromDict(b)
        return None

    def _getBlockContours(self, glyph: Any, blockId: str) -> List[Any]:
        """Get all contours belonging to a block.

        Args:
            glyph: The glyph to search.
            blockId: The block ID.

        Returns:
            List of contour objects.
        """
        verbosePrint("TransmutorModel::_getBlockContours")
        objectLibs = glyph.lib.get("public.objectLibs", {})
        contours = []
        for contour in glyph.contours:
            identifier = contour.getIdentifier()
            contourLib = objectLibs.get(identifier, {})
            if contourLib.get(LIB_KEY_BLOCK_ID) == blockId:
                contours.append(contour)
        return contours

    def _removeBlockContours(self, glyph: Any, blockId: str) -> bool:
        """Remove all contours belonging to a block from a glyph.

        Args:
            glyph: The glyph to modify.
            blockId: The block ID whose contours to remove.

        Returns:
            True if any contours were removed.
        """
        verbosePrint("TransmutorModel::_removeBlockContours")
        objectLibs = dict(glyph.lib.get("public.objectLibs", {}))
        contoursToRemove = []

        for contour in glyph.contours:
            identifier = contour.getIdentifier()
            contourLib = objectLibs.get(identifier, {})
            if contourLib.get(LIB_KEY_BLOCK_ID) == blockId:
                contoursToRemove.append(contour)
                # Clean up objectLibs entry
                objectLibs.pop(identifier, None)

        if not contoursToRemove:
            return False

        # Remove contours in reverse order to preserve indices
        for contour in reversed(contoursToRemove):
            glyph.removeContour(contour)

        glyph.lib["public.objectLibs"] = objectLibs
        return True

    def _updateBlockLibEntry(self, glyph: Any, block: BlockData) -> None:
        """Update or insert a block's data in the glyph lib.

        Args:
            glyph: The glyph to update.
            block: The block data to write.
        """
        verbosePrint("TransmutorModel::_updateBlockLibEntry")
        blocks = list(glyph.lib.get(LIB_KEY_BLOCKS, []))
        updated = False
        for i, b in enumerate(blocks):
            if b.get("id") == block.blockId:
                blocks[i] = block.toDict()
                updated = True
                break

        if not updated:
            blocks.append(block.toDict())

        glyph.lib[LIB_KEY_BLOCKS] = blocks
