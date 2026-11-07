"""Command line: `noggin-compass run` watches the camera, `noggin-compass report` shows a report."""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from .chart import day_label, render_chart
from .config import Config, DetectorConfig, language_of, load_config, load_dotenv
from .head import HeadEstimate, Side, estimate_head, side_label
from .i18n import LANGUAGES, set_language, t
from .motion import MotionMeter
from .naps import periods as nap_periods
from .notify import Notifier, check_sounds, load_history, monitor_from_config
from .pose import Person, PoseDetector
from .preview import Frame, corner_notes, fit, picture
from .report import build_report, day_bounds, day_of, fmt_duration, parse_day_start, render
from .sleep import SLEEP_LOOKBACK, render_sleep, sleep_days, sleep_line
from .source import is_file_source, open_source, preprocess
from .storage import Storage
from .thermometer import ThermometerPoller, menu
from .tracker import SideTracker

if TYPE_CHECKING:
    from .presence import PresenceCheck

log = logging.getLogger("noggin_compass")

STATUS_EVERY_SECONDS = 60
SLEEP_EVERY_SECONDS = 60  # how often to work out whether the baby is asleep, for /now and the picture
# A frame that fails is logged with its traceback at most this often, so a broken night does not
# fill the log with the same one.
FAILURE_LOG_SECONDS = 60
PREVIEW_WINDOW = "noggin-compass"


def analyze(
    detector: PoseDetector,
    frame: np.ndarray,
    cfg: Config,
    now: float | None = None,
    presence: PresenceCheck | None = None,
) -> tuple[HeadEstimate, Person | None]:
    """With `presence`, whether the baby is in the crib is decided by it, and the pose model only
    tells where the head is turned. Without it, or while it fails, the pose model decides both."""
    in_crib = None if presence is None else presence.check(frame, now)
    if in_crib is False:
        return HeadEstimate(Side.ABSENT, method="crib_empty"), None
    if cfg.detector.upright:
        person = detector.detect_upright(frame, cfg.detector.min_keypoint_conf, now)
    else:
        person = detector.detect(frame)
    if person is None:
        if in_crib:
            # In the crib, but the pose model lost it: a white sleep sack at night, a blanket.
            return HeadEstimate(Side.UNKNOWN, method="baby_unseen"), None
        return HeadEstimate(Side.ABSENT, method="no_person"), None
    if not person.checked:
        # The second pass found nothing to check the face points on. The first pass alone is often
        # wrong about the face points, so the side is not guessed: the baby is still there.
        return HeadEstimate(Side.UNKNOWN, method="unchecked"), person
    estimate = estimate_head(
        person.xy,
        person.conf,
        min_conf=cfg.detector.min_keypoint_conf,
        center_threshold=cfg.head.center_threshold,
        mirrored=cfg.head.mirrored,
        fallback_head_direction=cfg.head.fallback_head_direction,
        body_up=person.up,
    )
    return estimate, person


