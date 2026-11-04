"""Command line: `noggin-compass run` watches the camera, `noggin-compass report` shows a report."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from .config import Config, DetectorConfig, load_config, load_dotenv
from .head import HeadEstimate, Side, estimate_head, side_label
from .i18n import LANGUAGES, set_language, t
from .notify import Notifier, load_history, monitor_from_config
from .pose import Person, PoseDetector
from .preview import draw, fit
from .report import build_report, day_bounds, day_of, fmt_duration, parse_day_start, render
from .source import open_source, preprocess
from .storage import Storage
from .tracker import SideTracker

log = logging.getLogger("noggin_compass")

STATUS_EVERY_SECONDS = 60
PREVIEW_WINDOW = "noggin-compass"


def analyze(
    detector: PoseDetector, frame: np.ndarray, cfg: Config, now: float | None = None
) -> tuple[HeadEstimate, Person | None]:
    if cfg.detector.upright:
        person = detector.detect_upright(frame, cfg.detector.min_keypoint_conf, now)
    else:
        person = detector.detect(frame)
    if person is None:
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
    debug: np.ndarray,
    ts: datetime,
    estimate: HeadEstimate,
    person: Person | None,
    root: str,
    detector: DetectorConfig,
) -> None:
    """Save the clean frame (to review it or fine-tune the model later), a _debug copy with the
    overlay, and a line in analysis.jsonl with everything that was detected. The jsonl is enough
    to recompute stats and tune settings without running the model again."""
    folder = Path(root) / f"{ts:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    score = f"{estimate.score:+.2f}" if estimate.score is not None else "na"
    name = f"{ts:%H%M%S}_{estimate.side.value}_{score}"
    cv2.imwrite(str(folder / f"{name}.jpg"), frame)
    cv2.imwrite(str(folder / f"{name}_debug.jpg"), fit(debug))
    record = {
        "time": ts.isoformat(timespec="seconds"),
        "file": f"{name}.jpg",
        "side": estimate.side.value,
        "score": None if estimate.score is None else round(estimate.score, 3),
        "method": estimate.code,
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
        cfg.detector.model, cfg.detector.device, cfg.detector.min_person_conf, cfg.detector.imgsz
    )
    storage = Storage(cfg.storage.db_path)
    tracker = SideTracker(cfg.tracking.confirm_seconds, cfg.tracking.gap_seconds)
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
            estimate, person = analyze(detector, frame, cfg, ts.timestamp())
            storage.add(ts, estimate)
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
            if not (snapshot_due or args.preview):
                continue
            debug = draw(frame, person, estimate, cfg.detector.min_keypoint_conf, cfg.head.mirrored)
            if snapshot_due:
                last_snapshot = now
                save_snapshot(frame, debug, ts, estimate, person, cfg.storage.snapshots_dir, cfg.detector)
            if args.preview:
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
    else:
        report(cfg, args)


if __name__ == "__main__":
    main()
