<p align="center">
  <img src="docs/banner.jpg" alt="At night a developer with a mug looks at 'noggin-compass report' on a laptop: LEFT 68%, RIGHT 23%, STRAIGHT 9%, while a camera on the wall watches a baby asleep in the crib" width="820">
</p>

<h1 align="center">noggin-compass</h1>

<p align="center"><b>Which way does your baby's head point while sleeping, and for how long?</b></p>

<p align="center">
  A small, fully local tool that watches a sleeping baby through an overhead Wi-Fi camera<br>
  and logs how much time the head spends turned <b>left</b>, <b>right</b> or <b>straight</b>.
</p>

<p align="center"><b>English</b> | <a href="README.ru.md">Русский</a></p>

---

## Why

Many babies develop a favourite side to sleep on. If the head almost always rests on the same
spot, that spot can flatten over time (positional plagiocephaly), and a strong preference can
also be a sign of a tight neck muscle (torticollis). Paediatricians usually suggest alternating
the head position, but at 3 a.m. nobody knows how the night actually went.

**noggin-compass** answers that with numbers: *"last night: left 5 h 10 min (68%), right
1 h 45 min (23%), straight 40 min"*, plus a minute-by-minute timeline, so you can see whether
repositioning helps.

> [!IMPORTANT]
> This is a hobby project, **not a medical device and not a baby monitor**. It does not watch
> breathing and raises no safety alarms. If your baby strongly prefers one side, talk to your
> paediatrician.

## What it sees

<p align="center">
  <img src="docs/what-it-sees.jpg" alt="Overhead view of a sleeping baby with the detected keypoints, head axis, the verdict 'left', and lines in the corner: asleep for 3 h, two thermometer readings" width="820">
</p>

The debug view (`--preview`) shows everything the program bases its decision on: the detected
keypoints with their confidence (yellow ones are used, grey ones are too uncertain), the *crown*
and *baby's left* arrows, and a zoomed-in head. In the bottom left corner: whether the baby is
asleep, and the thermometer readings. `/marked` in Telegram and `noggin-compass draw` show the same
picture.

## How it works

```mermaid
flowchart TD
    cam["Wi-Fi camera<br/>RTSP, 1 frame per second"] --> crop["Crop to the crib"]
    subgraph nn ["YOLO pose, two passes"]
        pass1["Pass 1: find the baby<br/>and the shoulder line"] --> rotate["Rotate the crop<br/>so the head points up"]
        rotate --> pass2["Pass 2: nose, eyes, ears"]
        memory["Or the crop of the baby<br/>found in the last minute"] --> pass2
    end
    crop --> pass1
    pass2 --> geometry["Where is the face between the ears?<br/>score from -1 to +1"]
    geometry --> smooth["A new position counts<br/>after 10 seconds"]
    smooth --> db[("SQLite")]
    db --> report["Nightly report"]
```

