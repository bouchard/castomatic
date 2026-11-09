"""Errors meant for the person at the keyboard: what went wrong, and what to try."""

from typing import Optional


class UserError(Exception):
    """A problem to report to the user as a message plus an optional next step."""

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        """Initialize a new UserError."""
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        """Format for the terminal."""
        text = f"error: {self.message}"
        if self.hint:
            hint_lines = self.hint.strip().splitlines()
            text += "\n  hint: " + "\n        ".join(hint_lines)
        return text


# UPnP AVTransport error codes a renderer may answer with, and what usually causes them
UPNP_HINTS = {
    701: "The TV says playback is not possible right now. Wake the TV and leave it on its home screen.",
    705: "The TV is busy with another session. Stop whatever is playing on it, "
    "or pick up the remote and go back to the home screen, then try again.",
    714: "The TV does not accept this media type. Try `--mode convert` to send it in a "
    "format the TV plays.",
    715: "The TV refused the content. Try `--mode convert`.",
    716: "The TV could not fetch the media from this computer. A firewall on this Mac is the "
    "usual cause: System Settings > Network > Firewall, then allow incoming connections "
    "for Python (or turn the firewall off while testing).",
    718: "The TV rejected the request. Restart the TV's media player (go to the home screen) "
    "and try again.",
    737: "The TV has no free media slot. Stop what it is playing and try again.",
}
