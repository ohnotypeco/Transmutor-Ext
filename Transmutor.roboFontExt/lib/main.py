import functools
import site
import os

import AppKit
import ezui
# from lib.tools.defaults import getDefaultColor
from mojo.events import BaseEventTool, installTool
from mojo.roboFont import version
from mojo.UI import CurrentGlyphWindow, getDefault

if version > "4.4":
    from mojo.UI import appearanceColorKey

# if mutatorScale is already available, used that
try:
    import mutatorScale
except ImportError:
    # get the path to the MutatorScale lib folder    
    mutatorScaleLibFolder = os.path.join(os.getcwd(), "..", "submodules", "MutatorScale", "lib")
    print(mutatorScaleLibFolder)
    print(os.path.exists(mutatorScaleLibFolder))
    # add the MutatorScale lib folder to the site-packages
    site.addsitedir(mutatorScaleLibFolder)
finally:
    from mutatorScale.objects.scaler import MutatorScaleEngine
    from mutatorScale.utilities.fontUtils import getRefStems, makeListFontName
    
VERBOSE = False
EXTENSION_IDENTIFIER = "co.ohnotype.Transmutor"
VERSION = "0.1"


def verbosePrint(s):
    if VERBOSE:
        print(s)


_memoizeCache = dict()


def clearMemoizeCache():
    # clears all memoized caches
    # this is intended as the usage of memoize is made per context
    _memoizeCache.clear()


def cache(function):
    """
    Memoize a function's return value with the function's arguments.
    The next time a function is called with the same arguments, the cache is returned.
    Example usage:
        @cache
        def addNumbers(first, second):
            return first + second
        # The first time this function is called the calculation will be made,
        # and and the result will be stored in the cache dict as [first, second]: returnValue
        # From then on, this value will be returned when the same argument is made to the addNumbers function
    """
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        key = (function, args, frozenset(kwargs.items()))
        if key in _memoizeCache:
            return _memoizeCache[key]
        else:
            result = function(*args, **kwargs)
            _memoizeCache[key] = result
            return result
    return wrapper


def interpolate(a, b, v):
    return a + (b - a) * v


def norm(v, a, b):
    return (v - a) / (b - a)


@cache
def getRefStemsCached(font):
    return getRefStems(font)


