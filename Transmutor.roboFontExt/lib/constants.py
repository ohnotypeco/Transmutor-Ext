"""
Transmutor v3 — Shared constants.

Defines extension identifiers, lib keys, color defaults, scale bounds,
and other constants used across all Transmutor modules.
"""

from typing import Tuple

# Extension metadata
EXTENSION_IDENTIFIER: str = "co.ohnotype.Transmutor"
VERSION: str = "3.0.0"

# Glyph lib keys for Transmutor block storage
LIB_KEY_BLOCKS: str = "co.ohnotype.transmutor.blocks"
LIB_KEY_BLOCK_ID: str = "co.ohnotype.transmutor.blockId"

# Scale bounds — clamped to prevent degenerate geometry / kernel panics
MIN_SCALE: float = 0.01
MAX_SCALE: float = 10.0
DEFAULT_SCALE: float = 1.0

# Stem weight ratio bounds
MIN_STEM_RATIO: float = 0.0
MAX_STEM_RATIO: float = 4.0
DEFAULT_STEM_RATIO: float = 1.0

# Slider bounds for the UI
SLIDER_MIN_SCALE: float = 0.0
SLIDER_MAX_SCALE: float = 4.0
SLIDER_MIN_STEM: float = 0.0
SLIDER_MAX_STEM: float = 4.0

# Handle size for bounding box handles (in points, not glyph units)
HANDLE_SIZE: int = 10

# Hit-test radius multiplier (scaled by inverse of glyph view scale)
HIT_TEST_RADIUS: int = 10

# Maximum coordinate value allowed in output glyphs
MAX_COORDINATE: float = 100000.0

# Bounding box dash pattern
BOX_DASH_PATTERN: Tuple[int, int] = (5, 5)

# Reference glyph names for stem measurement
VSTEM_REFERENCE_GLYPH: str = "I"
HSTEM_REFERENCE_GLYPH: str = "H"

# Verbose logging toggle
VERBOSE: bool = True


def verbosePrint(s: str) -> None:
    """Print a message only when verbose mode is enabled."""
    if VERBOSE:
        print(f"[Transmutor] {s}")