def save_snapshot(
    shot: Frame,
    root: str,
    detector: DetectorConfig,
    motion: float | None = None,
    presence: float | None = None,
) -> None:
    """Save the clean frame (to review it or fine-tune the model later) and a line in
    analysis.jsonl with everything that was detected and the corner notes. The jsonl is enough to
    recompute stats and tune settings without running the model again, and `draw` makes the same
    picture as the preview from it when someone wants to look, so a drawn copy is not kept."""
    ts, estimate, person = shot.time, shot.estimate, shot.person
    folder = Path(root) / f"{ts:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    score = f"{estimate.score:+.2f}" if estimate.score is not None else "na"
    name = f"{ts:%H%M%S}_{estimate.side.value}_{score}"
    cv2.imwrite(str(folder / f"{name}.jpg"), shot.image)
    record = {
        "time": ts.isoformat(timespec="seconds"),
        "file": f"{name}.jpg",
        "side": estimate.side.value,
        "score": None if estimate.score is None else round(estimate.score, 3),
        "method": estimate.code,
        # Direction from the shoulders to the top of the head, for drawing the snapshot later.
        "axis": None if estimate.axis is None else [round(float(v), 3) for v in estimate.axis],
        "motion": None if motion is None else round(motion, 4),
        # How sure the presence check was that the baby is in the crib, to tune presence.threshold.
        "presence": None if presence is None else round(presence, 3),
        "model": detector.model,
        "imgsz": detector.imgsz,
        "upright": detector.upright,
        # Coordinates are in pixels of the saved frame, which is already cropped to camera.roi.
        "box": None if person is None else [round(float(v), 1) for v in person.box],
        "keypoints": None if person is None else [
            [round(float(x), 1), round(float(y), 1), round(float(c), 3)]
            for (x, y), c in zip(person.xy, person.conf)
        ],
        # The lines in the picture's corner then: asleep or not, the thermometers.
        "notes": shot.notes,
    }
    with open(folder / "analysis.jsonl", "a", encoding="utf-8") as out:
        out.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(cfg: Config, args: argparse.Namespace) -> None:
    if args.model:
        cfg.detector.model = args.model
    if args.device:
        cfg.detector.device = args.device
    source_spec = args.source if args.source is not None else cfg.camera.source
    source = open_source(source_spec, start=args.start, password=cfg.camera.password)
    detector = PoseDetector(
        cfg.detector.model,
        cfg.detector.device,
        cfg.detector.min_person_conf,
        cfg.detector.imgsz,
        cfg.detector.min_person_area,
    )
    presence = None
    if cfg.presence.enabled:
        # Imported here: it loads transformers, which takes a few seconds.
        from .presence import start

        presence = start(cfg.presence.model, cfg.detector.device, cfg.presence.threshold, cfg.presence.every_seconds)
    # A recorded video is not tonight: its frames go to a database of their own, so they do not
    # mix with the real nights, and nothing goes to Telegram or rings for it.
    replay = is_file_source(source_spec)
    if replay:
        db = Path(cfg.storage.db_path)
        cfg.storage.db_path = str(db.with_name(f"{db.stem}-replay{db.suffix}"))
        log.info(t("cli.replay", path=cfg.storage.db_path))
    else:
        check_sounds(cfg)
    # The main connection first: it creates or updates the tables before any thread opens its own.
    storage = Storage(cfg.storage.db_path)
    notifier = Notifier(cfg, live=not replay)
    thermometers = siren = watch = None
    if cfg.thermometers.devices and not replay:
        from .climate import ClimateWatch, Siren

        watch = ClimateWatch(cfg.thermometers, send=notifier.send, notify=notifier.notify)
        # The last saved readings, so /now and the preview show them from the first second.
        watch.remember(storage.latest_climate())
        notifier.climate = watch
        thermometers = ThermometerPoller(cfg.thermometers.devices, cfg.storage.db_path,
                                         cfg.thermometers.every_minutes, cfg.thermometers.scan_seconds, watch)
        siren = Siren(watch, cfg.thermometers.cold)
    tracker = SideTracker(cfg.tracking.confirm_seconds, cfg.tracking.gap_seconds)
    motion_meter = MotionMeter()
    monitor = monitor_from_config(cfg)
    notifier.start()
    if thermometers is not None:
        thermometers.start()
        siren.start()
    last_status = last_snapshot = last_sleep = 0.0
    failed_at = None
    asleep_note = None
    history_loaded = False

    log.info(t("cli.started", source=source_spec, fps=cfg.camera.sample_fps,
               quit=t("cli.quit_preview") if args.preview else ""))
    try:
        for ts, frame in source.frames(cfg.camera.sample_fps):
            try:
                if not history_loaded:
                    monitor.add(load_history(storage, cfg, until=ts))
                    history_loaded = True
                frame = preprocess(frame, cfg.camera.rotate, cfg.camera.roi)
                estimate, person = analyze(detector, frame, cfg, ts.timestamp(), presence)
                motion = motion_meter.update(frame, None if person is None else person.box, ts.timestamp())
                storage.add(ts, estimate, motion)
                monitor.add(tracker.update(ts, estimate.side))
                current = tracker.current
                now = ts.timestamp()

                if now - last_sleep >= SLEEP_EVERY_SECONDS:
                    last_sleep = now
                    asleep_note = None if current.side == Side.ABSENT else sleep_line(
                        storage.activity(ts - SLEEP_LOOKBACK, ts), cfg.sleep, ts)
                alert = monitor.check(ts, current)
                if alert is not None:
                    notifier.alert(alert)
                notifier.update(ts, current, monitor, asleep_note)
                readings = {} if watch is None else watch.readings()
                shot = Frame(frame, person, estimate, ts, corner_notes(asleep_note, cfg, readings, datetime.now()))
                notifier.set_frame(shot)

                if now - last_status >= STATUS_EVERY_SECONDS:
                    last_status = now
                    score = f"{estimate.score:+.2f}" if estimate.score is not None else "-"
                    log.info(t("cli.status", time=f"{ts:%H:%M:%S}", side=side_label(estimate.side),
                               score=score, method=estimate.describe(),
                               confirmed=side_label(current.side), duration=fmt_duration(current.seconds)))

                snapshot_due = (
                    cfg.storage.snapshot_every_seconds
                    and now - last_snapshot >= cfg.storage.snapshot_every_seconds
                )
                if snapshot_due:
                    last_snapshot = now
                    save_snapshot(shot, cfg.storage.snapshots_dir, cfg.detector, motion,
                                  presence.last_score if presence else None)
            except Exception:
                # One bad frame (a model error, a broken picture from the camera) must not end
                # the watch for the rest of the night: log it and go on with the next one.
                if failed_at is None or time.monotonic() - failed_at >= FAILURE_LOG_SECONDS:
                    failed_at = time.monotonic()
                    log.exception(t("cli.frame_failed"))
                continue
            if args.preview:
                cv2.imshow(PREVIEW_WINDOW, fit(picture(shot, cfg)))
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if siren is not None:
            siren.stop()
        if thermometers is not None:
            thermometers.stop()
        notifier.stop()
        storage.close()
        source.close()
        if args.preview:
            cv2.destroyAllWindows()
    log.info(t("cli.stopped", path=cfg.storage.db_path))