class TransmutorModel():
    currentGlyph = None
    currentFont = None
    allFonts = []
    activeFonts = []
    sourceGlyphName = None
    scaler = None

    scaledGlyphBounds = None
    unScaledGlyphBounds = None
    scaleV = 1.0
    scaleH = 1.0
    offsetX = None
    offsetY = None
    stemWtRatioV = 1.0
    stemWtRatioH = 1.0

    transformOrigin = (0.5, 0.5)

    scaledGlyphColor = (1.0, 0.0, 0.0, 0.5)

    @property
    def previewColor(self):
        verbosePrint("TransmutorModel::previewColor")
        if version > "4.4":
            return getDefault(appearanceColorKey("glyphViewPreviewFillColor"))
        else:
            return getDefault("glyphViewPreviewFillColor")

    @property
    def use45Constraint(self):
        verbosePrint("TransmutorModel::use45Constraint")
        return getDefault("glyphViewShouldUse45Contrain", True)

    def updateScaler(self):
        if self.allFonts:
            self.scaler = MutatorScaleEngine(self.activeFonts)

    def getScaledGlyph(self):
        verbosePrint("TransmutorModel::getScaledGlyph")
        if self.currentFont is not None and self.currentGlyph is not None:
            if self.sourceGlyphName != self.currentGlyph.name and self.sourceGlyphName in self.currentFont.keys():
                currentFontName = makeListFontName(self.currentFont)
                if currentFontName in self.scaler.masters:
                    stems = (self.scaler.masters[currentFontName].vstem * self.stemWtRatioV,
                             self.scaler.masters[currentFontName].hstem * self.stemWtRatioH)
                elif len(self.scaler.masters):
                    stems = (self.scaler.masters[list(self.scaler.masters.keys())[0]].vstem * self.stemWtRatioV,
                             self.scaler.masters[list(self.scaler.masters.keys())[0]].hstem * self.stemWtRatioH)
                else:
                    stems = (0, 0)

                self.scaler.set({
                    "width": 1,
                    "targetHeight": self.currentFont.info.unitsPerEm,
                    "referenceHeight": self.currentFont.info.unitsPerEm,
                })

                unScaledGlyph = self.scaler.getScaledGlyph(self.sourceGlyphName, stems)
                originPt = (interpolate(unScaledGlyph.bounds[0], unScaledGlyph.bounds[2], self.transformOrigin[0]),
                            interpolate(unScaledGlyph.bounds[1], unScaledGlyph.bounds[3], self.transformOrigin[1]))
                # move the glyph so the center is at (0,0)
                unScaledGlyph.moveBy((-originPt[0], -originPt[1]))
                self.unScaledGlyphBounds = unScaledGlyph.bounds

                self.scaler.set({
                    "width": self.scaleH,
                    "targetHeight": self.currentFont.info.unitsPerEm * self.scaleV,
                    "referenceHeight": self.currentFont.info.unitsPerEm,
                })

                newGlyph = self.scaler.getScaledGlyph(self.sourceGlyphName, stems)
                originPt = (interpolate(newGlyph.bounds[0], newGlyph.bounds[2], self.transformOrigin[0]),
                            interpolate(newGlyph.bounds[1], newGlyph.bounds[3], self.transformOrigin[1]))
                # move the glyph so the center is at (0,0)
                newGlyph.moveBy((-originPt[0], -originPt[1]))
                self.scaledGlyphBounds = newGlyph.bounds

                # self.offsetX += originPt[0]
                # self.offsetY += originPt[1]

                return newGlyph

            else:
                print("Source glyph cannot be the same as the current glyph.")

        return None