* **Keypoints.** An off-the-shelf [Ultralytics YOLO pose](https://docs.ultralytics.com/tasks/pose/)
  model finds 17 body keypoints; we use the nose, eyes, ears and shoulders.
* **Two passes.** Pose models are trained on people who stand or sit. On a baby lying sideways in
  the frame they confidently misplace facial points, ears especially. So the first pass only
  finds the baby and the shoulder line, then the crop is rotated so the head points up and the
  model runs again. On our recording of a sleeping baby with the head in profile this took the
  correct answers from 6 of 28 frames to 27 of 28.
* **Memory.** Seen from above in a white sleep sack, the baby is missed by the first pass on many
  frames even when nothing has changed. So the crop of the last baby found is tried again for a
  minute, and the second pass the model is more sure of wins. If the second pass does not see the
  shoulders, the body axis from the first pass is used. On an evening recording this cut frames
  without an answer from 77 of 229 to 18, and the left/right flips on a baby lying still almost
  disappeared. When the fresh find and the remembered one do not touch, for example a rolled
  bumper next to the head was found instead of the baby, the larger one wins: in a crib that is
  the baby.
* **Is the baby in the crib at all?** To find a baby in a sleep sack, the pose model's person
  threshold has to be low, and then it also finds "people" in an empty crib: in the sheet print, in
  a toy with a face, in clothes laid out, in an empty sleep sack. So a second network answers this
  question: [SigLIP 2](https://huggingface.co/google/siglip2-base-patch16-224) compares the whole
  picture with short descriptions such as "a baby in a crib" and "an empty crib" and needs no
  training. The pose model then only says where the head is turned, and when it loses a baby who
  is in the crib (a white sleep sack at night), the frame is "not visible" rather than "not in
  frame". On a day and a half of snapshots this cut the minutes of an empty crib that looked
  occupied from 64 to 6 and the minutes the baby went missing from 29 to 8, with no made-up sleep.
  See `presence` in [Configuration](#configuration).
* **Geometry, not magic.** Think of the head as a ball. When it turns, the face moves sideways
  while the ears move towards the middle, so the face position between the ears tells the angle.
  The score is 0 for straight, about 0.35 at 20 degrees and 1 for a full profile; positive means
  the baby's left, negative the right. "Sideways" is measured across the shoulders-to-head line,
  so it does not matter how the baby lies in the crib. Left and right are always **the baby's
  own** left and right.
* **Retroactive tuning.** The raw score of every frame is stored, so changing the threshold or the
  smoothing later re-computes the old nights too.

## Privacy

Everything runs on your own computer:

* the camera stream is read over RTSP inside your home network;
* video is **not recorded**: only numbers go to the database (plus optional snapshots for
  checking accuracy, off by default);
* the camera password stays in an environment variable, your local config or the macOS
  Keychain and never shows up in logs;
* after the model weights are downloaded once, both models are loaded from `models/` without
  going online, and the Ultralytics and Hugging Face telemetry is switched off;
* nothing stops you from blocking the camera's internet access in your router, RTSP keeps
  working locally;
* Telegram is optional: if you turn it on, alert texts and statistics go through Telegram.
  Pictures only go there if you also allow `/photo` with `telegram.photos: true`.
* thermometer readings stay on your computer too. The thermometers themselves give their readings
  to anyone within Bluetooth range who connects, that is how they are made.

## Hardware

Any Wi-Fi camera with **RTSP** will do. Developed with a **TP-Link Tapo C225** (starlight sensor,
f/1.6 lens, invisible 940 nm infrared, a privacy mode that hides the lens).

Mount the camera **above the crib looking down**: on the ceiling, or high on the wall next to the
crib. Keep the camera and its cable well out of the baby's reach.

## Getting started

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). Developed and tested on macOS.

```bash
uv sync
```

That is all the installation. The pose model (about 20 MB) and the presence model (about 1.5 GB)
are downloaded automatically into `models/` on the first run; after that the program works without
internet. It runs on the CPU by
default, see [CPU or GPU](#cpu-or-gpu) for the Apple or NVIDIA GPU.

The interface is in English; for Russian add `--lang ru` to any command or put `language: ru`
in the config.

**Try it on yourself first.** An overhead camera sees a baby lying on its back the same way a
webcam sees a person sitting in front of it, so the built-in webcam is enough:

```bash
uv run noggin-compass run --preview   # turn your head to your left shoulder: LEFT
uv run noggin-compass report
```

**Connect the camera** (Tapo example):

1. In the Tapo app: *Device settings, Advanced settings, Camera account*. Create a login and a
   password (better without special characters).
2. Copy the example config and put the stream address in, with `{password}` in place of the
   password:
   ```bash
   cp config.example.yaml config.yaml
   ```
   ```yaml
   camera:
     source: rtsp://<login>:{password}@192.168.1.50:554/stream1
     roi: [0.15, 0.25, 0.84, 0.88]   # the crib area, as fractions of the frame
   detector:
     imgsz: 1280                     # the head is small in an overhead view
   ```
3. Give the program the password in any of three ways; it looks for it in this order:
   * a `.env` file next to `config.yaml` (or a real environment variable):
     ```bash
     NOGGIN_CAMERA_PASSWORD=your-camera-password
     ```
   * `camera.password` in `config.yaml`;
   * the macOS Keychain (the command asks for the password and does not echo it):
     ```bash
     security add-generic-password -s noggin-compass-camera -a <login> -w
     ```

   `config.yaml` and `.env` are git-ignored.
4. Run it for the night (`caffeinate` keeps the Mac awake):
   ```bash
   caffeinate -i uv run noggin-compass run
   ```

## Command line

```bash
uv run noggin-compass [--config FILE] [--lang en|ru] run [options]
uv run noggin-compass [--config FILE] [--lang en|ru] report [options]
uv run noggin-compass [--config FILE] [--lang en|ru] draw SNAPSHOT... [-o DIR]
uv run noggin-compass [--config FILE] [--lang en|ru] thermometer [--seconds N]
```

| Option | What it does |
|---|---|
| `-c`, `--config FILE` | Config file to use. Default: `config.yaml` in the current folder; without it the built-in defaults are used. |
| `--lang en\|ru` | Interface language for this run, overrides `language` from the config. |
| **`run`** | Watch the camera (or a video file) and record the head position every second. |
| `--source SOURCE` | Use another video source just this once: an RTSP address, `0` for the webcam or a video file. |
| `--preview` | Open a window with the frame and everything that was detected. Close it with `q`. |
| `--model MODEL` | Use another model just this once, see [Models](#models). |
| `--device DEVICE` | Run the network on `cpu`, `mps` (Apple Silicon GPU) or `0` (first NVIDIA GPU) just this once. |
| `--start TIME` | For a video file: when the recording started, e.g. `2026-11-04T21:30`, so the report gets real times. |
| **`report`** | Print time per side for each day plus a minute-by-minute timeline, then sleep per day. |
| `--days N` | How many recent days to show. Default: 7. |
| `--date DATE` | Show one particular day, e.g. `2026-11-04`. |
| `--timelines N` | For how many of the last days to print details and the timeline. Default: 1. |
| `--chart FILE` | Also save a chart of the last day as a PNG: time per side, a timeline of the head and sleep, left versus right per day. |
| **`draw`** | Draw what was detected on saved snapshots (see `storage.snapshot_every_seconds`), as in the preview window. |
| `-o`, `--out DIR` | Where to put the drawn copies. Default: next to each snapshot, as `<name>_drawn.jpg`. |
| **`thermometer`** | Find the thermometers nearby, show their readings and choose which ones to read, see [Thermometer](#thermometer). |
| `--seconds N` | How long to look for thermometers. Default: 30. |

A report day runs from noon to noon, so a whole night lands in one day. In the timeline every row
is an hour and every character a minute: `L` left, `R` right, `·` straight, `?` in the crib but the face is not
visible, `_` nobody in the crib. The raw score of every frame is stored, so after changing the threshold
or the smoothing the report re-computes old nights too.

## Turn alerts and Telegram

The program can tell you when it is time to turn the baby. Two rules, both off by default:

* **Too long on one side** (`alerts.max_same_side_minutes`): the head has stayed on one side this
  long. A turn to the other side shorter than `alerts.min_turn_minutes` does not count, it does not
  take the pressure off.
* **Imbalance** (`alerts.max_imbalance_minutes`): over the last `alerts.window_hours` one side is
  ahead of the other by this much, and the head is on that side again. This catches patterns like
  "2 h right, half an hour left, 2 h right again", where no single stint is long but the balance is
  off.

```yaml
alerts:
  max_same_side_minutes: 180
  max_imbalance_minutes: 180
```

An alert says which side to turn the head to. It plays a sound and shows a notification on the
computer running the program (macOS) and is repeated every `alerts.repeat_minutes` while the head
stays on that side. The numbers above are an example; agree on the limits with your paediatrician.

**Telegram.** Alerts can also go to Telegram, and the bot answers `/now` (where the head is, whether
it is time to turn and whether the baby is asleep), and over the last 24 hours `/stats` (time per
side), `/sleep` (sleep, see [Sleep](#sleep)) and `/chart` (a chart like `report --chart`). With
`/nap` and `/naps`, parents log sleep the camera cannot see, see [Sleep](#sleep):

1. Create a bot with [@BotFather](https://t.me/BotFather) and store its token in the macOS Keychain
   (the command asks for the token and does not echo it):
   ```bash
   security add-generic-password -s noggin-compass-telegram -a bot -w
   ```
   Or put it into `.env` as `NOGGIN_TELEGRAM_TOKEN=123456:ABC...`.
2. Start the program and send `/start` to your bot. The bot ignores unknown chats and prints their
   id in the log: `ignored a message from chat 123456789`.
3. Allow that chat in `.env` (several ids are separated with commas) and restart the program:
   ```bash
   NOGGIN_TELEGRAM_CHAT_IDS=123456789
   ```

With `telegram.photos: true` the bot also sends a picture from the camera right now: `/photo` as
it is, `/marked` with the detected points, as in the preview. This is off by default, since the
pictures go through Telegram.

The token and chat ids can also go into `config.yaml` (`telegram.token`, `telegram.chat_ids`). The
token is looked up in this order: `NOGGIN_TELEGRAM_TOKEN`, `telegram.token`, the Keychain.

## Sleep

The program also counts how long the baby sleeps in the crib. No extra model is needed: every
second it measures how much of the baby's area changed since the previous frame (the overall
brightness change from a night light or the auto exposure is removed first). A sleeping baby
barely moves, an awake one turns, kicks and waves its arms. This is how sleep actigraphy works.

* Every minute in the crib is *still* or *restless*: restless when at least `sleep.restless_share`
  of its frames had movement.
* The baby is **asleep** after `sleep.fall_asleep_minutes` still minutes within
  `sleep.fall_asleep_window_minutes`, counted from the first of them. Not "in a row": falling
  asleep, a baby often waves its arms with eyes already closed.
* The baby is **awake** after `sleep.wake_minutes` restless minutes within
  `sleep.wake_window_minutes`, or as soon as it is taken out of the crib. Twitches and head turns
  during sleep do not count, even when they go on for a few minutes in a row.

`noggin-compass report` shows sleep per day and the list of sleeps, `/sleep` in Telegram the same
for the last 24 hours, `/now` and the preview whether the baby is asleep right now. The raw movement of every frame
is stored, so changed thresholds apply to old nights too.

```
Sleep (days start at 12:00):
04.11 (Wed)  11 h 20 min, sleeps: 4, longest: 4 h 05 min

Sleep on 04.11.2026:
  13:10-14:35  1 h 25 min
  16:00-17:30  1 h 30 min (outside the crib)
  ...
```

**Sleep outside the crib.** The camera does not see naps in arms or in a pram, so parents log them
in Telegram. `/nap` offers buttons with a length, from 15 minutes to 3 hours. The sleep is taken to
have ended when the button is pressed, so press it right after the baby wakes up. `/naps` lists
the latest entries, and each can be changed (a new length, the wake-up time stays) or deleted.
Logged sleep counts in `/sleep`, `/stats`, `/chart` and `report`, marked "outside the crib". A nap
that overlaps sleep the camera saw, or one logged by both parents, is counted once.

Lying still with open eyes also counts as sleep. For a rough daily total this is enough; telling
open eyes from closed would need a face model.

## Thermometer

The program can also show the temperature and humidity next to the baby, in `/now` and on the
preview. It reads Xiaomi Mijia **LYWSD03MMC** thermometers (the small square ones) over
Bluetooth, on their stock firmware: no Mi Home account, hub or key is needed.

```bash
uv run noggin-compass thermometer
```

looks for thermometers nearby for 30 seconds and lists each one with its reading:

```
Found nearby: 2
  1. 24.7 °C, humidity 45%, signal good, battery 2.88 V  (id 3C5D)
  2. 24.0 °C, humidity 50%, signal good, battery 2.83 V  (id 6A18)
To tell them apart: hold one in your hand for a minute and look again, its temperature goes up.
Which ones to read? Numbers separated by spaces, Enter to keep the current choice, 0 for none:
```

Enter the number of the one in the crib, or several numbers: then the program asks for a name for
each, and the readings are shown by name. The choice goes into the `thermometers` section of
`config.yaml`, the rest of the file stays as it was. Restart `noggin-compass run` to start reading.

* **How it reads.** The thermometer broadcasts its readings encrypted with a key from the Xiaomi
  account, but to a client that connects it sends them in the open. So every
  `thermometers.every_minutes` the program connects for about 10 seconds, takes one reading and
  disconnects. Staying connected would drain the coin cell, and the phone app could not connect
  meanwhile. All chosen thermometers are looked for in one search and each is read as soon as it
  is seen, so one with a weak signal does not hold up the others. The menu saves the readings it
  takes, so they show up right after the restart.
* **What you see.** `/now` ends with a line such as "Temperature 24.0 °C, humidity 50%, battery
  71%.", with several thermometers one line per name. The battery percentage is a rough guess from
  the voltage: a coin cell holds about 2.9 V most of its life and then drops fast. A reading older
  than two polls gets its age. The preview shows the same lines without the battery. Each reading
  also goes to the log and to the database (the `climate` table).
* **Bluetooth on a Mac.** The first time, macOS asks whether the terminal may use Bluetooth: allow
  it. A program started without that permission is stopped by macOS, so start it from a terminal.
  The computer has to be within Bluetooth range of each thermometer, about 10 m through a wall.
* **The address.** On a Mac a Bluetooth device has no fixed MAC address: each Mac gives it its own
  identifier. So take the address from `noggin-compass thermometer` rather than from the sticker,
  and choose again on another computer.
* If a thermometer does not answer (out of range, busy with the phone app), the log says so once
  and the program tries again at the next poll. The camera goes on either way.

### Cold alarm

For a window or a door left open at night. When a thermometer shows less than
`thermometers.limits.cold_below` (18 °C):

1. A quiet beep on the computer running the program once a minute, and one Telegram message:
   "Crib: 17.8 °C, colder than 18 °C. Check the windows and the door. If it is not warmer in 10 min,
   the alarm goes off." From then on the thermometers are read every 2 minutes.
2. If after 10 minutes it is not warmer, the alarm: a loud sound with no pause, a notification on
   the computer and a Telegram message with a **Turn off** button every 2 minutes. If the
   temperature falls by another degree before the 10 minutes are up, the alarm starts at once.
   If it is getting warmer by itself, the quiet beeping goes on for another 10 minutes instead.
3. **Turn off** silences the alarm for 20 minutes. If by then it is not warmer, the alarm comes back.
4. Once it is above `cold_below` again, it all ends, and Telegram says so without a sound.

`/now` says what the alarm is doing. The sounds play on the computer, so keep its volume up. The
times and the sounds are in `thermometers.cold`.

### Tips, a lost thermometer, a low battery

Telegram also says, without a sound:

* when it gets warmer than `hot_above` (22 °C), drier than `dry_below` (40%) or damper than
  `humid_above` (60%), with what to do about it. A tip comes when the trouble begins, and the same
  one no sooner than `thermometers.tips_every_hours` later. While it lasts, `/now` shows it too;
* when a thermometer has not answered for `thermometers.lost_minutes`, and when it answers again;
* when its battery drops below `thermometers.low_battery_volts`, once a day. It takes a CR2032.
  Users report 8-12 months from one on these thermometers; readings every 10 minutes cost some of
  that, so keep a spare.

### Limits for each thermometer

The defaults in `thermometers.limits` follow common pediatric advice for a baby's sleep: 18-22 °C
and 40-60% humidity. A thermometer can have its own, by the same names, next to its address in
`config.yaml`:

```yaml
thermometers:
  devices:
    - address: "5E1A7C30-9B2D-4F86-A1C4-3D7E9F2B6A18"
      name: "Crib"
      cold_below: 20       # the alarm already below 20 °C
    - address: "C9047B2E-61D3-4A5F-8E2B-07F4D1A93C5D"
      name: "Hall"
      alarm: false         # cold here gives a tip, without the alarm
```

The menu keeps them when the choice is changed. Restart `noggin-compass run` after editing.

## Configuration

All options with their defaults are in [`config.example.yaml`](config.example.yaml); copy it to
`config.yaml` and keep only what you change.

| Option | Default | What it does |
|---|---|---|
| `language` | `en` | Language of the messages, the report and the preview: `en` or `ru`. |
| **camera** | | |
| `camera.source` | `0` | Where the video comes from: an RTSP address, `0` for the built-in webcam, or a video file. |
| `camera.password` | not set | Camera password, put in place of `{password}` in the address. The `NOGGIN_CAMERA_PASSWORD` environment variable takes precedence; if neither is set, the macOS Keychain entry `noggin-compass-camera` is used. |
| `camera.sample_fps` | `1` | How many frames per second are analysed. The head does not turn often, one is plenty. |
| `camera.rotate` | `0` | Rotates the picture by 90, 180 or 270 degrees before the analysis. |
| `camera.roi` | whole frame | The part of the frame with the crib, as fractions `[left, top, right, bottom]`. Everything outside is ignored, so a parent walking by is not taken for the baby. |
| **detector** | | |
| `detector.model` | `models/yolo11s-pose.pt` | Which neural network finds the keypoints, see [Models](#models). |
| `detector.device` | `cpu` | Where it runs: `cpu`, `mps` (Apple Silicon GPU) or `0` (first NVIDIA GPU), see [CPU or GPU](#cpu-or-gpu). |
| `detector.imgsz` | `640` | How detailed a picture the network gets. In an overhead view the head is small, and 1280 finds the face points much more reliably, at the cost of speed. |
| `detector.upright` | `true` | The second pass on a crop with the baby turned head-up. Switch it off only to save CPU. |
| `detector.min_person_conf` | `0.35` | How sure the pose network must be that it sees a person at all. Lower it if the baby under a blanket is often "not visible". |
| `detector.min_person_area` | `0.06` | A "person" smaller than this share of the frame is ignored. With the frame cropped to the crib (`camera.roi`), a baby takes 10% of it or more, while the toys, the rail and the sheet print that the network took for people in an empty crib took 1-6% on our camera. Lower it if the baby is small in the frame (a camera mounted high, no `camera.roi`) and often comes out "not visible" while in the crib. With `presence.enabled: false`, raise it if the preview still finds people in an empty crib; with the presence check on, an empty crib that comes out occupied is a matter of `presence.threshold`. The real sizes on your camera are in the `box` field of `analysis.jsonl` next to the snapshots, in pixels of the cropped frame. |
| `detector.min_keypoint_conf` | `0.4` | Keypoints with lower confidence are ignored. Raising it makes the program more cautious: more "not visible", fewer mistakes. |
| `presence.enabled` | `true` | A second network decides whether the baby is in the crib at all (see How it works). Without it the pose model decides, and an empty crib with a patterned sheet, toys or clothes can show up as "not visible", head turns or even sleep. If the model cannot be loaded (no internet for the first download, too little memory) or fails while running, the program goes on with the pose model alone, as with `false`, and says so in the log: after a failed load until the next restart, after a failure while running until the next check works. |
| `presence.model` | `google/siglip2-base-patch16-224` | A fixed-resolution SigLIP or SigLIP 2 model from Hugging Face (the NaFlex ones do not work). The default is about 1.5 GB and takes 40-60 ms per check on an Apple M4 Pro CPU. |
| `presence.threshold` | `0.35` | How sure the network must be (0..1) that the baby is in the crib. On our camera a baby scored 0.6-1.0 and an empty crib 0-0.13, whatever lay in it, so the exact value matters little. Lower it if a baby under a blanket comes out "not in frame", raise it if an empty crib comes out occupied. The score of each snapshot is in the `presence` field of `analysis.jsonl`. Unlike the head thresholds, a new value only applies from then on. |
| `presence.every_seconds` | `5` | How often to check. In between the last answer holds, so putting the baby in or taking it out shows up within this many seconds. |
| **head** | | |
| `head.center_threshold` | `0.35` | How far the head has to turn before it counts as left or right instead of straight. 0.35 is about 20 degrees. Raise it if slight turns are counted as left or right, lower it to catch them. |
| `head.mirrored` | `false` | Set to `true` if the camera shows a mirrored picture, otherwise left and right get swapped. |
| `head.fallback_head_direction` | `up` | Where the top of the head points in the frame (`up`, `down`, `left`, `right`) when the shoulders are not visible. Normally the shoulders are found and this is not used. |
| **tracking** | | |
| `tracking.confirm_seconds` | `10` | How long a new head position has to hold before it counts. Filters out single-frame mistakes and brief twitches. |
| `tracking.gap_seconds` | `60` | If no frames arrive for longer (camera or Wi-Fi dropped out), that time is not counted for any side. |
| `tracking.day_starts_at` | `12:00` | When a report day begins. Noon keeps a whole night in one day. |
| **alerts** | | |
| `alerts.max_same_side_minutes` | `0` (off) | Alert when the head has stayed on one side this many minutes, see [Turn alerts](#turn-alerts-and-telegram). |
| `alerts.max_imbalance_minutes` | `0` (off) | Alert when one side is ahead of the other by this many minutes over `alerts.window_hours` and the head is on that side again. |
| `alerts.window_hours` | `12` | How far back the balance between the sides is counted. |
| `alerts.min_turn_minutes` | `10` | A turn to the other side shorter than this does not interrupt a stint on the first side. |
| `alerts.repeat_minutes` | `15` | How often the alert repeats while the head stays on that side. |
| `alerts.sound` | `true` | Play a sound with the alert (macOS). |
| `alerts.sound_file` | `Funk` | Which sound: the name of a macOS system sound (Funk, Glass, Sosumi, Basso...) or a path to a sound file. |
| **telegram** | | |
| `telegram.token` | not set | Bot token from @BotFather. The `NOGGIN_TELEGRAM_TOKEN` environment variable takes precedence; if neither is set, the macOS Keychain entry `noggin-compass-telegram` is used. |
| `telegram.chat_ids` | `[]` | Chats that get alerts and may use the bot commands. The `NOGGIN_TELEGRAM_CHAT_IDS` environment variable (comma-separated) takes precedence. |
| `telegram.photos` | `false` | Allow `/photo` and `/marked`: a picture from the camera, plain or with the detected points. Off by default because the pictures go through Telegram. |
| **sleep** | | |
| `sleep.frame_motion` | `0.02` | A frame counts as movement when this share of the baby's area changed since the previous frame. |
| `sleep.restless_share` | `0.3` | A minute is restless when at least this share of its frames had movement. |
| `sleep.fall_asleep_minutes` | `8` | Asleep after this many still minutes... |
| `sleep.fall_asleep_window_minutes` | `10` | ...within this many minutes; the sleep starts at the first still one. Set both to the same number to require still minutes in a row. |
| `sleep.wake_minutes` | `7` | Awake after this many restless minutes... |
| `sleep.wake_window_minutes` | `10` | ...within this many minutes. |
| `sleep.out_minutes` | `1` | The baby counts as taken out of the crib only after this many minutes in a row of "not in frame". With the presence check on, a baby the pose network loses stays "not visible" and the sleep goes on, so there is no reason to change it. With `presence.enabled: false` the network sometimes loses a still baby in a white sleep sack for a minute or two in daylight, and the sleep ends; 2-3 keeps such a sleep, but if the network finds "people" in an empty crib (a patterned sheet, an empty sleep sack), it can count sleep that never happened. |
| **storage** | | |
| `storage.db_path` | `data/noggin-compass.db` | SQLite file with the per-second results. |
| `storage.snapshots_dir` | `data/snapshots` | Where snapshots are saved. |
| `storage.snapshot_every_seconds` | `0` (off) | Every N seconds saves the frame and a line in `analysis.jsonl` with all keypoints and the score. Use it to check the accuracy by eye, to recompute statistics later without running the network again, or to train a model of your own. To see what was detected on a snapshot, `uv run noggin-compass draw data/snapshots/2026-11-05/0812*.jpg` draws the points on a copy, as in the preview window. A frame takes about 500 KB, so one every 10 s is about 4 GB a day. |
| `thermometers.devices` | `[]` | The thermometers to read, each with an `address` and an optional `name`. Easiest to fill in with `noggin-compass thermometer`, see [Thermometer](#thermometer). The name is shown only when there are several. |
| `thermometers.every_minutes` | `10` | How often to read the thermometers. Each reading connects for about 10 s, which costs the coin cell more than the thermometer's own work, so do not go much lower: the room temperature changes slowly anyway. |
| `thermometers.scan_seconds` | `90` | How long to look for a thermometer before each reading. It advertises every few seconds, and with a weak signal many are lost: one of ours took 47 s to be seen. The search ends as soon as it is seen, so a longer value costs nothing. Raise it if the log often says a thermometer was not seen. |
| `thermometers.low_battery_volts` | `2.5` | Below this voltage `/now` and Telegram say the battery runs low. A new CR2032 gives about 3.0 V, and the thermometer stops working at about 2.2 V. |
| `thermometers.lost_minutes` | `30` | Telegram says a thermometer is lost when it has not answered this long, and says it again when it answers. |
| `thermometers.tips_every_hours` | `3` | A tip about the heat or the humidity comes when the trouble begins, without a sound, and the same one no sooner than this: around a limit it would otherwise come and go every poll. |
| `thermometers.limits.cold_below` | `18` | Below this (°C) the [cold alarm](#cold-alarm) starts. 18 °C is the bottom of the range usually given for a baby's room, and the WHO's minimum for a home. Each thermometer can have its own, see [Limits for each thermometer](#limits-for-each-thermometer), as can the other limits. |
| `thermometers.limits.hot_above` | `22` | Above this (°C), a tip to dress the baby lighter or turn the heating down. |
| `thermometers.limits.dry_below` | `40` | Below this humidity (%), a tip to turn on a humidifier. |
| `thermometers.limits.humid_above` | `60` | Above this humidity (%), a tip to turn on a dehumidifier. |
| `thermometers.limits.alarm` | `true` | Whether cold starts the alarm. `false`: a tip, as for the heat. |
| `thermometers.cold.check_minutes` | `10` | How long the quiet beeping goes on before the alarm, if it does not get warmer. |
| `thermometers.cold.repeat_minutes` | `2` | How often the alarm message is repeated in Telegram until **Turn off**. |
| `thermometers.cold.snooze_minutes` | `20` | How long **Turn off** silences the alarm. |
| `thermometers.cold.warming_degrees` | `0.3` | How much the temperature has to rise to count as getting warmer, and to rise above `cold_below` to end the alarm. The readings wobble by about 0.1 °C: lower, and the alarm may end on that wobble. |
| `thermometers.cold.colder_degrees` | `1.0` | Falling this much below where the cold began starts the alarm at once, without waiting `check_minutes`. |
| `thermometers.cold.every_minutes` | `2` | How often to read the thermometers while it is colder than `cold_below` + `close_degrees`, so the alarm follows the temperature. Costs battery, but only on cold nights. |
| `thermometers.cold.close_degrees` | `1.0` | See `every_minutes` above. |
| `thermometers.cold.quiet_sound`, `quiet_volume` | `Tink`, `0.3` | The quiet beep: a macOS system sound (Tink, Ping, Sosumi, Funk...) or a sound file, and its volume 0..1. |
| `thermometers.cold.alarm_sound`, `alarm_volume` | `Sosumi`, `1.0` | The alarm sound and its volume. |

## Models

The keypoints come from an [Ultralytics](https://docs.ultralytics.com/tasks/pose/) pose model.
The default is **YOLO11s-pose** (`models/yolo11s-pose.pt`, about 20 MB): small enough to be fast
on a laptop CPU, accurate enough for a baby's face.

Any Ultralytics pose model works. Set it in the config (`detector.model`) or for one run with
`--model`. Models live in the `models/` folder (git-ignored). If the file is not there yet, it is
downloaded automatically on the next run, so switching to another model is just:

```bash
uv run noggin-compass run --preview --model yolo26s-pose.pt
```

The names follow `yolo11{n,s,m,l,x}-pose.pt` and `yolo26{n,s,m,l,x}-pose.pt`: `n` is the fastest
and least accurate, `x` the slowest and most accurate on standard benchmarks. A baby lying across
the frame is not a standard benchmark, though. On 28 hand-labelled frames from our camera:

| Model | Correct | Time per frame (Apple GPU) |
|---|---|---|
| **yolo11s-pose** (default) | **27 of 28** | 59 ms |
| yolo26s-pose | 14 of 28 | 59 ms |
| yolo26l-pose | 16 of 28 | 138 ms |

The newer generation often missed clear head turns or did not find the baby at all, so the
default stays `yolo11s`. You can also point `detector.model` at your own fine-tuned file.

## CPU or GPU

By default the network runs on the **CPU**, which is plenty: the program looks at one frame per
second. Measured on an Apple M4 Pro, two passes at `imgsz: 1280`:

| | CPU | Apple GPU (`mps`) |
|---|---|---|
| yolo11s-pose | 182 ms per frame | 60 ms per frame |
| yolo26s-pose | 215 ms per frame | 67 ms per frame |

The results are identical (keypoints differ by less than a pixel), so `device: mps` on Apple Silicon
simply leaves more CPU free. Trying the remembered crop (see How it works) adds one more pass, about
half as much time again. With an NVIDIA card use `device: 0` (needs a CUDA build of PyTorch).
Nothing else has to be set up.

## Limitations

* A face covered by a hand or a blanket is reported as "not visible" rather than guessed.
* If an adult leans over the crib, the largest person in the crib area is analysed.
* Sleep is told from movement only: lying still with open eyes counts as sleep. Sleep outside the
  crib is only counted when the parents log it in Telegram.
* The presence check has been tried on a day and a half of one crib and one camera, not yet with a
  baby under infrared light.

## Development

```bash
uv run pytest
```

## License

noggin-compass is free software under the
[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0), © 2026 Alexander Kirillov.

It is built on [Ultralytics](https://github.com/ultralytics/ultralytics) YOLO, which together
with its pretrained pose models is licensed under AGPL-3.0, so the whole program follows the same
terms. You may use, change and share it. If you pass it on, or let other people use your changed
version over a network, you have to offer them the source code under AGPL-3.0 as well. The
library is installed and the model is downloaded on your machine; neither is part of this
repository. See also the [Ultralytics licensing terms](https://www.ultralytics.com/license).

The presence check uses Google's SigLIP 2 model and Hugging Face Transformers, both under
Apache-2.0. The thermometers are read with [Bleak](https://github.com/hbldh/bleak), under the MIT
license.