def report(cfg: Config, args: argparse.Namespace) -> None:
    day_start = parse_day_start(cfg.tracking.day_starts_at)
    if args.date:
        since, until = day_bounds(args.date, day_start)
    else:
        first_day = day_of(datetime.now(), day_start) - timedelta(days=args.days - 1)
        since, until = day_bounds(first_day, day_start)[0], None

    storage = Storage(cfg.storage.db_path)
    try:
        samples = storage.samples(since, until)
        # From 12 hours earlier, so that a sleep that began before the first day is recognised.
        activity = storage.activity(since - SLEEP_LOOKBACK, until)
        naps = nap_periods(storage, since - SLEEP_LOOKBACK, until)
    finally:
        storage.close()
    summaries = build_report(
        samples,
        center_threshold=cfg.head.center_threshold,
        confirm_seconds=cfg.tracking.confirm_seconds,
        gap_seconds=cfg.tracking.gap_seconds,
        day_start=day_start,
    )
    print(render(summaries, day_start, timeline_days=args.timelines))
    first = summaries[0].day if summaries else day_of(since, day_start)
    days = [d for d in sleep_days(activity, cfg.sleep, day_start, naps) if d.day >= first]
    print()
    print(render_sleep(days, day_start, detail_days=args.timelines, samples=activity))
    if args.chart:
        last = summaries[-1] if summaries else None
        sleep = next((d.periods for d in days if last and d.day == last.day), [])
        title = t("chart.title_day", date=day_label(last.day, day_start)) if last else ""
        png = render_chart(last, sleep, summaries, title, day_start)
        if png is not None:
            Path(args.chart).write_bytes(png)
            print()
            print(t("cli.chart_saved", path=args.chart))