class TransmutorPanel(ezui.WindowController):
    def build(self, controller):
        verbosePrint("TransmutorPanel::build")
        self.controller = controller
        self.model = self.controller.model
        self.model.allFonts = [font for font in AllFonts(sortOptions=["magic"])]
        self.model.activeFonts = [font for font in self.model.allFonts if font.info.familyName]
        self.model.updateScaler()

        # TODO: The table is not working, I don't think it works with two column forms

        content = """
        = TwoColumnForm
        
        : Glyph:
        [__]                        @glyphNamesTextBox
        ----------------------
        
        : Sources:
        |--------------------------------------------|     @sourceFontTable
        | selected | font     | vStem    | hStem     |
        |----------|----------|----------| ----------|
        |          |          |          |           |
        |--------------------------------------------|
        
        ----------------------
        
        : V Stem:
        ---X--- [__]                @stemWtRatioVSlider
        : H Stem:
        ---X--- [__]                @stemWtRatioHSlider
        :
        [X] Constrain               @constrainStemWtRatioSwitch
        
        -----------------------
        
        : Height:
        ---X--- [__]                @scaleVSlider
        : Width:
        ---X--- [__]                @scaleHSlider
        
        =======================
        
        (Add to Current Glyph)   @addToGlyphButton
        
        """

        descriptionData = dict(
            content=dict(
                titleColumnWidth=60,
                itemColumnWidth=350,
            ),
            glyphNamesTextBox=dict(
                placeholder="Glyph name",
            ),
            sourceFontTable=dict(
                columnDescriptions=[
                    dict(
                        identifier="selected",
                        title="",
                        width=20,
                        editable=True,
                        cellDescription=dict(
                            cellType="Checkbox",
                        )
                    ),
                    dict(
                        identifier="font",
                        title="Font",
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
                    )
                ],
                items=[],
                height=150,
                showColumnTitles=True,
                alternatingRowColors=True
            ),
            stemWtRatioHSlider=dict(
                valueType="float",
                value=1.0,
                minValue=0.0,
                maxValue=2.0,
                tickMarks=5,
            ),
            stemWtRatioVSlider=dict(
                valueType="float",
                value=1.0,
                minValue=0.0,
                maxValue=2.0,
                tickMarks=5,
            ),
            scaleHSlider=dict(
                valueType="float",
                value=1.0,
                minValue=0.0,
                maxValue=2.0,
                tickMarks=5,
            ),
            scaleVSlider=dict(
                valueType="float",
                value=1.0,
                minValue=0.0,
                maxValue=2.0,
                tickMarks=5,
            ),
        )

        self.w = ezui.EZPanel(
            content=content,
            descriptionData=descriptionData,
            controller=self,
            title="Transmutor",
            autosaveName="TransmutorPanel",
            activeItem="glyphNamesTextBox",
            size=(300, "auto"),
            closable=False,
        )
        # self.w.getNSWindow().setTitlebarHeight_(22)
        # self.w.getNSWindow().setTitlebarAppearsTransparent_(True)

        # window = self.w.getNSWindow()
        # styleMask = window.styleMask()
        # styleMask -= AppKit.NSWindowStyleMaskClosable
        # window.setStyleMask_(styleMask)

        self.stemWtRatioVSlider = self.w.getItem("stemWtRatioVSlider")
        self.stemWtRatioHSlider = self.w.getItem("stemWtRatioHSlider")

        self.stemWtRatioHSlider.enable(False)

        self.constrainSwitch = self.w.getItem("constrainStemWtRatioSwitch")

        self.refreshFromModel()

    def started(self):
        verbosePrint("TransmutorPanel::started")
        self.w.open()

    def glyphNamesTextBoxCallback(self, sender):
        verbosePrint("TransmutorPanel::glyphNamesTextBoxCallback")
        self.model.sourceGlyphName = sender.get()
        if self.model.offsetX == None and self.model.offsetY == None:
            self.model.offsetX = interpolate(
                self.model.currentFont[self.model.sourceGlyphName].bounds[0],
                self.model.currentFont[self.model.sourceGlyphName].bounds[2],
                self.model.transformOrigin[0])
            self.model.offsetY = interpolate(
                self.model.currentFont[self.model.sourceGlyphName].bounds[1],
                self.model.currentFont[self.model.sourceGlyphName].bounds[3],
                self.model.transformOrigin[1])
        self.controller.redrawView()

    def sourceFontTableEditCallback(self, sender):
        verbosePrint("TransmutorPanel::sourceFontTableEditCallback")
        items = self.w.getItemValue("sourceFontTable")
        for i, item in enumerate(items):
            if item["selected"]:
                if self.model.allFonts[i] not in self.model.activeFonts:
                    self.model.activeFonts.append(self.model.allFonts[i])
            else:
                if self.model.allFonts[i] in self.model.activeFonts:
                    self.model.activeFonts.remove(self.model.allFonts[i])

        self.model.updateScaler()
        self.controller.redrawView()

    def stemWtRatioVSliderCallback(self, sender):
        verbosePrint("TransmutorPanel::stemWtRatioVSliderCallback")
        if self.constrainSwitch.get():
            self.model.stemWtRatioV = float(sender.get())
            self.model.stemWtRatioH = float(sender.get())
            self.stemWtRatioHSlider.set(float(sender.get()))
        else:
            self.model.stemWtRatioV = float(sender.get())

        self.controller.redrawView()

    def stemWtRatioVSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorPanel::stemWtRatioVSliderTextFieldCallback")
        if self.constrainSwitch.get():
            self.model.stemWtRatioV = float(sender.get())
            self.model.stemWtRatioH = float(sender.get())
            self.stemWtRatioHSlider.set(float(sender.get()))
        else:
            self.model.stemWtRatioV = float(sender.get())

        self.controller.redrawView()

    def stemWtRatioHSliderCallback(self, sender):
        verbosePrint("TransmutorPanel::stemWtRatioHSliderCallback")
        self.model.stemWtRatioH = float(sender.get())
        self.controller.redrawView()

    def stemWtRatioHSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorPanel::stemWtRatioHSliderTextFieldCallback")
        self.model.stemWtRatioH = float(sender.get())
        self.controller.redrawView()

    def constrainStemWtRatioSwitchCallback(self, sender):
        verbosePrint("TransmutorPanel::constrainStemWtRatioSwitchCallback")
        if self.constrainSwitch.get():
            self.stemWtRatioHSlider.enable(False)
            self.stemWtRatioHSlider.set(float(self.stemWtRatioVSlider.get()))
        else:
            self.stemWtRatioHSlider.enable(True)

        self.controller.redrawView()

    def scaleHSliderCallback(self, sender):
        verbosePrint("TransmutorPanel::scaleHSliderCallback")
        self.model.scaleH = float(sender.get())
        self.controller.redrawView()

    def scaleHSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorPanel::scaleHSliderTextFieldCallback")
        self.model.scaleH = float(sender.get())
        self.controller.redrawView()

    def scaleVSliderCallback(self, sender):
        verbosePrint("TransmutorPanel::scaleVSliderCallback")
        self.model.scaleV = float(sender.get())
        self.controller.redrawView()

    def scaleVSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorPanel::scaleVSliderTextFieldCallback")
        self.model.scaleV = float(sender.get())
        self.controller.redrawView()

    def addToGlyphButtonCallback(self, sender):
        verbosePrint("TransmutorPanel::addToGlyphButtonCallback")
        self.controller.addToGlyph()

    def refreshFromModel(self):
        self.w.getItem("stemWtRatioVSlider").set(self.model.stemWtRatioV)
        self.w.getItem("stemWtRatioHSlider").set(self.model.stemWtRatioH)

        self.w.getItem("scaleVSlider").set(self.model.scaleV)
        self.w.getItem("scaleHSlider").set(self.model.scaleH)

        if len(self.w.getItemValue("sourceFontTable")) > 0:
            self.w.getItem("sourceFontTable").set([])

        for font in self.model.allFonts:
            vStem, hStem = getRefStemsCached(font)
            self.w.getItem("sourceFontTable").appendItems([{
                "selected": font in self.model.activeFonts,
                "font": makeListFontName(font),
                "vStem": vStem,
                "hStem": hStem,
            }])


