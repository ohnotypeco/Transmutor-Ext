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
#     By Colin M. Ford and OH no Type Co, 2023-2025        #
#     See license file for use and distribution terms.     #
#                                                          #
#  Transmutor v3 — Live contour scaling tool.              #
#  Rewritten from scratch, replacing MutatorScale with     #
#  direct MutatorMath interpolation.                       #
#                                                          #
############################################################

"""
Transmutor v3 — Entry point.

This script is run at RoboFont startup (via launchAtStartUp in info.plist).
It installs the TransmutorTool as a toolbar icon in the glyph editor.
"""

from mojo.events import installTool

from transmutorTool import TransmutorTool

installTool(TransmutorTool())
