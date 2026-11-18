# Chromecast investigation (2026-11-15)

Test file: `/Volumes/video/tv/Saturday Night Live (1975)/Season 52/Saturday Night Live - S52E06 - John Mulaney.mkv`
(HEVC Main 10 1080p, E-AC3 5.1, embedded SRT, 4124 s, 1.9 Mbps, on an SMB share that reads at ~0.26 MB/s).

Receiver: "Travel TV", 192.168.8.145, model name `Chromecast`, cast firmware 3.72.446070,
uuid `5dd9b91a-ffe0-3bbe-b606-38aa6ccada2a`.

## Status

| Step | Result |
| --- | --- |
| `castomatic.sh` wrapper (runs via `uv run --project <repo>`) | Done, works from any directory. README mentions it. |
| Original code (HEVC + E-AC3 both transcoded, JIT HLS) | **Works**: played 4.5 min with no stalls, clean Ctrl-C stop. |
| catt, same file | Video plays, **no audio** (E-AC3 sent untouched). Started in ~4 s vs ~37 s for castomatic (share speed measurement plus buffer). |
| New: copy HEVC, convert only the audio | Plays and shows the picture. Segments are ~1–3 MB instead of 10–20 MB. Fixed one stutter bug (below). **Audio still ~0.5 s ahead of video**: open issue. |

Uncommitted changes: `castomatic/{hls,cli,config,buffering,planner}.py`, `castomatic/backends/chromecast.py`,
`tests/test_hls.py`, `README.md`, `castomatic.sh`. All 650 tests pass with 98.9% coverage (session 4).

## What the receiver does and doesn't report (measured)

| Sent | `playerState` | `videoInfo` | Notes |
| --- | --- | --- | --- |
| HEVC copied, MKV/MP4 file | PLAYING | 1920x1080 | |
| HEVC copied, HLS TS or fMP4 | PLAYING | 1920x1080 | |
| MPEG-2 video (can't decode), file or HLS | **PLAYING** | missing | HLS tracks list shows audio only |
| E-AC3 copied in a plain file | PLAYING | n/a | Silent (catt case). Undetectable. |
| E-AC3 copied in HLS fMP4 | IDLE / ERROR | — | |

- A receiver that can't decode the video **still reports PLAYING**, so "never started" failover doesn't catch it.
  Whether a picture is shown can be detected: `videoInfo` (in raw MEDIA_STATUS; pychromecast drops it) appears
  only when the video is decoded.
- Undecodable audio in a plain file can't be detected, so audio stays on Google's published baseline list
  (E-AC3/AC3 are always converted; that's cheap).
- Device identity can't decide codecs. Model name is just "Chromecast" for gen 1/2/3 and Chromecast with
  Google TV, which differ exactly on HEVC. `eureka_info` (port 8008) has no hardware model. A model table
  would also break the "capabilities, never brand/model" rule.

## Changes made

1. **`backends/chromecast.py`**: `TRIED_VIDEO = {"hevc"}` added to `CAPABILITIES.video` and `.live_video`
   (copied optimistically). A `PictureListener` media-namespace handler records `videoInfo`.
   `shows_picture()` returns True when `videoInfo` arrives, or False after 10 s of PLAYING without it.
   `capabilities()` drops codecs remembered for this device. `remember_cannot_show(codec)`.
2. **`config.py`**: `cannot_show(device)` and `remember_cannot_show(device, codec)`, stored under
   `devices.<id>.cannot_show` in `~/.config/castomatic/config.json`.
3. **`cli.py`**: `_watch_start(..., picture=)` calls `_check_picture` once PLAYING. A missing picture becomes
   `StartFailed(no_picture=True, fetched=True)`. `_play_renderer` remembers the codec and marks the failure
   `untouched`, so the existing failover retries with `--mode convert` (and the next run plans correctly).
   `_copies_video()` decides when to check. The `hls` delivery passes `caps.live_video/live_audio` to
   `hls.plan` for players built around HLS (`caps.live == LIVE_HLS`: Chromecast, Roku). DLNA and AirPlay keep
   the old H.264/AAC-only defaults.
4. **`hls.py`**: `plan(..., video_ok=, audio_ok=)`. `MediaPlan.video_codec`. Copy pixel formats per codec.
   Bitstream filter per codec (`hevc_mp4toannexb`).
5. **Stutter bug fix, `hls.segment_command`**: added `-copypriorss 0` for copied video. With B-frames,
   ffmpeg seeks 3/23 s before `-ss` (its dts heuristic), so a cut exactly on a keyframe lands on the
   *previous* keyframe. Transcoding discards those frames, but stream copy kept them, so every segment
   repeated the previous GOP (~500 frames instead of ~250). This affects any copied video with B-frames
   from MKV, H.264 included, not just HEVC. Regression test:
   `test_copied_segments_do_not_repeat_the_one_before` (h264 + hevc).
   TODO: confirm the test fails without the fix.

## Open issue: audio ~0.5 s ahead with copied HEVC

- castomatic's served segments are correctly timestamped. Fetched from the live server: first video/audio
  pts agree within ~20 ms in segments 0, 1 and 40 (video 8.342 vs audio 8.321, and so on).
- With video transcoded to H.264 (the first run), sync was fine. Audio processing is identical in both runs.
- Hypothesis: the receiver presents **HEVC in MPEG-TS** late. HLS only defines HEVC in fMP4 segments, so
  HEVC-in-TS is non-standard (the receiver's TS transmuxer).
- Next step, ready: sync clips in `<scratchpad>/sync/`. `src.mkv` is HEVC 10-bit with B-frames plus E-AC3,
  with a white flash and 1 kHz beep for 0.1 s every 2 s. Three HLS packagings: `ts_hevc/`, `fmp4_hevc/`,
  `ts_h264/` (baseline). Serve with `python serve.py <scratchpad>` (port 8765, CORS) and load each with
  `load.py <url> application/vnd.apple.mpegurl`, then watch flash vs beep.
  Scratchpad: `/private/tmp/claude-501/-Users-brady-code-castomatic/73b01f4e-ccac-421b-a283-1b2cb8c26521/scratchpad`
  (may be cleared on reboot; the commands are easy to recreate).
- If fMP4 fixes it: JIT HLS needs an fMP4 mode for copied HEVC (`#EXT-X-MAP` init segment, `moof/mdat`
  segments with correct `tfdt`, `hvc1` tag, `#EXT-X-VERSION:7`). If not, fall back to transcoding HEVC
  for Chromecast (the original behaviour), or deliver as a progressive remux (MKV played fine via catt).

## Update, session 2 (2026-11-15, later)

**Sync clip test (watched on the TV):** 1) HEVC in TS: *no picture at all* (yet the receiver said PLAYING);
2) HEVC in fMP4: in sync; 3) H.264 in TS: in sync. So copied HEVC must go in fMP4.