class TransmutorToolController(BaseEventTool):
    active = False
    downPt = None
    clickAction = None

    def getToolbarTip(self):
        return "Transmutor"

    # def getToolbarIcon(self):
    #     return(toolbarIcon)

    def setup(self):
        verbosePrint("Transmutor::setup")
        self.becomeActive()


    def becomeActive(self):
        verbosePrint("Transmutor::becomeActive")
        if not self.active:
            self.foregroundContainer = self.extensionContainer(
                identifier=EXTENSION_IDENTIFIER + ".foreground",
                location="foreground",
                clear=True
            )
            self.previewContainer = self.extensionContainer(
                identifier=EXTENSION_IDENTIFIER + ".preview",
                location="preview",
                clear=True
            )
            self.foregroundContainer.setVisible(True)
            self.previewContainer.setVisible(True)

            self.model = TransmutorModel()
            self.model.currentFont = CurrentFont()
            self.model.currentGlyph = CurrentGlyph()

            self.toolPanel = TransmutorPanel(self)

            self.active = True

            self.reset()

    def becomeInactive(self):
        verbosePrint("Transmutor::becomeInactive")
        if self.active:
            if self.foregroundContainer is None:
                return
            self.foregroundContainer.setVisible(False)
            self.previewContainer.setVisible(False)

            self.foregroundContainer.clearSublayers()
            self.previewContainer.clearSublayers()

            if self.toolPanel:
                self.toolPanel.w.close()

            del self.model
            self.model = None
            del self.toolPanel
            self.toolPanel = None

            self.active = False

    # def applicationDidBecomeActive(self, info):
    #     verbosePrint("Transmutor::applicationDidBecomeActive")
    #     if self.active == True:
    #         self.reset()

    # def applicationWillResignActive(self, info):
    #     verbosePrint("Transmutor::applicationWillResignActive")
    #     if self.active == True:
    #         self.reset()

    def fontBecameCurrent(self):
        verbosePrint("Transmutor::fontBecameCurrent")
        if self.active == True:
            self.reset()

    def fontDidOpen(self):
        verbosePrint("Transmutor::fontDidOpen")
        if self.active == True:
            self.reset()

    def viewDidChangeGlyph(self):
        verbosePrint("Transmutor::viewDidChangeGlyph")
        if self.active == True:
            self.reset()
    
    def currentGlyphChanged(self):
        verbosePrint("Transmutor::currentGlyphChanged")
        if self.active == True:
            self.reset()

    def mouseDown(self, point, clickCount):
        verbosePrint("Transmutor::mouseDown")
        if self.active:
            self.downPt = point
            self._leftMouseAction(point)

    def mouseDragged(self, point, delta):
        verbosePrint("Transmutor::mouseDragged")
        if self.active:
            self._leftMouseAction(point, delta)

    def mouseUp(self, point):
        verbosePrint("Transmutor::mouseUp")
        if self.active:
            self.downPt = None
            self._leftMouseAction(point)

    def _leftMouseAction(self, point, delta=None):
        verbosePrint("Transmutor::_leftMouseAction")
        if self.active:
            x, y = point
            modifiers = self.getModifiers()
            scaledGlyph = self.model.getScaledGlyph()
            if scaledGlyph is not None:
                if self.downPt:
                    if self.optionDown:
                        self.clickAction = "interpolating"
                        if self.isDragging():
                            dampener = 200 * (1/CurrentGlyphWindow().getGlyphViewScale())
                            altDragDistanceH = min(1, max(-1, (y - self.downPt[1])/dampener))
                            altDragDistanceV = min(1, max(-1, (x - self.downPt[0])/dampener))

                            if not self.shiftDown:
                                self.model.stemWtRatioH = altDragDistanceV + 1
                                self.model.stemWtRatioV = altDragDistanceV + 1
                            else:

                                self.model.stemWtRatioH = altDragDistanceH + 1
                                self.model.stemWtRatioV = altDragDistanceV + 1
                    else:

                        if not self.isDragging():
                            hitSize = 20 * (1/CurrentGlyphWindow().getGlyphViewScale())
                            self.corner = None
                            self.clickX = 0
                            self.clickY = 0

                            # print the corner if the `point` is within 10 units of it
                            if scaledGlyph.bounds[0]-hitSize+self.model.offsetX < x < scaledGlyph.bounds[0]+hitSize+self.model.offsetX and scaledGlyph.bounds[1]-hitSize+self.model.offsetY < y < scaledGlyph.bounds[1]+hitSize+self.model.offsetY:
                                self.corner = (0.0, 0.0)
                                self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                                scaledGlyph.bounds[2],
                                                                1.0) - interpolate(scaledGlyph.bounds[0],
                                                                                    scaledGlyph.bounds[2],
                                                                                    self.model.transformOrigin[0])
                                self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                                scaledGlyph.bounds[3],
                                                                1.0) - interpolate(scaledGlyph.bounds[1],
                                                                                    scaledGlyph.bounds[3],
                                                                                    self.model.transformOrigin[1])
                                self.model.transformOrigin = (1.0, 1.0)
                                self.clickAction = "scaling"
                            elif scaledGlyph.bounds[2]-hitSize+self.model.offsetX < x < scaledGlyph.bounds[2]+hitSize+self.model.offsetX and scaledGlyph.bounds[3]-hitSize+self.model.offsetY < y < scaledGlyph.bounds[3]+hitSize+self.model.offsetY:
                                self.corner = (1.0, 1.0)
                                self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                                scaledGlyph.bounds[2],
                                                                0.0) - interpolate(scaledGlyph.bounds[0],
                                                                                    scaledGlyph.bounds[2],
                                                                                    self.model.transformOrigin[0])
                                self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                                scaledGlyph.bounds[3],
                                                                0.0) - interpolate(scaledGlyph.bounds[1],
                                                                                    scaledGlyph.bounds[3],
                                                                                    self.model.transformOrigin[1])
                                self.model.transformOrigin = (0.0, 0.0)
                                self.clickAction = "scaling"
                            elif scaledGlyph.bounds[0]-hitSize+self.model.offsetX < x < scaledGlyph.bounds[0]+hitSize+self.model.offsetX and scaledGlyph.bounds[3]-hitSize+self.model.offsetY < y < scaledGlyph.bounds[3]+hitSize+self.model.offsetY:
                                self.corner = (0.0, 1.0)
                                self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                                scaledGlyph.bounds[2],
                                                                1.0) - interpolate(scaledGlyph.bounds[0],
                                                                                    scaledGlyph.bounds[2],
                                                                                    self.model.transformOrigin[0])
                                self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                                scaledGlyph.bounds[3],
                                                                0.0) - interpolate(scaledGlyph.bounds[1],
                                                                                    scaledGlyph.bounds[3],
                                                                                    self.model.transformOrigin[1])
                                self.model.transformOrigin = (1.0, 0.0)
                                self.clickAction = "scaling"
                            elif scaledGlyph.bounds[2]-hitSize+self.model.offsetX < x < scaledGlyph.bounds[2]+hitSize+self.model.offsetX and scaledGlyph.bounds[1]-hitSize+self.model.offsetY < y < scaledGlyph.bounds[1]+hitSize+self.model.offsetY:
                                self.corner = (1.0, 0.0)
                                self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                                scaledGlyph.bounds[2],
                                                                0.0) - interpolate(scaledGlyph.bounds[0],
                                                                                    scaledGlyph.bounds[2],
                                                                                    self.model.transformOrigin[0])
                                self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                                scaledGlyph.bounds[3],
                                                                1.0) - interpolate(scaledGlyph.bounds[1],
                                                                                    scaledGlyph.bounds[3],
                                                                                    self.model.transformOrigin[1])
                                self.model.transformOrigin = (0.0, 1.0)
                                self.clickAction = "scaling"
                            elif scaledGlyph.bounds[0]+self.model.offsetX < x < scaledGlyph.bounds[2]+self.model.offsetX and scaledGlyph.bounds[1]+self.model.offsetY < y < scaledGlyph.bounds[3]+self.model.offsetY:
                                self.clickAction = "moving"
                                if self.downPt:
                                    self.clickX = self.downPt[0] - self.model.offsetX
                                    self.clickY = self.downPt[1] - self.model.offsetY
                            else:
                                self.clickAction = None
                        else:

                            if self.clickAction == "scaling" and self.corner is not None:
                                originPt = (
                                    interpolate(scaledGlyph.bounds[0] + self.model.offsetX, scaledGlyph.bounds[2] + self.model.offsetX, self.model.transformOrigin[0]),
                                    interpolate(scaledGlyph.bounds[1] + self.model.offsetY, scaledGlyph.bounds[3] + self.model.offsetY, self.model.transformOrigin[1]))

                                unScaledPt = (interpolate(self.model.unScaledGlyphBounds[0], self.model.unScaledGlyphBounds[2], self.corner[0]) + self.model.offsetX,
                                            interpolate(self.model.unScaledGlyphBounds[1], self.model.unScaledGlyphBounds[3], self.corner[1]) + self.model.offsetY)

                                # get distance from originPt to unScaledPt, in H and V directions
                                totalDistanceH = (unScaledPt[0] - originPt[0])
                                totalDistanceV = (unScaledPt[1] - originPt[1])

                                # get distance from originPt to x, y, in H and V directions
                                currentDistanceH = (x - originPt[0])
                                currentDistanceV = (y - originPt[1])

                                scaleV = currentDistanceV/totalDistanceV
                                if not self.shiftDown:
                                    if scaleV:
                                        scaleH = (currentDistanceH/totalDistanceH)/scaleV
                                    else:
                                        scaleH = (currentDistanceH/totalDistanceH)
                                else:
                                    scaleH = 1.0

                                self.model.scaleV = scaleV
                                self.model.scaleH = scaleH

                            elif self.clickAction == "moving":
                                self.model.offsetX = x - self.clickX
                                self.model.offsetY = y - self.clickY
                else:
                    self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                    scaledGlyph.bounds[2],
                                                    0.5) - interpolate(scaledGlyph.bounds[0],
                                                                        scaledGlyph.bounds[2],
                                                                        self.model.transformOrigin[0])
                    self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                    scaledGlyph.bounds[3],
                                                    0.5) - interpolate(scaledGlyph.bounds[1],
                                                                        scaledGlyph.bounds[3],
                                                                        self.model.transformOrigin[1])
                    self.model.transformOrigin = (0.5, 0.5)

            self.redrawView()

    def reset(self):
        verbosePrint("Transmutor::reset")
        self.model.glyph = self.getGlyph()
        self.model.currentFont = self.getGlyph().font
        self.redrawView()

    def addToGlyph(self):
        verbosePrint("Transmutor::addToGlyphCallback")
        scaledGlyph = self.model.getScaledGlyph()
        scaledGlyph.moveBy((self.model.offsetX, self.model.offsetY))
        with self.getGlyph().undo("Transmutor"):
            self.getGlyph().appendGlyph(scaledGlyph)

    def redrawView(self):
        self.foregroundContainer.clearSublayers()
        self.previewContainer.clearSublayers()

        if self.model.sourceGlyphName:
            scaledGlyph = self.model.getScaledGlyph()

            scaledGlyphLayer = self.foregroundContainer.appendPathSublayer(
                fillColor=self.model.scaledGlyphColor,
                strokeColor=None,
                opacity=1
            )
            pen = scaledGlyphLayer.getPen()
            scaledGlyph.draw(pen)

            previewLayer = self.previewContainer.appendPathSublayer(
                fillColor=self.model.previewColor,
                strokeColor=None,
                opacity=1,
            )
            pen = previewLayer.getPen()
            scaledGlyph.draw(pen)

            boxLayer = self.foregroundContainer.appendPathSublayer(
                fillColor=None,
                strokeColor=(1, 0, 0, 1),
                strokeWidth=1,
                name="box"
            )
            pen = boxLayer.getPen()
            # bounds (x, y, w, h)
            pen.moveTo((self.model.scaledGlyphBounds[0], self.model.scaledGlyphBounds[1]))
            pen.lineTo((self.model.scaledGlyphBounds[2], self.model.scaledGlyphBounds[1]))
            pen.lineTo((self.model.scaledGlyphBounds[2], self.model.scaledGlyphBounds[3]))
            pen.lineTo((self.model.scaledGlyphBounds[0], self.model.scaledGlyphBounds[3]))
            pen.closePath()
            boxLayer.setStrokeDash((5, 5))

            handleSize = 10

            swHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                position=(self.model.scaledGlyphBounds[0], self.model.scaledGlyphBounds[1]),
                imageSettings=dict(
                    name="rectangle",
                    size=(handleSize, handleSize),
                    fillColor=(1, 0, 0, 1)
                )
            )

            seHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                position=(self.model.scaledGlyphBounds[2], self.model.scaledGlyphBounds[1]),
                imageSettings=dict(
                    name="rectangle",
                    size=(handleSize, handleSize),
                    fillColor=(1, 0, 0, 1)
                )
            )

            neHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                position=(self.model.scaledGlyphBounds[2], self.model.scaledGlyphBounds[3]),
                imageSettings=dict(
                    name="rectangle",
                    size=(handleSize, handleSize),
                    fillColor=(1, 0, 0, 1)
                )
            )

            nwHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                position=(self.model.scaledGlyphBounds[0], self.model.scaledGlyphBounds[3]),
                imageSettings=dict(
                    name="rectangle",
                    size=(handleSize, handleSize),
                    fillColor=(1, 0, 0, 1)
                )
            )

            scaledGlyphLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
            previewLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
            boxLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
            swHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
            seHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
            neHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
            nwHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))

        self.toolPanel.refreshFromModel()


def main():
    tool = TransmutorToolController()
    installTool(tool)
    return tool


if __name__ == "__main__":
    main()