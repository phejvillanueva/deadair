# DeadAir

**Remove dead air. Keep the story.**

Automatically remove dead air from talking-head, UGC, VSL, podcast, and interview videos. Runs locally with FFmpeg.

---

## What is DeadAir?

DeadAir automatically removes dead air from talking-head, UGC, VSL, podcast, interview, and other speech-heavy videos. You drop in a video, DeadAir finds the silent stretches by analysing the real audio, shows you exactly what it found, and exports a **new video with those sections cut out of the timeline** - not muted, not frozen, actually gone.

## Why?

Editors waste time manually scrubbing through footage looking for pauses and dead air. DeadAir automates that repetitive part so you can spend your time on the actual edit.

This is not an AI product. There is no API, no cloud, no account, no database, no subscription and no payment. It is a small local utility around FFmpeg.

## Features

- Automatic silence detection (FFmpeg's `silencedetect` filter on your real audio)
- Configurable threshold, minimum silence duration, and padding
- Three presets: Natural, Balanced (default), Aggressive
- Visual timeline: kept footage vs. removed silence
- Actual timeline removal - the exported video is shorter, audio and video stay in sync
- Real progress reporting from FFmpeg (no fake progress bars)
- Local processing - nothing leaves your computer
- FFmpeg powered
- No API key, no cloud, no subscription
- Zero Python dependencies (standard library only)

## Installation (Windows)

You need two things: **Python 3.10+** and **FFmpeg**. `run.bat` checks for both and tells you what's missing.

1. **Python** - install from <https://www.python.org/downloads/windows/>. Tick **"Add python.exe to PATH"** in the installer.
2. **FFmpeg** - easiest is `winget install Gyan.FFmpeg` in Command Prompt (`run.bat` will offer to do this for you). Or download a build from <https://www.gyan.dev/ffmpeg/builds/>, unzip it, and either add its `bin` folder to your PATH or copy `ffmpeg.exe` and `ffprobe.exe` into `DeadAir\ffmpeg\bin\`.
3. Download or clone this repository.
4. Double-click **`run.bat`**.

`run.bat` will create a virtual environment, check requirements, check for FFmpeg/FFprobe, start the app, and open it in your browser (usually <http://127.0.0.1:8765/>). Close the window or press Ctrl+C to stop.

Other platforms work too (macOS/Linux): install Python 3.10+ and FFmpeg, then run `python3 -m app.main`.

## Usage

1. Run `run.bat`.
2. Drop in a video (MP4, MOV, MKV, WebM or AVI) or click **Choose Video**.
3. Click **Detect Silence**.
4. Review the detected sections and the timeline. Adjust the preset or settings and detect again if you like.
5. Click **Export Clean Video**. When it's done you'll see original / final / removed durations and can click **Open File** or **Open Folder**.

Exports are saved to the `output` folder inside the DeadAir directory as `<name>_deadair.mp4`. Existing files are never overwritten (`_deadair_2.mp4`, ...).

### Settings

| Setting | Options | Default | Meaning |
|---|---|---|---|
| Silence threshold | -20, -25, -30, -35, -40 dB | -30 dB | Audio quieter than this counts as silence. Higher (e.g. -20) is more aggressive; lower (e.g. -40) only catches near-total silence. |
| Minimum silence | 0.3, 0.5, 0.7, 1.0, 1.5, 2.0 s | 0.5 s | Pauses shorter than this are left alone. |
| Padding | 0, 0.1, 0.15, 0.25, 0.5 s | 0.15 s | Seconds of each silence that are **kept** on both sides of a cut so it doesn't feel unnaturally tight. |

| Preset | Threshold | Min silence | Padding |
|---|---|---|---|
| Natural | -35 dB | 0.7 s | 0.25 s |
| Balanced (default) | -30 dB | 0.5 s | 0.15 s |
| Aggressive | -25 dB | 0.3 s | 0.10 s |

Padding is not applied at the very start/end of the file, so leading and trailing dead air is trimmed right up to the edge. Note that a short pause only loses `duration - 2 x padding` seconds, and a pause shorter than `2 x padding` is left completely alone.

## How it works

**Detection.** DeadAir runs `ffmpeg -i input -map 0:a:0 -vn -af silencedetect=noise=<threshold>dB:d=<min> -f null -` and parses the `silence_start` / `silence_end` timestamps. Overlapping intervals are merged, padding is applied, and the inverse "keep" ranges are computed:

```
speech 0-5 | silence 5-7 | speech 7-12 | silence 12-13.5 | speech 13.5-20
keep:  0-5  ->  7-12  ->  13.5-20          = 16.5 s
```

**Export.** Stream-copy can only cut on keyframes, so DeadAir re-encodes for accurate cuts. It builds one FFmpeg filter graph that `trim`s the video and `atrim`s the audio for every kept segment, resets timestamps, and joins everything with the `concat` filter. Cut points are snapped to the video's frame grid (for constant-frame-rate sources) so video and audio segments have identical lengths and no A/V drift builds up across many cuts. The graph is passed via a script file, so hundreds of cuts don't hit the Windows command-line length limit.

Output: MP4, H.264 (CRF 18, `yuv420p`) + AAC 192 kb/s, same resolution, aspect ratio, and frame rate as the source. Phone footage with a rotation tag comes out correctly rotated. If nothing was detected, DeadAir tells you no changes are needed instead of re-encoding. If the *entire* video is silent it refuses to export rather than create a broken file.

Only the first video stream and first audio stream are exported; other tracks (extra audio languages, subtitles, data) are not carried over. 10-bit/HDR sources are converted to 8-bit `yuv420p`.

## Privacy

Videos are processed locally and are not uploaded to a server. The web page talks only to a server running on your own computer (`127.0.0.1`). The browser hands the file to that local server, which keeps a temporary copy in your system temp folder while you work; it is deleted when you load another video or quit DeadAir.

## Project layout

```
DeadAir/
  app/
    main.py        local HTTP server + API
    silence.py     silence parsing, padding, keep/remove maths (pure functions)
    video.py       FFprobe/FFmpeg: probe, detect, export
    utils.py       filename sanitising, binary discovery, formatting
    templates/     index.html
    static/        app.css, app.js, favicon.svg
  tests/           unit, FFmpeg and API tests
  requirements.txt
  run.bat
```

DeadAir deliberately uses Python's standard-library HTTP server instead of FastAPI: the app has a handful of endpoints, so this keeps setup to "Python + FFmpeg" with nothing to `pip install`.

## Running the tests

```
python -m unittest discover -s tests -v
```

The suite generates small synthetic videos with FFmpeg (tone "speech" with known silent ranges, plus an optional -50 dB room-tone floor), then checks detection against the known ranges, exports, verifies the result with FFprobe, and checks A/V sync frame by frame. It also starts a real DeadAir server and drives the API. FFmpeg and FFprobe must be installed to run the tests. The suite takes about two minutes.

## Known limitations

- Re-encoding takes real time. Correctness is prioritised over speed.
- Very long files with hundreds of cuts use more memory during export.
- Variable-frame-rate sources are exported correctly, but cut points aren't frame-snapped for them, so expect small (sub-frame) differences between planned and actual length.
- The browser copies the file to a temp folder on upload, so you need free disk space roughly equal to the video's size.
- Windows is the target platform; the `run.bat` launcher and Open File/Folder buttons were written for Windows.

## Roadmap

Potential future features (not implemented in V1):

- V0.2 manual exclude/include cuts
- V0.3 filler-word detection
- V0.4 automatic jump-cut editing
- V0.5 caption generation
- V0.6 editor presets
- V0.7 Premiere/CapCut/DaVinci workflow integration

## License

Add the license of your choice before publishing (MIT is a common choice for tools like this).
