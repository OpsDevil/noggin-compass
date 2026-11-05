"""Command line: `noggin-compass run` watches the camera, `noggin-compass report` shows a report."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from .chart import day_label, render_chart
from .config import Config, DetectorConfig, load_config, load_dotenv
from .head import HeadEstimate, Side, estimate_head, side_label
from .i18n import LANGUAGES, set_language, t
from .motion import MotionMeter
from .naps import periods as nap_periods
from .notify import Notifier, load_history, monitor_from_config
from .pose import Person, PoseDetector
from .preview import draw, fit
from .report import build_report, day_bounds, day_of, fmt_duration, parse_day_start, render
from .sleep import render_sleep, sleep_days
from .source import open_source, preprocess
from .storage import Storage
from .tracker import SideTracker

if TYPE_CHECKING:
    from .presence import PresenceCheck

log = logging.getLogger("noggin_compass")

STATUS_EVERY_SECONDS = 60
PREVIEW_WINDOW = "noggin-compass"


def analyze(
    detector: PoseDetector,
    frame: np.ndarray,
    cfg: Config,
    now: float | None = None,
    presence: PresenceCheck | None = None,
) -> tuple[HeadEstimate, Person | None]:
    """With `presence`, whether the baby is in the crib is decided by it, and the pose model only
    tells where the head is turned. Without it, the pose model decides both."""
    if presence is not None and not presence.check(frame, now):
        return HeadEstimate(Side.ABSENT, method="crib_empty"), None
    if cfg.detector.upright:
        person = detector.detect_upright(frame, cfg.detector.min_keypoint_conf, now)
    else:
        person = detector.detect(frame)
    if person is None:
        if presence is not None:
            # In the crib, but the pose model lost it: a white sleep sack at night, a blanket.
            return HeadEstimate(Side.UNKNOWN, method="baby_unseen"), None
        return HeadEstimate(Side.ABSENT, method="no_person"), None
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
    frame: np.ndarray,
    ts: datetime,
    estimate: HeadEstimate,
    person: Person | None,
    root: str,
    detector: DetectorConfig,
    motion: float | None = None,
    presence: float | None = None,
) -> None:
    """Save the clean frame (to review it or fine-tune the model later) and a line in
    analysis.jsonl with everything that was detected. The jsonl is enough to recompute stats and
    tune settings without running the model again, and `draw` puts the detected points on a saved
    frame when someone wants to look, so a drawn copy is not kept."""
    folder = Path(root) / f"{ts:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    score = f"{estimate.score:+.2f}" if estimate.score is not None else "na"
    name = f"{ts:%H%M%S}_{estimate.side.value}_{score}"
    cv2.imwrite(str(folder / f"{name}.jpg"), frame)
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
    }
    with open(folder / "analysis.jsonl", "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")


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
        from .presence import PresenceCheck

        presence = PresenceCheck(
            cfg.presence.model, cfg.detector.device, cfg.presence.threshold, cfg.presence.every_seconds
        )
    storage = Storage(cfg.storage.db_path)
    tracker = SideTracker(cfg.tracking.confirm_seconds, cfg.tracking.gap_seconds)
    motion_meter = MotionMeter()
    monitor = monitor_from_config(cfg)
    notifier = Notifier(cfg)
    notifier.start()
    last_status = last_snapshot = 0.0
    history_loaded = False

    log.info(t("cli.started", source=source_spec, fps=cfg.camera.sample_fps,
               quit=t("cli.quit_preview") if args.preview else ""))
    try:
        for ts, frame in source.frames(cfg.camera.sample_fps):
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

            alert = monitor.check(ts, current)
            if alert is not None:
                notifier.alert(alert)
            notifier.update(ts, current, monitor)
            notifier.set_frame(frame, person, estimate, ts)

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
                save_snapshot(frame, ts, estimate, person, cfg.storage.snapshots_dir, cfg.detector, motion,
                              presence.last_score if presence else None)
            if args.preview:
                debug = draw(frame, person, estimate, cfg.detector.min_keypoint_conf, cfg.head.mirrored)
                cv2.imshow(PREVIEW_WINDOW, fit(debug))
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
    except KeyboardInterrupt:
        pass
    finally:
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
        activity = storage.activity(since - timedelta(hours=12), until)
        naps = nap_periods(storage, since - timedelta(hours=12), until)
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


def load_snapshot(path: Path, cfg: Config) -> tuple[np.ndarray, HeadEstimate, Person | None]:
    """A saved frame and what was detected on it, from the analysis.jsonl next to it."""
    frame = cv2.imread(str(path))
    if frame is None:
        raise SystemExit(t("cli.draw.unreadable", path=path))
    record = None
    analysis = path.parent / "analysis.jsonl"
    if analysis.exists():
        with open(analysis, encoding="utf-8") as lines:
            record = next((r for r in map(json.loads, lines) if r["file"] == path.name), None)
    if record is None:
        raise SystemExit(t("cli.draw.no_record", path=path))
    method, _, axis_source = record["method"].partition("/")
    estimate = HeadEstimate(Side(record["side"]), record["score"], method, axis_source)
    if not record["keypoints"]:
        return frame, estimate, None
    points = np.array(record["keypoints"], dtype=float)
    person = Person(np.array(record["box"], dtype=float), 0.0, points[:, :2], points[:, 2])
    if record.get("axis"):
        estimate.axis = tuple(record["axis"])
        return frame, estimate, person
    # Snapshots saved before the axis was stored: recompute it. Without the first pass axis, on a
    # frame without shoulders the arrow follows head.fallback_head_direction.
    estimate.axis = estimate_head(
        person.xy,
        person.conf,
        min_conf=cfg.detector.min_keypoint_conf,
        center_threshold=cfg.head.center_threshold,
        mirrored=cfg.head.mirrored,
        fallback_head_direction=cfg.head.fallback_head_direction,
    ).axis
    return frame, estimate, person


def draw_snapshots(cfg: Config, args: argparse.Namespace) -> None:
    for path in args.snapshots:
        frame, estimate, person = load_snapshot(path, cfg)
        image = draw(frame, person, estimate, cfg.detector.min_keypoint_conf, cfg.head.mirrored)
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
    cfg = load_config(config_path if config_path.exists() else None)
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
    else:
        report(cfg, args)


if __name__ == "__main__":
    main()