def load_snapshot(path: Path, cfg: Config) -> Frame:
    """A saved frame and what was made of it, from the analysis.jsonl next to it."""
    frame = cv2.imread(str(path))
    if frame is None:
        raise SystemExit(t("cli.draw.unreadable", path=path))
    record = None
    analysis = path.parent / "analysis.jsonl"
    if analysis.exists():
        with open(analysis, encoding="utf-8") as lines:
            for line in lines:
                try:
                    found = json.loads(line)
                except json.JSONDecodeError:  # a line cut short when the program was stopped
                    continue
                if found.get("file") == path.name:
                    record = found  # the last one: a restart within the same second wrote the file again
    if record is None:
        raise SystemExit(t("cli.draw.no_record", path=path))
    method, _, axis_source = record["method"].partition("/")
    estimate = HeadEstimate(Side(record["side"]), record["score"], method, axis_source)
    shot = Frame(frame, None, estimate, datetime.fromisoformat(record["time"]), record.get("notes") or [])
    if not record["keypoints"]:
        return shot
    points = np.array(record["keypoints"], dtype=float)
    shot.person = Person(np.array(record["box"], dtype=float), 0.0, points[:, :2], points[:, 2])
    if record.get("axis"):
        estimate.axis = tuple(record["axis"])
        return shot
    # Snapshots saved before the axis was stored: recompute it. Without the first pass axis, on a
    # frame without shoulders the arrow follows head.fallback_head_direction.
    estimate.axis = estimate_head(
        shot.person.xy,
        shot.person.conf,
        min_conf=cfg.detector.min_keypoint_conf,
        center_threshold=cfg.head.center_threshold,
        mirrored=cfg.head.mirrored,
        fallback_head_direction=cfg.head.fallback_head_direction,
    ).axis
    return shot


def draw_snapshots(cfg: Config, args: argparse.Namespace) -> None:
    for path in args.snapshots:
        image = picture(load_snapshot(path, cfg), cfg)
        folder = args.out or path.parent
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{path.stem}_drawn.jpg"
        cv2.imwrite(str(out), fit(image))
        print(t("cli.draw.saved", path=out))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noggin-compass", description=t("cli.description"))
    parser.add_argument("-c", "--config", default="config.yaml", help=t("cli.help.config"))
    parser.add_argument("--lang", choices=LANGUAGES, help=t("cli.help.lang"))
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help=t("cli.help.run"))
    run_cmd.add_argument("--source", help=t("cli.help.source"))
    run_cmd.add_argument("--preview", action="store_true", help=t("cli.help.preview"))
    run_cmd.add_argument("--start", type=datetime.fromisoformat, help=t("cli.help.start"))
    run_cmd.add_argument("--model", help=t("cli.help.model"))
    run_cmd.add_argument("--device", help=t("cli.help.device"))

    report_cmd = commands.add_parser("report", help=t("cli.help.report"))
    report_cmd.add_argument("--days", type=int, default=7, help=t("cli.help.days"))
    report_cmd.add_argument("--date", type=date.fromisoformat, help=t("cli.help.date"))
    report_cmd.add_argument("--timelines", type=int, default=1, help=t("cli.help.timelines"))
    report_cmd.add_argument("--chart", metavar="FILE", help=t("cli.help.chart"))

    draw_cmd = commands.add_parser("draw", help=t("cli.help.draw"))
    draw_cmd.add_argument("snapshots", nargs="+", type=Path, metavar="SNAPSHOT", help=t("cli.help.snapshots"))
    draw_cmd.add_argument("-o", "--out", type=Path, metavar="DIR", help=t("cli.help.out"))

    thermometer_cmd = commands.add_parser("thermometer", help=t("cli.help.thermometer"))
    thermometer_cmd.add_argument("--seconds", type=float, default=30, help=t("cli.help.seconds"))
    return parser


def main(argv: list[str] | None = None) -> None:
    # The language is needed before the full parse so that --help is translated too:
    # --lang if given, otherwise the config.
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("-c", "--config", default="config.yaml")
    early.add_argument("--lang", choices=LANGUAGES)
    known, _ = early.parse_known_args(argv)
    config_path = Path(known.config)
    load_dotenv(config_path.parent / ".env")
    # The config's own language before checking it, so what is wrong in it is said in that language.
    set_language(known.lang or (language_of(config_path) if config_path.exists() else "en"))
    try:
        cfg = load_config(config_path if config_path.exists() else None)
    except ValueError as error:
        raise SystemExit(f"{config_path}: {error}") from None
    set_language(known.lang or cfg.language)

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not config_path.exists():
        if args.config != parser.get_default("config"):
            parser.error(t("cli.no_config_file", path=config_path))
        log.info(t("cli.defaults"))

    if args.command == "run":
        run(cfg, args)
    elif args.command == "draw":
        draw_snapshots(cfg, args)
    elif args.command == "thermometer":
        menu(cfg, config_path, args.seconds)
    else:
        report(cfg, args)


if __name__ == "__main__":
    main()
