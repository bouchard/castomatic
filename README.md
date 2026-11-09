# castomatic

Play a video file or a web video on your TV, with one command:

```sh
castomatic movie.mkv
castomatic https://www.youtube.com/watch?v=VIDEO
castomatic movie.mkv --start 600          # begin ten minutes in
```

It finds your TV, sends the video the easiest way the TV allows, and tells you what to do
when something goes wrong. It speaks **DLNA** (preferred, no setup) and **AirPlay 2**
(for TVs without DLNA). The AirPlay side is built on
[pyatv](https://github.com/postlund/pyatv) by Pierre Ståhl and its contributors; see
[Credits](#credits-and-licenses).

## What it does by default

| You give it | What happens |
| --- | --- |
| A file the TV can play | Sent **untouched**: the TV downloads the file itself. |
| A file the TV cannot play | Converted **just in time** while the TV downloads it. Video and audio the TV already understands are copied, so usually only the audio (or nothing but the container) is re-encoded. No temporary files. |
| A direct media URL (`.mp4`, `.m3u8`, ...) | Handed to the TV as is when it can fetch it (plain `http://`), otherwise relayed through this computer without re-encoding. |
| Any other URL (a YouTube page, a live stream, ...) | Streamed by [yt-dlp](https://github.com/yt-dlp/yt-dlp) itself into a local buffer; playback starts once enough is buffered that it cannot run dry, and the buffer is then re-streamed to the TV (video and audio copied when the TV takes them). ffmpeg never opens the site's address, so sites that refuse it (HTTP 403) work. See [Web videos](#web-videos-and-live-streams). |
| A web or remote video, over AirPlay | Streamed **live**: ffmpeg writes HLS segments to a temporary folder as it downloads, and playback starts after a few seconds. `--download-first` fetches everything before playing instead. |

Slow files need no options. Each local file is timed against what the video needs, and one
that cannot comfortably keep up, whether on a network drive or a tired USB disk, plays through a
local copy instead: playback starts once the rest can no longer run out, and the whole movie
ends up in the copy. See [Slow files](#slow-files).

It asks the TV what it can play, so "can the TV play this?" is decided from the TV's own
list of media types plus the video and audio codecs, not guessed from the file name.

## Install

```sh
uv tool install .        # or: pip install .
```

You also need **ffmpeg** (with `ffprobe`) on your `PATH`: `brew install ffmpeg`. yt-dlp is
installed with the package and used as a library, so nothing else is needed.

## Choosing the TV

By default the TV is found automatically.

- With **one** TV on the network it is used and remembered.
- With **several**, you are asked once, and the answer is remembered in
  `~/.config/castomatic/config.json` (it holds only the TV's name and address, never
  credentials).
- From then on the remembered TV is used without searching the network.

```sh
castomatic --list                      # show the TVs found
castomatic movie.mkv --choose          # pick a different TV (and remember it)
castomatic movie.mkv --tv Bedroom      # this TV, just this once (name, IP or id)
castomatic --forget                    # forget the remembered TV
```

`--protocol dlna` or `--protocol airplay` forces one; the default `auto` uses DLNA when the
TV has it, because it needs no pairing.

## Options

| Option | Meaning |
| --- | --- |
| `--tv TV` | A TV's name (or part of it), IP address or AirPlay id. Also `--host`, `--id`, and a DLNA description URL |
| `--choose`, `--forget`, `--list` | Pick a TV again, forget the remembered one, or list what is on the network |
| `--protocol {auto,dlna,airplay}` | How to reach the TV (default `auto`) |
| `--start SECONDS` | Start position |
| `--mode {auto,direct,convert}` | `auto` sends untouched when possible, `direct` never converts, `convert` always converts |
| `--subtitles FILE` / `--no-subtitles` | Use this subtitle file, or send none |
| `--download-first` | Web videos: download all of it (with progress) before playing, for a seek bar and subtitles, instead of streaming it through the buffer (AirPlay with a plain remote video: instead of streaming it live) |
| `--ytdlp-format FORMAT` | yt-dlp format selector (default: up to 1080p, H.264 + AAC, so it can be copied) |
| `--cookies-from-browser BROWSER` | Use a browser's login for sites that need it, e.g. `chrome` |
| `--no-jit`, `--workdir DIR` | AirPlay/HLS only: convert everything to disk first |
| `-v` | Show everything sent and received |

## Subtitles

A subtitle is chosen automatically, preferring English: a file you name with
`--subtitles`, then a file next to the video (`Movie.srt`, `Movie.en.srt`, ...), then a
text track inside it. Conversion happens in memory.

- **DLNA**: sent as an SRT file the TV picks up (Samsung's `CaptionInfo.sec`).
- **AirPlay**: offered as a WebVTT track. Most TVs only load it once **you select it in
  the TV's own subtitle menu**.

Videos downloaded by yt-dlp are kept in `castomatic-cache` inside the system temporary folder
(which the OS clears on its own), so playing the same address again, in the same format, reuses
the copy instead of downloading it again.

Subtitles for web videos are fetched by yt-dlp when the video is downloaded first
(`--download-first`, or automatically when ffmpeg cannot read the address). They are
fetched **separately** from the video and retried with a pause between tries, because sites
such as YouTube often answer subtitle requests with "429 Too Many Requests". If they never
come through, the video still plays, with a note saying so. Streaming live does not include
subtitles; use `--subtitles FILE` or `--download-first`.

## Web videos and live streams

For a page yt-dlp handles, yt-dlp's own downloader (so signed addresses, DASH, HLS and live
streams all work) writes MPEG-TS into a growing file in a temporary folder. Nothing plays until
enough is there:

- **Live streams** (no known length): 20 seconds of video, then the TV is kept that far
  behind. `--start` is not available.
- **Other videos**: at least 10 seconds, plus whatever the connection is not expected to
  deliver while the video plays (trusting 80% of the measured speed), so a slow link waits longer.

The buffer is then sent to the TV through ffmpeg, which copies video and audio the TV
understands. yt-dlp is told to keep the original timestamps (`-copyts`) when it joins the
separate video and audio streams of a live video: they rarely start at the same moment, and
without this the audio plays several seconds early. The buffer lives in the temporary folder (`--workdir` to move it) and is removed
when playback ends; a long live stream uses roughly 0.5 GB an hour at 1080p. There are no
subtitles and no seeking while streaming; `--download-first` gives both.

## When something goes wrong

Every failure says what happened and what to try. For example:

```
error: no TV was found on the network.
  hint: Is the TV on and awake? Many TVs stop answering discovery in standby.
        Is this computer on the same Wi-Fi or network as the TV?
        If you know the TV's address, give it: `--tv 192.168.1.50`.
```

The common ones:

- **The TV never starts playing.** The CLI watches for this and says whether the TV never
  asked this computer for the video (usually a firewall: allow incoming connections for
  Python in System Settings > Network > Firewall) or fetched it but could not play it
  (try `--mode convert`).
- **An "allow" prompt on the TV.** Some TVs ask the first time a new device sends to them.
  Accept it with the remote.
- **No TV found while a VPN is on.** A VPN or tunnel can hide the TV; the message lists
  your network interfaces when there is more than one.
- **A web page will not play.** Sites change often: update yt-dlp, or add
  `--cookies-from-browser chrome` for sites that need a login.
- **"yt-dlp did not deliver any video" / "could not stream".** The site is slow, blocking
  yt-dlp or needs a login: update yt-dlp, add `--cookies-from-browser chrome`, or try
  `--download-first`.
- **AirPlay says "not paired".** Pair once with
  `atvremote --id <id> --protocol airplay pair` and enter the PIN shown on the TV.

## How it works

### Slow files

Whether a file needs buffering is measured, not guessed from where it lives. Cold reads at
four places across the file (past the operating system's cache) give the speed it really
reads at, and anything under six times what the video needs goes through a local copy. An
earlier copy of the same path is reused if the file is still exactly the same size, without
reading the source at all.

- **The copy** fills in blocks: first whatever is being read right now, then onward from
  the playback position, then whatever was skipped. Seeking simply moves where it fetches,
  and whatever order the video is watched in, the copy ends up complete. The index (`moov`) is
  fetched first, wherever it is in the file. After the video ends the rest is fetched too;
  Ctrl-C leaves it for next time, and an interrupted copy carries on from where it was.
- **Starting**: playback waits until what is copied, plus what the source will deliver
  while the video plays, covers the whole video, trusting 80% of the measured speed. The
  renderer, or ffmpeg when converting, reads the copy through the same address either way.
- **While playing over DLNA**: the renderer's own position is compared with the copy. If it
  gets within ten seconds, the renderer is paused while the same sum is redone against the
  current speed, then resumed. Renderers that report no position are judged by what they last
  read, and ones that refuse to pause are left to ride it out. AirPlay receivers rebuffer on
  their own.

A file judged fast enough plays directly, but is still checked every 15 seconds. If it stops
reading comfortably faster than the video needs, twice in a row, or the renderer's position
freezes while it says it is playing, playback moves onto a local copy. HLS moves between one
segment and the next with no interruption. A file sent as is, or a live conversion, ties the
renderer to the old address, so it is paused, the copy gets ahead of that position, and the
video is reloaded where it was. A short interruption beats the video dying on the TV.

Copies live in `castomatic-cache-<user>/files` inside the system's temporary folder, which the
OS clears on its own, so a copy lasts until then. When the disk is short of room the least recently used copies are removed; if there is still no room, the
video plays directly from the file.

### DLNA

The TV is found over SSDP and controlled with SOAP `AVTransport` calls
(`SetAVTransportURI`, `Play`, `Pause`, `Seek`, `Stop`). It then downloads the media over HTTP
from a small server this program runs for the length of playback. The renderer's
`ConnectionManager` says which media types it plays, and that decides how the video is sent:

1. **As is**: the file with HTTP Range support.
2. **HLS**: a playlist made from the file's keyframes, with each segment produced by
   ffmpeg only when requested (when the TV lists HLS).
3. **Live MPEG-TS**: one ffmpeg process converting while the TV downloads (for TVs that
   play files but not HLS), copying whichever streams are already supported.

Subtitles go out as an SRT file announced in the DIDL metadata and in the `CaptionInfo.sec`
response header. Samsung TVs also need the `contentFeatures.dlna.org` response header and a
`Stop` before a new item; both are handled.

### AirPlay 2

Everything runs over one HAP-encrypted RTSP connection, using pyatv's pairing and crypto.
The flow was captured from a commercial sender talking to a fake receiver:

1. `pair-verify` with your stored credentials.
2. `SETUP` (session), connect the encrypted **event channel**, `RECORD`, `GET /info`.
3. `SETUP` with a stream of `type` 130; the receiver returns a `streamID`.
4. `POST /command` (header `X-Apple-StreamID`) with `{"params": {"data": <binary plist>}}`,
   whose inner plist is `insertPlayQueueItem` carrying an `http://` HLS playlist URL, then
   `setRate`. `POST /feedback` every 2 s keeps the session alive.
5. `removePlayQueueItem` and `TEARDOWN` end it.

There is no `/play`, `/reverse` or FairPlay step on these receivers. HLS comes from the
same just-in-time server as above: H.264 and AAC are copied, anything else is converted,
and segments are cut just before the next keyframe's decode time so neighbours neither
overlap nor leave gaps.

That server needs the whole file's keyframe list up front, which for a remote video would
mean downloading it all first. So a **remote** video is instead handled by ffmpeg writing
HLS segments into a temporary folder as it fetches them, with a playlist that grows until
the video is complete (an HLS `EVENT` playlist). Playback starts once the first two
segments exist, and ffmpeg stays well ahead of playback for a copied video. The temporary
folder is removed when playback ends or you press Ctrl-C.

## Library use

```python
from castomatic import DlnaPlayer, JitHlsServer, find_renderer, plan
from pathlib import Path

player = DlnaPlayer(await find_renderer(host="192.168.1.50"))
await player.connect()
server = JitHlsServer(await plan(Path("movie.mkv")), player.local_ip)   # HLS
player.mime = "application/vnd.apple.mpegurl"
await player.play(await server.start(), title="Movie")
await player.wait_until_ended()
await player.stop(); await server.stop(); await player.close()
```

`AirPlayPlayer` (AirPlay) has the same methods; see `castomatic/cli.py` for a complete example
of choosing a delivery method.

## Development

```sh
uv run --group dev pytest        # fails below 95% coverage
```

Network pieces are tested against in-process mock renderers and receivers. The HLS and
streaming tests generate a tiny video with ffmpeg and are skipped without it.

## Limitations

- Tested on one TV, a Samsung UTU7000_KA, over both DLNA and AirPlay. Others may differ.
- While a web video streams live over AirPlay, the TV's seek bar covers only what has been
  fetched so far, and subtitles are not included (`--download-first` fixes both).
- No seeking while playing yet (use `--start`), choice between several subtitle or audio
  tracks, burned-in subtitles (they need an ffmpeg built with libass), or DRM.
- A video with no MP4/MOV index can only be cut into HLS once its keyframes are known, so
  over HLS such a file is copied in full first (sent as is over DLNA, it starts as usual).
- Jumping far past what has been copied waits for that part to arrive.
- A converted video is streamed live, so the TV's own seek bar does not work on it.
- Video that needs re-encoding uses VideoToolbox on a Mac, else software `libx264`, which
  may not keep up in real time for high-bitrate sources.

## Credits and licenses

### pyatv

This project would not exist without **[pyatv](https://github.com/postlund/pyatv)**
([pyatv.dev](https://pyatv.dev)), created by **Pierre Ståhl** and maintained by its
contributors, and released under the MIT License (Copyright (c) 2020 Pierre Ståhl). It is
the reason AirPlay works here at all. Thank you.

pyatv is used as a normal dependency: it is installed from PyPI, imported as is, and none of
its source files are copied into this repository or modified. Specifically, the AirPlay
sender relies on pyatv for:

- **Device discovery and pairing.** Scanning for AirPlay receivers, reading the credentials
  stored by pyatv (`atvremote ... pair`), and parsing them (`pyatv.scan`,
  `FileStorage`, `HapCredentials`).
- **HomeKit pairing and encryption.** The `pair-verify` handshake and the HAP-encrypted
  channel set-up, including the key derivation for the event channel
  (`pyatv.protocols.airplay.auth`, `pyatv.auth.hap_channel`, and the event-channel salts in
  `pyatv.protocols.airplay.ap2_session`).
- **RTSP/HTTP transport.** The connection and request/response handling used to talk to the
  receiver (`pyatv.support.http`, `pyatv.support.rtsp`).
- **The event channel.** `castomatic.airplay` subclasses pyatv's `EventChannel` to read the
  receiver's playback events, and its `AirPlayError` extends pyatv's `PlaybackError`.

pyatv's server-side pairing code was also used to build a throw-away fake AirPlay receiver
for capturing how a commercial sender behaves. That is how the AirPlay video flow documented
above was worked out; the capture tool is not part of this repository. pyatv's open
documentation and source were the reference for the pairing and transport details.

If any pyatv source is ever copied into this project, it will keep its original copyright
and license notice and be marked as such. This project is independent: it is not affiliated
with, or endorsed by, the pyatv project.

### Other software

| Project | Used for | License |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Finding the real video address behind web pages (read as a library, and run to stream the video) | Unlicense |
| [FFmpeg](https://ffmpeg.org) | Inspecting and converting video (run as a separate program) | LGPL/GPL |
| [aiohttp](https://github.com/aio-libs/aiohttp) | The small HTTP servers the TV downloads from | Apache-2.0 |
| [ifaddr](https://github.com/ifaddr/ifaddr) | Listing network interfaces so discovery works with VPNs | MIT |
| [srptools](https://github.com/idlesign/srptools) | SRP, required by pyatv's pairing | BSD |

The DLNA/UPnP support follows the public UPnP AV and DLNA specifications; Samsung-specific
behaviour (`CaptionInfo.sec`) was learned from the TV's own requests.

This project is licensed under the MIT License; see [LICENSE](LICENSE).
