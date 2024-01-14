############################################################
#                                                          #
#            OOOOO  HH   HH    NN   NN  OOOOO              #
#           OO   OO HH   HH    NNN  NN OO   OO             #
#           OO   OO HHHHHHH    NN N NN OO   OO             #
#           OO   OO HH   HH    NN  NNN OO   OO             #
#            OOOO0  HH   HH    NN   NN  OOOO0              #
#                                                          #
#    TTTTTTT YY   YY PPPPPP  EEEEEEE     CCCCC   OOOOO     #
#      TTT   YY   YY PP   PP EE         CC    C OO   OO    #
#      TTT    YYYYY  PPPPPP  EEEEE      CC      OO   OO    #
#      TTT     YYY   PP      EE         CC    C OO   OO    #
#      TTT     YYY   PP      EEEEEEE     CCCCC   OOOO0     #
#                                                          #
############################################################
#                                                          #
#         By Colin M. Ford and OH no Type Co, 2023         #
#     See license file for use and distribution terms.     #
#                                                          #
#  Huge thanks to Loïc Sander for MutatorScale/ScaleFast   #
#                                                          #
############################################################


import functools
import math
import os
import site
from copy import deepcopy

import ezui
from mojo.events import EditingTool, getActiveEventTool, postEvent
from mojo.roboFont import version
from mojo.subscriber import Subscriber, registerRoboFontSubscriber, registerSubscriberEvent, getRegisteredSubscriberEvents
from mojo.tools import IntersectGlyphWithLine as intersect
from mojo.UI import CurrentGlyphWindow, getDefault

try:
    import mutatorScale
except ImportError:
    mutatorScaleLibFolder = os.path.join(os.getcwd(), "..", "submodules", "MutatorScale", "lib")
    site.addsitedir(mutatorScaleLibFolder)
finally:
    from mutatorScale.objects.scaler import MutatorScaleEngine
    from mutatorScale.utilities.fontUtils import getRefStems, makeListFontName


VERBOSE = False
EXTENSION_IDENTIFIER = "co.ohnotype.Transmutor"
VERSION = "2.0.3"

def verbosePrint(s):
    if VERBOSE:
        print(s)


def disable(klass):
    return False


enable = deepcopy(getActiveEventTool().__class__.canSelectWithMarque)

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


def distance(p1, p2):
    return round(math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2), 2)


def interpolate(a, b, v):
    return a + (b - a) * v


def norm(v, a, b):
    return (v - a) / (b - a)


@cache
def getRefStemsCached(font):
    return getRefStems(font)

@cache
def makeListFontNameCached(font):
    return makeListFontName(font)

class TransmutorModel():
    currentGlyph = None
    currentFont = None
    allFonts = []
    activeFonts = []
    firstActiveFont = None
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

    @property
    def scaledGlyphColor(self):
        verbosePrint("TransmutorModel::scaledGlyphColor")
        return getDefault("glyphViewTransformationColor")

    @property
    def previewColor(self):
        verbosePrint("TransmutorModel::previewColor")
        return getDefault("glyphViewPreviewFillColor")

    @property
    def textColor(self):
        verbosePrint("TransmutorModel::textColor")
        return getDefault("glyphViewMeasurementsTextColor")

    @property
    def use45Constraint(self):
        verbosePrint("TransmutorModel::use45Constraint")
        return getDefault("glyphViewShouldUse45Contrain")

    def updateScaler(self):
        if self.allFonts:
            self.scaler = MutatorScaleEngine(self.activeFonts)
            self.firstActiveFont = self.activeFonts[0]

    def getScaledGlyph(self):
        verbosePrint("TransmutorModel::getScaledGlyph")
        if self.currentFont is not None and self.currentGlyph is not None and self.activeFonts:
            self.firstActiveFont = self.activeFonts[0]
            if self.sourceGlyphName != self.currentGlyph.name and self.sourceGlyphName in self.firstActiveFont.keys():
                firstActiveFontName = makeListFontNameCached(self.firstActiveFont)
                currentFontName = makeListFontNameCached(self.currentFont)
                if firstActiveFontName in self.scaler.masters:
                    stems = (self.scaler.masters[firstActiveFontName].vstem * self.stemWtRatioV,
                             self.scaler.masters[firstActiveFontName].hstem * self.stemWtRatioH)
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
                unScaledGlyph.moveBy((-originPt[0], -originPt[1]))
                self.unScaledGlyphBounds = unScaledGlyph.bounds

                self.scaler.set({
                    "width": self.scaleH/self.scaleV,
                    "targetHeight": self.currentFont.info.unitsPerEm * self.scaleV,
                    "referenceHeight": self.currentFont.info.unitsPerEm,
                })

                newGlyph = self.scaler.getScaledGlyph(self.sourceGlyphName, stems)
                originPt = (interpolate(newGlyph.bounds[0], newGlyph.bounds[2], self.transformOrigin[0]),
                            interpolate(newGlyph.bounds[1], newGlyph.bounds[3], self.transformOrigin[1]))
                newGlyph.moveBy((-originPt[0], -originPt[1]))
                self.scaledGlyphBounds = newGlyph.bounds

                return newGlyph

        return None