**Done:**
- **fMP4 JIT segments in `hls.py`.** When copied video is HEVC (`FMP4_VIDEO`), `MediaPlan.fmp4` is set. That
  gives an EXT-X-VERSION 7 playlist with `#EXT-X-MAP:URI="init.mp4"` and `segNNNNN.m4s` segments.
  `_fmp4_segment_command` uses `-copyts -ss S -i src -to S+cut`, `-output_ts_offset time_offset`
  (= ts_shift + `FMP4_PAD` 0.1 s, to keep B-frame and AAC-priming timestamps ≥ 0),
  `-movflags +frag_keyframe+empty_moov+default_base_moof+frag_discont`, `-use_editlist 0`,
  `-video_track_timescale 90000` and `-tag:v hvc1`. `frag_discont` is what makes `tfdt` the real decode
  time; without it every file starts at 0. `split_init()` splits ftyp+moov from the first segment made
  (byte-identical across runs) and the server serves it at `/init.mp4`. `convert_to_directory` and
  `StaticHlsServer` handle `.m4s` too. WebVTT `X-TIMESTAMP-MAP` uses `time_offset`.
- **Verified on the TV:** the SNL episode (copied HEVC + AAC in fMP4) is **in sync**.
- **Tests added:** fMP4 playlist/command, `split_init`, a real JIT fMP4 round trip checking `tfdt`,
  `convert_to_directory` fMP4, Chromecast `capabilities`/`remember_cannot_show`/`shows_picture`/
  PictureListener, and config `cannot_show` memory. All pass. **Still missing:** cli tests for
  `_check_picture`, `_copies_video` and the no-picture → remember → retry path in `_play_renderer`.
  The full suite and coverage gate have not been re-run since.

