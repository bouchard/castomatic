"""Castomatic: cast anything, anywhere.

Plays a video file, URL or web page on any TV over Chromecast, DLNA, Roku or AirPlay 2,
sending it untouched when the TV plays it and converting just in time when it does not.
"""

__version__ = "0.2.0"

from castomatic.dlna import DirectFileServer, DlnaError, DlnaPlayer, find_renderer  # noqa: E402
from castomatic.hls import (  # noqa: E402
    JitHlsServer,
    MediaPlan,
    StaticHlsServer,
    convert_to_directory,
    plan,
)
from castomatic.airplay import AirPlayError, AirPlayPlayer  # noqa: E402
from castomatic.backends.base import CastError, Capabilities  # noqa: E402
from castomatic.backends.chromecast import ChromecastPlayer  # noqa: E402
from castomatic.backends.roku import RokuPlayer  # noqa: E402

__all__ = [
    "AirPlayError",
    "AirPlayPlayer",
    "CastError",
    "Capabilities",
    "ChromecastPlayer",
    "DirectFileServer",
    "DlnaError",
    "DlnaPlayer",
    "JitHlsServer",
    "MediaPlan",
    "RokuPlayer",
    "StaticHlsServer",
    "convert_to_directory",
    "find_renderer",
    "plan",
]
