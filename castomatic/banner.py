"""The starburst: Castomatic's mark in the terminal, and the first-run welcome."""

import os
import sys
from typing import Optional, TextIO

from castomatic import __version__

STARBURST = r"""
      \   |   /
    .  \  |  /  .
   --- -- * -- ---    castomatic {version}
    '  /  |  \  '     cast anything, anywhere
      /   |   \
"""

WELCOME = """\
Welcome! Castomatic plays a video file, a video URL or a web page on your TV.

  1. It looks for TVs on your network: Chromecast, DLNA, Roku and AirPlay all at once.
  2. If there is more than one it asks which, once, and remembers your answer.
  3. It sends the video the most reliable way the TV offers, untouched if the TV plays it,
     converted just in time if not, and buffered first if your network or drive is slow.

Before you start:
  - Turn the TV on and leave it on its home screen.
  - On a Mac, allow this terminal "Local Network" access if macOS asks, and allow
    incoming connections if the firewall asks: the TV fetches the video from here.
  - Some TVs show an "allow this device?" prompt the first time: accept it with the remote.
  - AirPlay needs one-time pairing; Chromecast, DLNA and Roku need none.

Something not working? `castomatic doctor` checks everything and says what to fix.
"""


def use_color(stream: TextIO) -> bool:
    """Colour only on a terminal, and never when NO_COLOR is set (no-color.org)."""
    return stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def render(color: bool = False) -> str:
    """The starburst, optionally in colour."""
    art = STARBURST.format(version=__version__).strip("\n")
    if not color:
        return art
    gold, bold, reset = "\033[33m", "\033[1m", "\033[0m"
    lines = []
    for line in art.splitlines():
        head, sep, tail = line.partition("castomatic")
        if sep:
            lines.append(f"{gold}{head}{reset}{bold}castomatic{reset}{tail}")
        elif "cast anything" in line:
            art_part, _, words = line.partition("cast anything")
            lines.append(f"{gold}{art_part}{reset}cast anything{words}")
        else:
            lines.append(f"{gold}{line}{reset}")
    return "\n".join(lines)


def show(stream: Optional[TextIO] = None, force: bool = False) -> bool:
    """Print the starburst if ``stream`` is a terminal (or ``force``). Returns whether it did."""
    stream = stream or sys.stdout
    if not (force or stream.isatty()):
        return False
    print(render(use_color(stream)), file=stream)
    print(file=stream)
    return True


def welcome(stream: Optional[TextIO] = None) -> None:
    """The first-run greeting: the starburst, then how it works and what to check."""
    stream = stream or sys.stdout
    show(stream, force=True)
    print(WELCOME, file=stream)