**"Very slight A/V stutter every few seconds" (user report, SNL, fMP4): fixed in code, awaiting a TV check.**
- Cause: **segments overlapped by 3–5 frames.** `mkvindex.parse_cues` returns dts = pts (Matroska Cues have
  no decode times), so each segment was cut at the next keyframe's *pts*. ffmpeg's `-to` with `-copyts`
  compares **dts**, which runs ahead of pts with B-frames, so each segment ended with the next one's
  keyframe and the frames after it.
- A single per-file delay is **not enough**: x265's adaptive B-frames give each keyframe its own lead.
  Measured on SNL: 8.342→0.134 s, 83.417→0.067 s, 91.758→0.133 s, 100.100→0.167 s. A first fix with one
  measured delay still left the 11→12 join overlapping by 3 frames.
- **Fix (`hls.py`):** `keyframe_delay(source, at)` ffprobes 8 packets from an exact seek to a keyframe.
  After a seek the first dts values are N/A, so it counts back one frame per packet from the first known
  ones (checked against full reads: exact). It returns lead + half a frame, so the cut lands between
  frames whatever the ms rounding. It returns None if the seek didn't land on that keyframe. `plan()`
  uses one keyframe's lead as a first guess and sets `MediaPlan.cuts_guessed`. The new
  `settle_cut(media, i)` measures segment i's real end just before the segment is made (JIT `_convert`
  and `convert_to_directory`), once per segment. This costs one small ffprobe per segment and logs at
  debug level when the guess was off. Playlist durations don't change.
- **Verified locally** on the full SNL file (Cues index, the same path as the CLI): segments 0–13 each join
  with exactly a one-frame step (0.033 s), with no repeats and no gaps. The sync clip is clean too.
  (`<scratchpad>/local.mkv` is sparse: only the first ~127 MB is real, so later segments can't be checked
  there.)
- **Tests:** `keyframe_delay` (count-back, no B-frames, failed seeks), `settle_cut` (per-keyframe leads,
  once each, unreadable keeps the guess, no-op with real dts), and an end-to-end Matroska/x265 test
  (b-adapt=2): every frame appears exactly once across all segments. It fails with the fix disabled.
- CLI tests added for `_check_picture`, `_copies_video`, `_watch_start(picture=)` and the no-picture →
  remember → retry-converted path. **Full suite: 634 passed, coverage 98.8%.**
- **Next:** play SNL on the TV again and confirm the stutter is gone (the SMB share `/Volumes/video` was
  not mounted at the time). If a slight stutter remains, the suspect is audio: each segment's AAC is
  encoded separately and starts with a 21 ms priming frame that overlaps the previous segment's end.
  Options are to strip it (trun/tfdt/mdat surgery) or use FLAC if the receiver takes it.

## Update, session 4 (2026-11-16)

With the share mounted, the episode played from the complete local copy, so its **embedded subtitles were
included for the first time** (they're skipped on a first play from the slow share). That serves a master
playlist, and the TV stopped 3 s in with "receiver reported a playback error". Two fixes:

- **No `CODECS` in the master playlist → receiver ERROR.** castomatic only named codecs for copied H.264.
  Without them the receiver assumes H.264 and fails on HEVC. Given the media playlist directly (every
  earlier copied-HEVC run) it reads the codec from the segments, so this never showed up before. A/B
  tested on the TV with the same segments: no CODECS → IDLE/ERROR; `CODECS="hvc1.2.4.L120.90,mp4a.40.2"`
  → plays. Fix: `hevc_codec_string(profile, level)` from ffprobe's fields (main tier, `90` constraints).
  CODECS is now also set when the audio is *converted* to AAC (only copied AAC counted before).
- **Audio holes at every join** (caused by the session-3 cut fix). The audio was cut at the video's
  shortened cut point, leaving 85–130 ms holes. Both segment commands now open the source twice: video ends
  at `cut`, audio at the next segment's start (TS path: converted video also runs to `duration`, since
  decoded frames are trimmed by pts). Audio now overlaps the next segment by ~40 ms (AAC priming plus frame
  rounding), which MSE overwrites. Static A/B on the TV: the old-style and new-style cut segments both play.
