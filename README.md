<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/castomatic-dark.svg">
    <img src="assets/castomatic-light.svg" alt="castomatic: cast anything, anywhere" width="460">
  </picture>
</p>

<p align="center">
  Play a video file, a video address or a web page on <b>any TV</b>, with one command,<br>
  over <b>Chromecast</b>, <b>DLNA</b>, <b>Roku</b> or <b>AirPlay 2</b>.
</p>

```sh
castomatic movie.mkv
castomatic https://www.youtube.com/watch?v=VIDEO
castomatic https://example.com/video.mp4 --start 600    # begin ten minutes in
```

Castomatic finds every TV on your network and picks the way of reaching it that will play this
video most reliably. It sends the video **untouched** when the TV plays it and **converted just in
time** when it does not, **buffers first** when your network or drive is slow, and **tries the
next way** if the first one does not start. When something goes wrong it says what happened and
what to do.

Castomatic grew out of [pyatv-cast](https://github.com/bouchard/pyatv-cast), and its remembered
TV is carried over. It builds on:

- [pyatv](https://github.com/postlund/pyatv) for AirPlay
- [catt](https://github.com/skorokithakis/catt) for Chromecast
- [rokuecp](https://github.com/ctalkington/python-rokuecp) for Roku
- [castor](https://github.com/stupside/castor), when installed, for pages nothing else can read

See [Credits](#credits-and-licenses).

## Quick start

```sh
brew install ffmpeg      # or your package manager: ffmpeg and ffprobe must be on PATH
uv tool install .        # or: pip install .
castomatic doctor        # checks everything, finds your TVs, says what to fix
castomatic movie.mkv     # play something
```

The first run explains itself. In short:

1. Turn the TV on and leave it on its home screen.
2. On a Mac, allow the terminal **Local Network** access and Python **incoming connections**
   when macOS asks: the TV fetches the video from this computer.
3. Some TVs show an "allow this device?" prompt the first time. Accept it with the remote.
4. Chromecast, DLNA and Roku need no setup. AirPlay needs pairing once (see
   [Troubleshooting](#troubleshooting)). Roku needs **Control by mobile apps** left on.

## What happens to each kind of source

| You give it | What happens |
| --- | --- |
| A file the TV can play | Sent **untouched**: the TV downloads the file itself, with seeking. |
| A file the TV cannot play | **Remuxed or converted just in time** while the TV downloads it. Streams the TV understands are copied, so usually only the container changes, or only the audio is re-encoded. No temporary files. |
| A file on a slow drive or network share | Its read speed is measured first. If it cannot comfortably keep up, it plays through a local copy that stays ahead: **a short wait at the start instead of stalls part way**. Network shares are held to a higher bar. See [Slow files](#slow-files). |
| A video address (`.mp4`, `.mkv`, `.m3u8`, ...) | Handed straight to the TV when it can fetch and play it. If the address downloads slower than the video plays, it is **buffered here first**, the same way. |
| Any other page (YouTube, a live stream, ...) | [yt-dlp](https://github.com/yt-dlp/yt-dlp) streams it into a local buffer, and playback starts once the buffer cannot run dry. If yt-dlp cannot read the page and [castor](#castor-optional) is installed, castor finds the video instead. |

Whether the TV can play a video is decided by comparing the video's real codecs with what the TV
plays. A DLNA TV lists what it plays. For Chromecast and Roku, Castomatic uses what every receiver
of that protocol is published to play. Nothing depends on the TV's brand or model.

### Which way, when a TV offers several

Many TVs answer on more than one protocol. Castomatic tries them in this order, and moves on
automatically if one does not start playing:

1. **The way that plays this video untouched.** Converting is the last resort.
2. Then by reliability:
   1. **Chromecast**: reports exactly what it is doing, seeks, and takes subtitles.
   2. **DLNA**: needs no pairing and says what it plays.
   3. **Roku**: cannot be told where to start, and takes no subtitle file.
   4. **AirPlay**: needs pairing, and only takes HLS.

If the TV fetched an untouched video but never started it, the same way is tried once more with
the video converted, then the next protocol. `castomatic explain movie.mkv` shows the whole plan
without playing anything:

```
Source:    movie
Streams:   video h264 (yuv420p), audio ac3, 5400s
Buffering: not needed: reads at 834.4 MB/s, plays at 1.2 MB/s
TV:        Living Room (192.168.1.50), reachable over Chromecast, DLNA, Roku
Plan:      tried in this order, moving on if one does not start:
  1. Roku: the file is sent untouched
  2. Chromecast: repackaged as HLS segment by segment, as the TV asks (the TV does not list .mkv files; audio is ac3)
  3. DLNA: decided once connected (the TV is asked what it plays)
```

`--protocol chromecast|dlna|roku|airplay` uses one way only. `--mode direct` never converts and
`--mode convert` always does.

## Choosing the TV

- With **one** TV on the network it is used and remembered.
- With **several**, you are asked once, and the answer is remembered in
  `~/.config/castomatic/config.json`. That file holds only the TV's name, its address and which
  protocols it answered on, never credentials.
- From then on the remembered TV is reached without searching the network, except over AirPlay.

```sh
castomatic --list                      # show the TVs found, and how each can be reached
castomatic movie.mkv --choose          # pick a different TV (and remember it)
castomatic movie.mkv --tv Bedroom      # this TV, just this once (name, IP, AirPlay id, Cast UUID, Roku serial)
castomatic --forget                    # forget the remembered TV
```

## Commands and options

| Command | What it does |
| --- | --- |
| `castomatic SOURCE` | Play a file, address or page |
| `castomatic doctor [--save FILE]` | Check ffmpeg, the network, the firewall and every protocol's search, and say what to fix |
| `castomatic explain SOURCE` | Show what would happen and why, without playing |

| Option | Meaning |
| --- | --- |
| `--tv TV`, `--choose`, `--forget`, `--list` | Pick a TV (see above) |
| `--protocol {auto,chromecast,dlna,roku,airplay}` | How to reach the TV (default `auto`: see above) |
| `--start SECONDS` | Start position |
| `--mode {auto,direct,convert}` | `auto` sends untouched when possible, `direct` never converts, `convert` always does |
| `--subtitles FILE` / `--no-subtitles` | Use this subtitle file, or send none |
| `--burn-subtitles` | Draw the subtitles onto the picture. This re-encodes the video; it is for TVs that take no subtitle file, such as Roku |
| `--download-first` | Web videos: download all of it before playing, for a seek bar and subtitles |
| `--ytdlp-format FORMAT` | yt-dlp format selector (default: up to 1080p, H.264 + AAC, so it can be copied) |
| `--cookies-from-browser BROWSER` | Use a browser's login for sites that need it, e.g. `chrome` |
| `--engine {castomatic,castor}` | `castor` hands the whole cast to castor (if installed) |
| `--no-jit`, `--workdir DIR` | HLS: convert everything to disk first |
| `-v` / `-vv` | `-v` shows each decision and why; `-vv` also shows everything sent to and received from the TV |
| `--log-file FILE` | Also write the full log to a file, for a bug report |
| `-q` | No welcome or banner |

## Subtitles

A subtitle is chosen automatically, preferring English, from the first of these that exists:

1. A file you name with `--subtitles`.
2. A file next to the video (`Movie.srt`, `Movie.en.srt`, ...).
3. A text track inside the video.

| TV | How subtitles are sent |
| --- | --- |
| Chromecast | A WebVTT file loaded with the video and switched on |
| DLNA | An SRT file announced with the video |
| AirPlay | A WebVTT track: choose it in the TV's own subtitle menu |
| Roku | Roku's media player takes no subtitle file: use `--burn-subtitles` |

`--burn-subtitles` needs an ffmpeg built with libass; `castomatic doctor` checks for it. Subtitles
for web videos come with `--download-first`.

## castor (optional)

[castor](https://github.com/stupside/castor) finds the video on a web page by opening it in
headless Chrome, which works on pages yt-dlp has no extractor for. Castomatic never needs it,
but when it is installed (`brew install --cask stupside/tap/castor`):

- **When yt-dlp cannot read a page**, Castomatic asks castor (`castor cast player URL --dry-run`)
  for the streams on it, and plays the best one itself, with the usual buffering, conversion and
  protocols.
- **If that stream only plays from inside castor**, or with `--engine castor`, the whole cast is
  handed to castor, pointed at the TV Castomatic found (DLNA, Chromecast or Roku).

## Troubleshooting

Start with `castomatic doctor`. Every failure also says what happened and what to try:

```
error: no TV was found on the network.
  hint: Is the TV on and awake? Many TVs stop answering discovery in standby.
        Is this computer on the same Wi-Fi or network as the TV?
        If you know the TV's address, give it: `--tv 192.168.1.50`.
        Run `castomatic doctor` to see what each kind of search found.
```

- **The TV never starts playing.** Castomatic watches for this, and if another way is left it
  tries that and says why. Otherwise it says which of two things happened:
  - the TV never asked this computer for the video, usually because of the firewall (allow
    incoming connections for Python);
  - the TV fetched the video but could not play it (try `--mode convert`).
- **No TV found.** Check the TV is awake and on the same network, turn off a VPN, and on a Mac
  allow the terminal Local Network access (System Settings > Privacy & Security).
- **Roku refuses (HTTP 403).** On the Roku: Settings > System > Advanced system settings >
  Control by mobile apps > Network access: Default.
- **AirPlay says "not paired".** Pair once with `atvremote --id <id> --protocol airplay pair`
  and enter the PIN shown on the TV. Usually not needed: the same TV often answers over DLNA or
  Chromecast too.
- **A web page will not play.** Update yt-dlp, add `--cookies-from-browser chrome` for sites
  that need a login, try `--download-first`, or install castor.
- **Reporting a bug.** Run `castomatic -vv --log-file castomatic.log SOURCE` and
  `castomatic doctor --save doctor.txt`, then attach both files.

## How it works

Every protocol makes the TV fetch the video over HTTP from small servers Castomatic runs for the
length of playback. The same servers, buffering and conversion are used for every protocol:

| Delivery | When |
| --- | --- |
| The file as it is, with Range requests | The TV plays it |
| The address itself | A remote video the TV can fetch and play |
| Just-in-time HLS: segments cut at keyframes and made by ffmpeg as the TV asks for them, with streams copied when possible | A local file the TV cannot play as it is, on a TV that plays HLS |
| Live HLS: ffmpeg writing segments as it goes | Web videos and remote addresses, on Chromecast, Roku and AirPlay; any `--start` on Roku |
| Live MPEG-TS: one ffmpeg stream | Web videos and remote addresses, on DLNA renderers |

Discovery runs every protocol's search at once: SSDP for DLNA and Roku, mDNS for Chromecast, and
pyatv for AirPlay. Each search has its own time limit. The SSDP searches go out from every network
interface, so a VPN does not hide the TV. Whatever answers from the same address is merged into
one TV.

### Chromecast

Built on [catt](https://github.com/skorokithakis/catt) and its pychromecast:

- **Discovery** reads each device's mDNS announcement, and skips devices without a screen.
- **Playback** starts the Default Media Receiver and loads the media with catt's controller.

Every server allows any origin (CORS), which Cast receivers need.

### Roku

Built on the External Control Protocol, through [rokuecp](https://github.com/ctalkington/python-rokuecp):

- **Discovery** is an SSDP search for `roku:ecp`.
- **Playback** uses the built-in media player channel (`/input/15985`), and progress is read
  from `/query/media-player`.

That channel cannot be told where to start, so `--start` sends a conversion that begins there
instead.

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

Subtitles go out as an SRT file announced in the DIDL metadata (as a standard `res` and as the
widely read `CaptionInfo.sec` extension). Every response carries `contentFeatures.dlna.org`,
and a `Stop` is sent before each new item, which some renderers need and the rest ignore.

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

### Web videos and live streams

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

## Library use

Every player has the same methods, so the servers work with any of them:

```python
from castomatic import ChromecastPlayer, JitHlsServer, plan
from castomatic.backends.chromecast import discover
from pathlib import Path

player = ChromecastPlayer((await discover())[0])
await player.connect()
server = JitHlsServer(await plan(Path("movie.mkv")), player.local_ip)
player.mime = "application/vnd.apple.mpegurl"
await player.play(await server.start(), title="Movie")
await player.wait_until_ended()
await player.stop(); await server.stop(); await player.close()
```

`DlnaPlayer`, `RokuPlayer` and `AirPlayPlayer` work the same way. `castomatic.planner` decides
how to send a video from a TV's `Capabilities`; see `castomatic/cli.py` for the whole flow.

## Development

```sh
uv run --group dev pytest        # fails below 95% coverage
```

Every protocol is tested against fakes that behave like the real thing:

- an in-process DLNA renderer and an in-process Roku, both of which really fetch the video from
  Castomatic's servers;
- a Chromecast faked at catt's device boundary;
- a fake AirPlay receiver;
- a fake `castor` program.

Decisions are tested as tables of video and TV capabilities. Tests that need ffmpeg generate a
tiny video and are skipped without it.

## Limitations

- Tested on real hardware so far: a Samsung TV over DLNA and AirPlay. Chromecast and Roku are
  built to their published behaviour and tested against fakes. Reports from real devices are
  welcome.
- No seeking from the command line while playing (use `--start`), no choice between several
  audio or subtitle tracks, and no DRM.
- A converted video is streamed live, so the TV's own seek bar does not work on it.
- Re-encoding uses VideoToolbox on a Mac, else software `libx264`, which may not keep up in real
  time for high-bitrate sources.

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
| [catt](https://github.com/skorokithakis/catt) and [pychromecast](https://github.com/home-assistant-libs/pychromecast) | Chromecast discovery and control | BSD-2-Clause, MIT |
| [rokuecp](https://github.com/ctalkington/python-rokuecp) | Roku control (ECP) | MIT |
| [castor](https://github.com/stupside/castor) | Optional: finding videos on pages yt-dlp cannot read (run as a separate program, never bundled) | MIT |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Finding the real video address behind web pages, and streaming it | Unlicense |
| [FFmpeg](https://ffmpeg.org) | Inspecting and converting video (run as a separate program) | LGPL/GPL |
| [aiohttp](https://github.com/aio-libs/aiohttp) | The small HTTP servers the TV downloads from | Apache-2.0 |
| [ifaddr](https://github.com/ifaddr/ifaddr) | Listing network interfaces so discovery works with VPNs | MIT |
| [srptools](https://github.com/idlesign/srptools) | SRP, required by pyatv's pairing | BSD |

DLNA/UPnP support follows the public UPnP AV and DLNA specifications; Roku support follows Roku's
published External Control Protocol documentation.

This project is licensed under the MIT License; see [LICENSE](LICENSE).