class TransmutorToolController(Subscriber, ezui.WindowController):
    active = False
    downPt = None
    clickAction = None
    isDragging = False
    optionDown = False
    shiftDown = False

    # Build, Destroy, Etc.
    #############################################################

    def build(self):
        verbosePrint("TransmutorToolController::build")

        content = """
        = TwoColumnForm
        
        : Glyph:
        * HorizontalStack       
        > [__]                      @glyphNamesTextBox
        > ({xmark})                 @clearNameButton
        
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
        [X] Constrain               @constrainScaleSwitch
        
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
                width=330,
            ),
            clearNameButton=dict(
                symbolConfiguration={
                    'scale'        : 'medium', 
                    'weight'       : 'regular', 
                    },
                drawBorder=True,
                width=20
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
                maxValue=4.0,
                tickMarks=5,
            ),
            stemWtRatioVSlider=dict(
                valueType="float",
                value=1.0,
                minValue=0.0,
                maxValue=4.0,
                tickMarks=5,
            ),
            scaleHSlider=dict(
                valueType="float",
                value=1.0,
                minValue=0.001,
                maxValue=4.0,
                tickMarks=5,
            ),
            scaleVSlider=dict(
                valueType="float",
                value=1.0,
                minValue=0.001,
                maxValue=4.0,
                tickMarks=5,
            ),
        )

        self.w = ezui.EZPanel(
            content=content,
            descriptionData=descriptionData,
            controller=self,
            title=f"Transmutor v{VERSION}",
            autosaveName="TransmutorPanel",
            activeItem="glyphNamesTextBox",
            size=(300, "auto"),
        )

        self.w.getNSWindow().setTitlebarHeight_(22)
        self.w.getNSWindow().setTitlebarAppearsTransparent_(True)

        self.stemWtRatioVSlider = self.w.getItem("stemWtRatioVSlider")
        self.stemWtRatioHSlider = self.w.getItem("stemWtRatioHSlider")
        self.constrainStemWtRatioSwitch = self.w.getItem("constrainStemWtRatioSwitch")

        self.stemWtRatioHSlider.enable(False)
        
        self.scaleVSlider = self.w.getItem("scaleVSlider")
        self.scaleHSlider = self.w.getItem("scaleHSlider")
        self.constrainScaleSwitch = self.w.getItem("constrainScaleSwitch")

        self.scaleHSlider.enable(False)

        # self.w.setDefaultButton(self.w.getItem("addToGlyphButton"))

    def started(self):
        verbosePrint("TransmutorToolController::started")
        if not self.active:
            if CurrentGlyphWindow():
                self.foregroundContainer = CurrentGlyphWindow().extensionContainer(
                    identifier=EXTENSION_IDENTIFIER + ".foreground",
                    location="foreground",
                    clear=True
                )
                self.previewContainer = CurrentGlyphWindow().extensionContainer(
                    identifier=EXTENSION_IDENTIFIER + ".preview",
                    location="preview",
                    clear=True
                )
                self.foregroundContainer.setVisible(True)
                self.previewContainer.setVisible(True)

                self.model = TransmutorModel()
                
                # self.model.setInitialActive()

                self.active = True
                self.userHasMovedGlyph = False

                self.reset(resetActiveFonts=True)

                self.w.open()
                self.w.getNSWindow().makeKeyWindow()

    def destroy(self):
        verbosePrint("TransmutorToolController::destroy")
        if self.active:
            if self.foregroundContainer is None:
                return
            self.foregroundContainer.setVisible(False)
            self.previewContainer.setVisible(False)

            self.foregroundContainer.clearSublayers()
            self.previewContainer.clearSublayers()

            # if self.w:
            #     self.w.close()

            del self.model
            self.model = None

            self.active = False
            postEvent(f"{EXTENSION_IDENTIFIER}.transmutorDidStopDrawing")

    # State Management Functions
    #############################################################

    def reset(self, resetActiveFonts=False):
        verbosePrint("TransmutorToolController::reset")
        self.model.currentFont = CurrentFont()
        self.model.currentGlyph = CurrentGlyph()
        self.model.allFonts = [font for font in AllFonts(sortOptions=["magic"])]
        if resetActiveFonts:
            self.model.activeFonts = [font for font in self.model.allFonts if font.info.familyName]

        self.model.updateScaler()
        self.redrawView()

    def clearSelection(self):
        self.model.currentGlyph.selectedContours = ()
        self.model.currentGlyph.selectedComponents = ()
        self.model.currentGlyph.selectedAnchors = ()
        # self.model.currentGlyph.changed()

    def addToGlyph(self):
        verbosePrint("TransmutorToolController::addToGlyph")
        scaledGlyph = self.model.getScaledGlyph()
        scaledGlyph.moveBy((self.model.offsetX, self.model.offsetY))
        scaledGlyph.round()
        with self.model.currentGlyph.undo("Transmutor"):
            self.model.currentGlyph.appendGlyph(scaledGlyph)

    def refreshFromModel(self):
        verbosePrint("TransmutorToolController::refreshFromModel")
        self.w.getItem("stemWtRatioVSlider").set(self.model.stemWtRatioV)
        self.w.getItem("stemWtRatioHSlider").set(self.model.stemWtRatioH)

        self.w.getItem("scaleVSlider").set(self.model.scaleV)
        self.w.getItem("scaleHSlider").set(self.model.scaleH)

        if len(self.w.getItemValue("sourceFontTable")) > 0:
            self.w.getItem("sourceFontTable").set([])
            
        for font in self.model.allFonts:
            vStem, hStem = getRefStemsCached(font)
            self.w.getItem("sourceFontTable").appendItems([{
                "selected": (font in self.model.activeFonts),
                "font": makeListFontNameCached(font),
                "vStem": vStem,
                "hStem": hStem,
            }])

    def redrawView(self):
        verbosePrint("TransmutorToolController::redrawView")
        self.foregroundContainer.clearSublayers()
        self.previewContainer.clearSublayers()

        if self.model.sourceGlyphName:
            scaledGlyph = self.model.getScaledGlyph()

            if scaledGlyph is not None:

                scaledGlyphLayer = self.foregroundContainer.appendPathSublayer(
                    fillColor=self.model.scaledGlyphColor,
                    strokeColor=None,
                    opacity=0.5
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

                postEvent(f"{EXTENSION_IDENTIFIER}.transmutorDidDraw", transmutorGlyph=scaledGlyph, offset=(self.model.offsetX, self.model.offsetY), color=self.model.scaledGlyphColor)

                boxLayer = self.foregroundContainer.appendPathSublayer(
                    fillColor=None,
                    strokeColor=self.model.scaledGlyphColor,
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
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                sHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                    position=(interpolate(self.model.scaledGlyphBounds[0], self.model.scaledGlyphBounds[2], 0.5), self.model.scaledGlyphBounds[1]),
                    imageSettings=dict(
                        name="rectangle",
                        size=(handleSize, handleSize),
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                seHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                    position=(self.model.scaledGlyphBounds[2], self.model.scaledGlyphBounds[1]),
                    imageSettings=dict(
                        name="rectangle",
                        size=(handleSize, handleSize),
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                eHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                    position=(self.model.scaledGlyphBounds[2], interpolate(self.model.scaledGlyphBounds[1], self.model.scaledGlyphBounds[3], 0.5)),
                    imageSettings=dict(
                        name="rectangle",
                        size=(handleSize, handleSize),
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                neHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                    position=(self.model.scaledGlyphBounds[2], self.model.scaledGlyphBounds[3]),
                    imageSettings=dict(
                        name="rectangle",
                        size=(handleSize, handleSize),
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                nHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                    position=(interpolate(self.model.scaledGlyphBounds[0], self.model.scaledGlyphBounds[2], 0.5), self.model.scaledGlyphBounds[3]),
                    imageSettings=dict(
                        name="rectangle",
                        size=(handleSize, handleSize),
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                nwHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                    position=(self.model.scaledGlyphBounds[0], self.model.scaledGlyphBounds[3]),
                    imageSettings=dict(
                        name="rectangle",
                        size=(handleSize, handleSize),
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                wHandleLayer = self.foregroundContainer.appendSymbolSublayer(
                    position=(self.model.scaledGlyphBounds[0], interpolate(self.model.scaledGlyphBounds[1], self.model.scaledGlyphBounds[3], 0.5)),
                    imageSettings=dict(
                        name="rectangle",
                        size=(handleSize, handleSize),
                        fillColor=self.model.scaledGlyphColor
                    )
                )

                # show measurements from currentGlyph
                measurements = self.model.currentGlyph.naked().measurements

                for m in measurements:
                    if m.startPoint and m.endPoint:
                        sx, sy = m.startPoint
                        ex, ey = m.endPoint

                        sx -= self.model.offsetX
                        sy -= self.model.offsetY
                        ex -= self.model.offsetX
                        ey -= self.model.offsetY

                        l = (sx, sy), (ex, ey)
                        i = sorted(intersect(scaledGlyph, l))
                        inters = [i[ii:ii+2] for ii in range(0, len(i), 2-1)]
                        for coords in inters:
                            if len(coords) == 2:
                                front, back = coords
                                front = front[0] + self.model.offsetX, front[1] + self.model.offsetY
                                back = back[0] + self.model.offsetX, back[1] + self.model.offsetY

                                self.foregroundContainer.appendSymbolSublayer(
                                    position=front,
                                    imageSettings=dict(
                                        name="oval",
                                        size=(handleSize*0.5, handleSize*0.5),
                                        fillColor=self.model.scaledGlyphColor
                                    )
                                )
                                self.foregroundContainer.appendSymbolSublayer(
                                    position=back,
                                    imageSettings=dict(
                                        name="oval",
                                        size=(handleSize*0.5, handleSize*0.5),
                                        fillColor=self.model.scaledGlyphColor
                                    )
                                )
                                xM = interpolate(front[0], back[0], .5)
                                yM = interpolate(front[1], back[1], .5)
                                self.foregroundContainer.appendTextLineSublayer(
                                    position=(xM, yM),
                                    size=(20, 20),
                                    pointSize=8,
                                    weight="bold",
                                    text=f"{distance(front,back)}",
                                    fillColor=self.model.textColor,
                                    horizontalAlignment="center",
                                    verticalAlignment="center",
                                )

                scaledGlyphLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                previewLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                boxLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                swHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                sHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                seHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                eHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                neHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                nHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                nwHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))
                wHandleLayer.addTranslationTransformation((self.model.offsetX, self.model.offsetY))

        self.refreshFromModel()
        

    # Panel Callbacks
    #############################################################

    def glyphNamesTextBoxCallback(self, sender):
        verbosePrint("TransmutorToolController::glyphNamesTextBoxCallback")
        self.model.sourceGlyphName = sender.get()
        if not self.userHasMovedGlyph and self.model.sourceGlyphName in self.model.firstActiveFont.keys():
            self.model.offsetX = interpolate(
                self.model.firstActiveFont[self.model.sourceGlyphName].bounds[0],
                self.model.firstActiveFont[self.model.sourceGlyphName].bounds[2],
                self.model.transformOrigin[0])
            self.model.offsetY = interpolate(
                self.model.firstActiveFont[self.model.sourceGlyphName].bounds[1],
                self.model.firstActiveFont[self.model.sourceGlyphName].bounds[3],
                self.model.transformOrigin[1])
        if not self.model.sourceGlyphName:
            postEvent(f"{EXTENSION_IDENTIFIER}.transmutorDidStopDrawing")
        self.redrawView()
        
    def clearNameButtonCallback(self, sender):
        self.glyphNamesTextBox = self.w.getItem("glyphNamesTextBox")
        self.glyphNamesTextBox.set("")
        self.glyphNamesTextBoxCallback(self.glyphNamesTextBox)

    def sourceFontTableEditCallback(self, sender):
        verbosePrint("TransmutorToolController::sourceFontTableEditCallback")
        items = self.w.getItemValue("sourceFontTable")
        for i, item in enumerate(items):
            if item["selected"]:
                if self.model.allFonts[i] not in self.model.activeFonts:
                    self.model.activeFonts.append(self.model.allFonts[i])
            else:
                if self.model.allFonts[i] in self.model.activeFonts:
                    self.model.activeFonts.remove(self.model.allFonts[i])

        self.model.updateScaler()
        self.redrawView()

    def stemWtRatioVSliderCallback(self, sender):
        verbosePrint("TransmutorToolController::stemWtRatioVSliderCallback")
        if self.constrainStemWtRatioSwitch.get():
            self.model.stemWtRatioV = float(sender.get())
            self.model.stemWtRatioH = float(sender.get())
            self.stemWtRatioHSlider.set(float(sender.get()))
        else:
            self.model.stemWtRatioV = float(sender.get())

        self.redrawView()

    def stemWtRatioVSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorToolController::stemWtRatioVSliderTextFieldCallback")
        if self.constrainStemWtRatioSwitch.get():
            self.model.stemWtRatioV = float(sender.get())
            self.model.stemWtRatioH = float(sender.get())
            self.stemWtRatioHSlider.set(float(sender.get()))
        else:
            self.model.stemWtRatioV = float(sender.get())

        self.redrawView()

    def stemWtRatioHSliderCallback(self, sender):
        verbosePrint("TransmutorToolController::stemWtRatioHSliderCallback")
        self.model.stemWtRatioH = float(sender.get())
        self.redrawView()

    def stemWtRatioHSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorToolController::stemWtRatioHSliderTextFieldCallback")
        self.model.stemWtRatioH = float(sender.get())
        self.redrawView()

    def constrainStemWtRatioSwitchCallback(self, sender):
        verbosePrint("TransmutorToolController::constrainStemWtRatioSwitchCallback")
        if self.constrainStemWtRatioSwitch.get():
            self.stemWtRatioHSlider.enable(False)
            self.stemWtRatioHSlider.set(float(self.stemWtRatioVSlider.get()))
        else:
            self.stemWtRatioHSlider.enable(True)

        self.redrawView()

    def scaleVSliderCallback(self, sender):
        verbosePrint("TransmutorToolController::scaleVSliderCallback")
        self.model.scaleV = float(sender.get())
        if self.constrainScaleSwitch.get():
            self.model.scaleH = float(sender.get()) if float(sender.get()) > 0 else 0.0001
            self.scaleHSlider.set(float(sender.get()))

        self.redrawView()

    def scaleVSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorToolController::scaleVSliderTextFieldCallback")
        self.model.scaleV = float(sender.get())
        if self.constrainScaleSwitch.get():
            self.model.scaleH = float(sender.get()) if float(sender.get()) > 0 else 0.0001
            self.scaleHSlider.set(float(sender.get()))

        self.redrawView()

    def scaleHSliderCallback(self, sender):
        verbosePrint("TransmutorToolController::scaleHSliderCallback")
        scaleH = float(sender.get()) if float(sender.get()) > 0 else 0.0001
        self.model.scaleH = scaleH
        self.redrawView()

    def scaleHSliderTextFieldCallback(self, sender):
        verbosePrint("TransmutorToolController::scaleHSliderTextFieldCallback")
        scaleH = float(sender.get()) if float(sender.get()) > 0 else 0.0001
        self.model.scaleH = scaleH
        self.redrawView()
        
    def constrainScaleSwitchCallback(self, sender):
        verbosePrint("TransmutorToolController::constrainScaleSwitchCallback")
        if self.constrainScaleSwitch.get():
            self.scaleHSlider.enable(False)
            self.scaleHSlider.set(float(self.scaleVSlider.get()))
        else:
            self.scaleHSlider.enable(True)

        self.redrawView()

    def addToGlyphButtonCallback(self, sender):
        verbosePrint("TransmutorToolController::addToGlyphButtonCallback")
        self.addToGlyph()

    # Subscriber Events
    #############################################################

    def fontDocumentDidBecomeCurrent(self, info):
        verbosePrint("TransmutorToolController::fontDocumentDidBecomeCurrent")
        if self.active == True:
            self.reset()

    def fontDocumentDidOpen(self, info):
        verbosePrint("TransmutorToolController::fontDocumentDidOpen")
        if self.active == True:
            self.reset()

    def roboFontDidSwitchCurrentGlyph(self, info):
        verbosePrint("TransmutorToolController::roboFontDidSwitchCurrentGlyph")
        if self.active == True:
            self.reset()

    def glyphEditorDidOpen(self, info):
        verbosePrint("TransmutorToolController::glyphEditorDidOpen")
        if self.active == True:
            self.reset()

    def glyphEditorDidSetGlyph(self, info):
        verbosePrint("TransmutorToolController::glyphEditorDidSetGlyph")
        if self.active == True:
            self.reset()
            
    def glyphEditorDidKeyDown(self, info):
        verbosePrint("TransmutorToolController::glyphEditorDidKeyDown")
        if self.active == True:
            if info["NSEvent"].keyCode() == 53:
                self.w.close()

    # Mouse Events
    #############################################################

    def glyphEditorDidMouseDown(self, info):
        verbosePrint("TransmutorToolController::glyphEditorDidMouseDown")
        if self.active:
            global enable
            enable = deepcopy(getActiveEventTool().__class__.canSelectWithMarque)
            point = info['lowLevelEvents'][0]['point']
            self.downPt = point
            self.isDragging = False
            self._leftMouseAction(point)

    # glyphEditorDidMouseDragDelay = 0
    def glyphEditorDidMouseDrag(self, info):
        verbosePrint("TransmutorToolController::glyphEditorDidMouseDrag")
        if self.active:
            point = info['lowLevelEvents'][0]['point']
            self.isDragging = True
            self._leftMouseAction(point)

    def glyphEditorDidMouseUp(self, info):
        verbosePrint("TransmutorToolController::glyphEditorDidMouseUp")
        if self.active:
            global enable
            point = info['lowLevelEvents'][0]['point']
            self.downPt = None
            self.isDragging = False
            self._leftMouseAction(point)
            getActiveEventTool().__class__.canSelectWithMarque = enable

    def glyphEditorDidChangeModifiers(self, info):
        verbosePrint("TransmutorToolController::glyphEditorDidChangeModifiers")
        self.optionDown = bool(info["deviceState"]["optionDown"])
        self.shiftDown = bool(info["deviceState"]["shiftDown"])
        
    def _ptOnHandle(self, point, handle):
        hitSize = 10 * (1/CurrentGlyphWindow().getGlyphViewScale())
        return handle[0]-hitSize+self.model.offsetX < point[0] < handle[0]+hitSize+self.model.offsetX and handle[1]-hitSize+self.model.offsetY < point[1] < handle[1]+hitSize+self.model.offsetY

    def _leftMouseAction(self, point, delta=None):
        verbosePrint("TransmutorToolController::_leftMouseAction")
        if self.active:
            global enable, disable

            x, y = point
            scaledGlyph = self.model.getScaledGlyph()

            if scaledGlyph is not None and issubclass(getActiveEventTool().__class__, EditingTool):
                # If the scaled glyph is a live glyph and not none
                # and the active tool is the EditingTool or a subclass of it
                if self.downPt:
                    # if there is a downPt, i.e. it is not a mouseUp action
                    if not self.isDragging:
                        # if the mouse is not dragging, i.e. it is a mouseDown action, figure out where the click happened
                        self.corner = None
                        self.clickX = 0
                        self.clickY = 0

                        if self._ptOnHandle(point, (scaledGlyph.bounds[0], scaledGlyph.bounds[1])):
                            # SW Corner
                            getActiveEventTool().__class__.canSelectWithMarque = disable
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
                        elif self._ptOnHandle(point, (interpolate(scaledGlyph.bounds[0], scaledGlyph.bounds[2], 0.5), scaledGlyph.bounds[1])):
                            # S center
                            getActiveEventTool().__class__.canSelectWithMarque = disable
                            self.corner = (0.5, 0.0)
                            self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                              scaledGlyph.bounds[2],
                                                              0.5) - interpolate(scaledGlyph.bounds[0],
                                                                                 scaledGlyph.bounds[2],
                                                                                 self.model.transformOrigin[0])
                            self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                              scaledGlyph.bounds[3],
                                                              1.0) - interpolate(scaledGlyph.bounds[1],
                                                                                 scaledGlyph.bounds[3],
                                                                                 self.model.transformOrigin[1])
                            self.model.transformOrigin = (0.5, 1.0)
                            self.clickAction = "scaling"
                        elif self._ptOnHandle(point, (scaledGlyph.bounds[2], scaledGlyph.bounds[1])):
                            # SE Corner
                            getActiveEventTool().__class__.canSelectWithMarque = disable
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
                        elif self._ptOnHandle(point, (scaledGlyph.bounds[2], interpolate(scaledGlyph.bounds[1], scaledGlyph.bounds[3], 0.5))):
                            # E center
                            getActiveEventTool().__class__.canSelectWithMarque = disable
                            self.corner = (1.0, 0.5)
                            self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                              scaledGlyph.bounds[2],
                                                              0.0) - interpolate(scaledGlyph.bounds[0],
                                                                                 scaledGlyph.bounds[2],
                                                                                 self.model.transformOrigin[0])
                            self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                              scaledGlyph.bounds[3],
                                                              0.5) - interpolate(scaledGlyph.bounds[1],
                                                                                 scaledGlyph.bounds[3],
                                                                                 self.model.transformOrigin[1])
                            self.model.transformOrigin = (0.0, 0.5)
                            self.clickAction = "scaling"
                        elif self._ptOnHandle(point, (scaledGlyph.bounds[2], scaledGlyph.bounds[3])):
                            # NE Corner
                            getActiveEventTool().__class__.canSelectWithMarque = disable
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
                        elif self._ptOnHandle(point, (interpolate(scaledGlyph.bounds[0], scaledGlyph.bounds[2], 0.5), scaledGlyph.bounds[3])):
                            # N center
                            getActiveEventTool().__class__.canSelectWithMarque = disable
                            self.corner = (0.5, 1.0)
                            self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                              scaledGlyph.bounds[2],
                                                              0.5) - interpolate(scaledGlyph.bounds[0],
                                                                                 scaledGlyph.bounds[2],
                                                                                 self.model.transformOrigin[0])
                            self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                              scaledGlyph.bounds[3],
                                                              0.0) - interpolate(scaledGlyph.bounds[1],
                                                                                 scaledGlyph.bounds[3],
                                                                                 self.model.transformOrigin[1])
                            self.model.transformOrigin = (0.5, 0.0)
                            self.clickAction = "scaling"
                        elif self._ptOnHandle(point, (scaledGlyph.bounds[0], scaledGlyph.bounds[3])):
                            # NW Corner
                            getActiveEventTool().__class__.canSelectWithMarque = disable
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
                        elif self._ptOnHandle(point, (scaledGlyph.bounds[0], interpolate(scaledGlyph.bounds[1], scaledGlyph.bounds[3], 0.5))):
                            # W center
                            getActiveEventTool().__class__.canSelectWithMarque = disable
                            self.corner = (0.0, 0.5)
                            self.model.offsetX += interpolate(scaledGlyph.bounds[0],
                                                              scaledGlyph.bounds[2],
                                                              1.0) - interpolate(scaledGlyph.bounds[0],
                                                                                 scaledGlyph.bounds[2],
                                                                                 self.model.transformOrigin[0])
                            self.model.offsetY += interpolate(scaledGlyph.bounds[1],
                                                              scaledGlyph.bounds[3],
                                                              0.5) - interpolate(scaledGlyph.bounds[1],
                                                                                 scaledGlyph.bounds[3],
                                                                                 self.model.transformOrigin[1])
                            self.model.transformOrigin = (1.0, 0.5)
                            self.clickAction = "scaling"
                        elif scaledGlyph.bounds[0]+self.model.offsetX < x < scaledGlyph.bounds[2]+self.model.offsetX and scaledGlyph.bounds[1]+self.model.offsetY < y < scaledGlyph.bounds[3]+self.model.offsetY:
                            # Inside the box
                            getActiveEventTool().__class__.canSelectWithMarque = disable
                            if self.optionDown:
                                self.clickAction = "interpolating"
                            else:
                                self.clickAction = "moving"
                                self.userHasMovedGlyph = True

                                self.clickX = self.downPt[0] - self.model.offsetX
                                self.clickY = self.downPt[1] - self.model.offsetY

                                self.storedOffsetX = self.model.offsetX
                                self.storedOffsetY = self.model.offsetY
                        else:
                            # outside the box, not on one of the handles
                            self.clickAction = None
                            getActiveEventTool().__class__.canSelectWithMarque = enable
                    else:
                        # if the mouse IS dragging
                        if self.clickAction:
                            self.clearSelection()
                            # If there's a click action
                            if self.clickAction == "scaling" and self.corner is not None:
                                # If the user is scaling
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

                                if not totalDistanceV:
                                    scaleV = self.model.scaleV
                                else:
                                    scaleV = currentDistanceV/totalDistanceV
                                    
                                if not totalDistanceH:
                                    scaleH = self.model.scaleH
                                else:
                                    if not self.shiftDown:
                                        scaleH = currentDistanceH/totalDistanceH
                                    else:
                                        scaleH = scaleV

                                self.model.scaleV = scaleV
                                self.model.scaleH = scaleH

                            elif self.clickAction == "moving":
                                
                                # If the user is moving, change the offset to match the current mouse position relative to the position of the click within the bounds of the glyph
                                if not self.shiftDown:
                                    self.model.offsetX = x - self.clickX
                                    self.model.offsetY = y - self.clickY
                                # If shift is down, keep the X or Y the same, depending on which mouse delta is higher.
                                else:
                                    deltaX = (x - self.downPt[0])
                                    deltaY = (y - self.downPt[1])
                                    if abs(deltaX) >= abs(deltaY):
                                        self.model.offsetX = x - self.clickX
                                        self.model.offsetY = self.storedOffsetY
                                    else:
                                        self.model.offsetX = self.storedOffsetX
                                        self.model.offsetY = y - self.clickY
                                
                            elif self.clickAction == "interpolating":
                                # The user held down option, and is interpolating
                                dampener = 200 * (1/CurrentGlyphWindow().getGlyphViewScale())
                                altDragDistanceH = min(3, max(-1, (y - self.downPt[1])/dampener))
                                altDragDistanceV = min(3, max(-1, (x - self.downPt[0])/dampener))

                                if not self.shiftDown:
                                    self.model.stemWtRatioH = altDragDistanceV + 1
                                    self.model.stemWtRatioV = altDragDistanceV + 1
                                else:
                                    self.model.stemWtRatioH = altDragDistanceH + 1
                                    self.model.stemWtRatioV = altDragDistanceV + 1
                else:
                    # Reset the origin point and offset to defaults
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


def main():
    # Register a subscriber event for Transmutor updating a drawing
    event_name = f"{EXTENSION_IDENTIFIER}.transmutorDidDraw"
    if event_name not in getRegisteredSubscriberEvents():
        registerSubscriberEvent(
            subscriberEventName=event_name,
            methodName="transmutorDidDraw",
            lowLevelEventNames=[event_name],
            dispatcher="roboFont",
            documentation="Sent when Transmutor has updated the current glyph drawing.",
            delay=None
        )
    # Register a subscriber event for transmutor stopping drawing
    event_name = f"{EXTENSION_IDENTIFIER}.transmutorDidStopDrawing"
    if event_name not in getRegisteredSubscriberEvents():
        registerSubscriberEvent(
            subscriberEventName=event_name,
            methodName="transmutorDidStopDrawing",
            lowLevelEventNames=[event_name],
            dispatcher="roboFont",
            documentation="Sent when Transmutor has stopped drawing.",
            delay=None
        )
    registerRoboFontSubscriber(TransmutorToolController)


if __name__ == "__main__":
    main()