- Tests: `hevc_codec_string`, plan CODECS for copied h264/hevc with converted audio, the fMP4 command's
  two `-to` values and the audio map, and the Matroska end-to-end test now has audio and checks there's no
  hole at any join (it fails with audio cut at the video's end). **638 passed, coverage 98.8%.**
- Live run on the TV: it played, but with **severe video stutter** (audio fine).

**First theory (wrong): open-GOP keyframes.** The x265 keyframes are CRA + RASL (frames shown before the
keyframe that reference the previous GOP). A/B on the TV: exact per-keyframe cuts → severe stutter;
session-2 overlapping cuts → slight stutter; with or without master playlist → same. A continuous
fragmented-MP4 stream (no segments) played smoothly and in sync (once started on a keyframe with
`-copyts` + `frag_discont`; a non-keyframe start put audio 7.7 s off). Built as a `remux` delivery, and the
TV check passed with subtitles. **But the TV's seek bar can't seek in it, and the user prefers keeping
seeking**, so it was replaced (the code is in `<scratchpad>/retired/`).

**Real root cause: audio discontinuity at every join.** HLS segments sliced from *one* continuous ffmpeg run
(same cut points, same timestamps) played smoothly on the TV, and seeking worked (40 s, seek to 1:40, fine).
Comparing segment 1 from both: **the video sample tables are byte-identical**. Only the audio differs.
Per-segment runs encode audio separately: each starts with the encoder's priming frame, a hole or overlap at
every join (session 2 slightly, session 3 130 ms holes, then 40 ms overlaps). A continuous run's audio flows
on. The receiver keeps time by the audio, so the video stuttered.

**Fix: `hls.FragmentRun`.** For fMP4 (copied HEVC), segments are cut from one continuous ffmpeg run
(`run_command`: `-copyts -ss <segment start>`, no `-to`, `-output_ts_offset time_offset`,
`frag_keyframe+empty_moov+default_base_moof+frag_discont`). Output is split into boxes as it arrives. The
first moof sets the init segment. Each fragment (one GOP, starting on a keyframe) goes to the segment its
first video frame falls in (`fragment_start`: track 1 tfdt + first composition offset at 90 kHz). A
segment is complete when a fragment of a later segment arrives (or at EOF). `JitHlsServer` waits on the
current run when the wanted segment is within `AHEAD` (2) of where it is, and otherwise starts a new run
there (a seek). A run stops reading `AHEAD` segments past the last request, so ffmpeg blocks and costs
nothing. `use_source` restarts the run for anyone waiting. `/init.mp4` comes from the current run, or a new
one at the last asked position. `convert_to_directory` uses one run to the end. Bug found and fixed: a
paused run's full pipe made `proc.wait()` hang after kill (now drained first). Removed: per-segment fMP4
command, `split_init`, open-GOP detection, the `remux` delivery.
- Locally on SNL: init and segments 0–3 identical to the slices that played well; jump to 100 and back to
  4 starts new runs, 0.2–0.5 s per segment.
- The MPEG-TS path (copied H.264 / converted video) still converts audio per segment, so it may show the same
  slight stutter. Not yet changed (DLNA HLS players might not take fMP4).
- Tests: `run_command`, `fragment_start` (synthetic boxes), runs follow playback / restart on seek / pause
  ahead / init identical across runs, `use_source` takeover, failures → HTTP 500 and conversion error,
  broken boxes, Matroska end-to-end now checks that audio is seamless at every join, and a Chromecast CLI test.
  **650 passed, coverage 98.9%.**
- **Next:** full episode on the TV through castomatic, checking stutter and sync, then seeking with the
  remote, forward and back. (Waiting for the user.)

## Other observations (not changed)

- The JIT HLS transcode uses **libx264 CRF 20** (software), not the `h264_videotoolbox` that `doctor`
  reports. Output was ~15 Mbps for a 1.9 Mbps source. Encoding runs ~4x real time on an M1 Pro, so speed
  is fine, but adding a `-maxrate` cap would cut Wi-Fi load. Also, the playlist's `BANDWIDTH` gives the
  source bitrate even when transcoding.
- Audio is cut per segment with the video's dts-based `-t`, leaving ~40–80 ms audio gaps at segment
  boundaries (present in the original code too; not audible in testing).
- `explain` / ranking use the static (optimistic) Chromecast capabilities and ignore per-device memory.
- The `explain` label "repackaged as HLS" is shown even when streams are transcoded.
- Embedded subtitles are skipped on the first play from a slow share ("need all of it"), by design.
