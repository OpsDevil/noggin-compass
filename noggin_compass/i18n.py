"""Interface texts: English by default, Russian as a translation.

Usage: ``t("cli.stopped", path=...)``. A key missing in the current language falls back to English.
"""

from __future__ import annotations

LANGUAGES = ("en", "ru")

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # Head positions
        "side.left": "left",
        "side.right": "right",
        "side.center": "straight",
        "side.unknown": "not visible",
        "side.absent": "not in frame",
        "sym.left": "L",
        "sym.right": "R",
        # How the estimate was computed
        "method.no_person": "nobody in the frame",
        "method.crib_empty": "the crib is empty",
        "presence.downloading": "Downloading the presence model {model} (about 1.5 GB) into {path}, once",
        "presence.unavailable": "The presence model {model} could not be loaded ({error}). Working on the pose model alone, as with presence.enabled: false, so an empty crib may show up as occupied. Restart the program to try again",
        "presence.failed": "The presence check failed ({error}). Working on the pose model alone and trying again every {seconds} s",
        "presence.recovered": "The presence check works again",
        "method.baby_unseen": "in the crib, but the points are not found",
        "method.face_hidden": "face not visible",
        "method.ears_hidden": "ears not visible",
        "method.both_ears": "both ears",
        "method.ear_and_eyes": "ear and eyes",
        "method.one_ear_profile": "one ear, profile",
        "method.nose_and_eyes": "nose and eyes",
        "method.with_axis": "{method}, axis: {axis}",
        "axis.shoulders": "shoulders",
        "axis.body": "body",
        "axis.config": "config",
        # Preview
        "kp.0": "nose",
        "kp.1": "left eye",
        "kp.2": "right eye",
        "kp.3": "left ear",
        "kp.4": "right ear",
        "kp.5": "left shoulder",
        "kp.6": "right shoulder",
        "preview.crown": "crown",
        "preview.baby_left": "baby's left",
        "preview.no_point": "none",
        # Durations and dates
        "dur.seconds": "{s} s",
        "dur.minutes": "{m} min",
        "dur.hours": "{h} h {m:02d} min",
        "weekdays": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        # Report
        "report.no_data": "No data yet.",
        "report.day_note": (
            "A day runs from {start} to {start}. "
            "Percentages are of the time the head turn was visible."
        ),
        "report.col.day": "day",
        "report.col.turns": "turns",
        "report.day_title": "Day {date}:",
        "report.longest": "longest in a row: {duration}",
        "report.difference": "Left/right difference: {pct} towards '{side}'",
        "report.timeline": "Timeline (1 character = 1 minute):",
        # Command line
        "cli.description": "How long a baby sleeps with the head turned left / right / straight",
        "cli.help.config": "config file (default: config.yaml)",
        "cli.help.lang": "interface language",
        "cli.help.run": "analyse the camera stream or a video file",
        "cli.help.source": "instead of camera.source: an RTSP URL, a camera index or a video file",
        "cli.help.preview": "show a window with the frame and the detected keypoints",
        "cli.help.start": "for a video file: when the recording started, e.g. 2026-10-29T21:00",
        "cli.help.model": "instead of detector.model: a model file or name, e.g. models/yolo26s-pose.pt",
        "cli.help.device": "instead of detector.device: cpu, mps (Apple GPU) or 0 (first NVIDIA GPU)",
        "cli.help.report": "per-day report",
        "cli.help.days": "how many recent days (default: 7)",
        "cli.help.date": "a single day, e.g. 2026-10-29",
        "cli.help.timelines": "for how many recent days to show details and the timeline",
        "cli.help.chart": "also save a chart of the last day as a PNG file",
        "cli.chart_saved": "Chart saved to {path}",
        "cli.help.draw": "draw the detected points on saved snapshots",
        "cli.help.snapshots": "snapshot files, e.g. data/snapshots/2026-11-05/0812*.jpg",
        "cli.help.out": "folder for the drawn copies (default: next to each snapshot)",
        "cli.draw.saved": "Saved {path}",
        "cli.draw.unreadable": "cannot read the image {path}",
        "cli.draw.no_record": "no line for {path} in analysis.jsonl next to it",
        "cli.no_config_file": "config file {path} not found",
        "cli.defaults": "config.yaml not found, using defaults (the Mac's built-in camera)",
        "cli.started": "Source: {source}, analysing {fps:.1f} frames/s. Stop with Ctrl+C{quit}",
        "cli.quit_preview": " or q in the preview window",
        "cli.status": "{time} frame: {side} (score {score}; {method}) | confirmed: {confirmed}, {duration}",
        "cli.stopped": "Stopped, data saved to {path}",
        # Turn alerts and the Telegram bot
        "alert.stint": "Time to turn the baby: the head has been turned {side} for {duration}. Turn it {turn}.",
        "alert.imbalance": (
            "Time to turn the baby: over the last {hours} h the head was turned {side} for {more} "
            "and {turn} for only {less}. Turn it {turn}."
        ),
        "bot.help": (
            "noggin-compass\n"
            "/now - where the head is now and whether it is time to turn\n"
            "/stats - time per side over the last 24 hours\n"
            "/sleep - sleep over the last 24 hours\n"
            "/nap - log sleep outside the crib (in arms, in a pram)\n"
            "/naps - change or delete logged sleep\n"
            "/chart - a chart of the last 24 hours and left versus right per day\n"
            "/photo - a picture from the camera right now\n"
            "/marked - the same picture with what the program detected\n"
            "Turn alerts arrive here by themselves."
        ),
        "bot.cmd.now": "where the head is now",
        "bot.cmd.stats": "time per side, last 24 hours",
        "bot.cmd.photo": "picture from the camera now",
        "bot.cmd.marked": "picture with the detected points",
        "bot.photos_off": "Pictures are off. To allow /photo and /marked, set telegram.photos: true in the config.",
        "bot.photo_caption": "{time}, head turned {side} (score {score})",
        "bot.no_data": "No data yet: the program has just started.",
        "bot.now_side": "The head is turned {side}, on this side for {duration}.",
        "bot.now_center": "The head is straight.",
        "bot.now_unknown": "The face is not visible right now.",
        "bot.now_absent": "The baby is not in the crib.",
        "bot.now_balance": "Last {hours} h: left {left}, right {right}.",
        "bot.turn": "Time to turn it {turn}.",
        "bot.ok": "No need to turn yet.",
        "bot.stats": (
            "Last 24 hours:\n"
            "left {left} ({left_pct})\n"
            "right {right} ({right_pct})\n"
            "straight {center} ({center_pct})\n"
            "not visible {unknown}\n"
            "turns: {turns}"
        ),
        "sleep.title": "Sleep (days start at {start}):",
        "sleep.day": "{total}, sleeps: {count}, longest: {longest}",
        "sleep.day_title": "Sleep on {date}:",
        "sleep.period": "{start}-{end}  {duration}",
        "sleep.ongoing": "{start}-now  {duration}",
        "sleep.none": "No sleep found for these days. Sleep is told from the baby's movement, which older versions did not record.",
        "sleep.none_yet": "No sleep found yet: it counts after the baby has lain still for a while.",
        "bot.cmd.sleep": "sleep, last 24 hours",
        "bot.sleep": "Sleep over the last 24 hours: {total}, sleeps: {count}, longest: {longest}",
        "bot.now_asleep": "Asleep for {duration}, since {since}.",
        "bot.now_awake": "Not asleep right now.",
        "bot.stats_sleep": "sleep {total}, sleeps: {count}",
        "chart.title_recent": "Head over the last 24 hours",
        "chart.title_day": "Head, day {date}",
        "chart.timeline": "When",
        "chart.head": "head",
        "chart.sleep": "sleep",
        "chart.days": "{left} and {right} per day",
        "bot.cmd.chart": "chart, last 24 hours",
        "bot.chart_caption": "Data as of {time}",
        "bot.cmd.nap": "log sleep outside the crib",
        "bot.cmd.naps": "change logged sleep",
        "sleep.manual": "{line} (outside the crib)",
        "nap.ask": "How long did the baby sleep outside the crib? The sleep has just ended.",
        "nap.hours": "{h} h",
        "nap.entry": "{when}, {duration}",
        "nap.added": "Logged sleep outside the crib: {entry}.",
        "nap.list": "Sleep outside the crib, latest first. Tap an entry to change or delete it.",
        "nap.list_empty": "No sleep outside the crib has been logged yet. To log it: /nap",
        "nap.gone": "This entry no longer exists.",
        "nap.ask_again": "Logged: {entry}. How long did the baby really sleep?",
        "nap.changed": "Changed: {entry}.",
        "nap.deleted": "Deleted: {entry}.",
        "nap.btn.change": "Change",
        "nap.btn.delete": "Delete",
        "nap.btn.back": "Back",
        "nap.btn.list": "Change logged sleep",
        "tg.started": "Telegram: alerts and commands for {count} chat(s)",
        "tg.unknown_chat": (
            "Telegram: ignored a message from chat {chat}. To allow it, add {chat} to "
            "telegram.chat_ids or NOGGIN_TELEGRAM_CHAT_IDS."
        ),
        "tg.error": "Telegram is unavailable: {error}",
        # Errors and warnings
        "err.unknown_option": "Unknown config option: {key}",
        "err.not_section": "{key} must be a section",
        "err.fallback_direction": "head.fallback_head_direction must be up, down, left or right",
        "err.rotate": "camera.rotate must be 0, 90, 180 or 270",
        "err.video": "Could not open video {path}",
        "err.password": (
            "Camera password not found. Use one of:\n"
            "  - the {env} environment variable, e.g. a line {env}=... in the .env file\n"
            "    next to config.yaml;\n"
            "  - camera.password in config.yaml;\n"
            "  - the macOS Keychain: security add-generic-password -s {service} -a CAMERA_LOGIN -w"
        ),
        "warn.no_frames": "No frames from {source}, reconnecting in {delay:.0f} s",
    },
    "ru": {
        "side.left": "влево",
        "side.right": "вправо",
        "side.center": "прямо",
        "side.unknown": "не видно",
        "side.absent": "нет в кадре",
        "sym.left": "Л",
        "sym.right": "П",
        "method.no_person": "человека нет в кадре",
        "method.crib_empty": "кроватка пустая",
        "presence.downloading": "Скачиваю модель присутствия {model} (около 1.5 ГБ) в {path}, один раз",
        "presence.unavailable": "Модель присутствия {model} не загрузилась ({error}). Работаю только на модели поз, как с presence.enabled: false, поэтому пустая кроватка может выйти занятой. Чтобы попробовать снова, перезапустите программу",
        "presence.failed": "Проверка присутствия не сработала ({error}). Пока работаю только на модели поз и пробую снова каждые {seconds} с",
        "presence.recovered": "Проверка присутствия снова работает",
        "method.baby_unseen": "в кроватке, но точки не найдены",
        "method.face_hidden": "лицо не видно",
        "method.ears_hidden": "уши не видны",
        "method.both_ears": "оба уха",
        "method.ear_and_eyes": "ухо и глаза",
        "method.one_ear_profile": "одно ухо, профиль",
        "method.nose_and_eyes": "нос и глаза",
        "method.with_axis": "{method}, ось: {axis}",
        "axis.shoulders": "плечи",
        "axis.body": "тело",
        "axis.config": "конфиг",
        "kp.0": "нос",
        "kp.1": "лев. глаз",
        "kp.2": "прав. глаз",
        "kp.3": "лев. ухо",
        "kp.4": "прав. ухо",
        "kp.5": "лев. плечо",
        "kp.6": "прав. плечо",
        "preview.crown": "макушка",
        "preview.baby_left": "его лево",
        "preview.no_point": "нет",
        "dur.seconds": "{s} с",
        "dur.minutes": "{m} мин",
        "dur.hours": "{h} ч {m:02d} мин",
        "weekdays": "пн,вт,ср,чт,пт,сб,вс",
        "report.no_data": "Данных пока нет.",
        "report.day_note": (
            "Сутки считаются с {start} до {start}. "
            "Проценты - от времени, когда поворот головы был виден."
        ),
        "report.col.day": "сутки",
        "report.col.turns": "поворотов",
        "report.day_title": "Сутки {date}:",
        "report.longest": "дольше всего подряд: {duration}",
        "report.difference": "Разница влево/вправо: {pct} в пользу '{side}'",
        "report.timeline": "Хронология (1 символ = 1 минута):",
        "cli.description": "Сколько времени малыш спит с головой, повёрнутой влево / вправо / прямо",
        "cli.help.config": "файл настроек (по умолчанию config.yaml)",
        "cli.help.lang": "язык интерфейса",
        "cli.help.run": "анализировать поток с камеры или видеофайл",
        "cli.help.source": "вместо camera.source: RTSP-адрес, номер камеры или путь к видео",
        "cli.help.preview": "окно с кадром и найденными точками",
        "cli.help.start": "для видеофайла: когда началась запись, напр. 2026-10-29T21:00",
        "cli.help.model": "вместо detector.model: файл или имя модели, напр. models/yolo26s-pose.pt",
        "cli.help.device": "вместо detector.device: cpu, mps (GPU Apple) или 0 (первая видеокарта NVIDIA)",
        "cli.help.report": "отчёт по суткам",
        "cli.help.days": "за сколько последних суток (по умолчанию 7)",
        "cli.help.date": "одни конкретные сутки, напр. 2026-10-29",
        "cli.help.timelines": "для скольких последних суток показать подробности и хронологию",
        "cli.help.chart": "ещё сохранить график за последние сутки в PNG-файл",
        "cli.chart_saved": "График сохранён в {path}",
        "cli.help.draw": "нарисовать найденные точки на сохранённых снимках",
        "cli.help.snapshots": "файлы снимков, напр. data/snapshots/2026-11-05/0812*.jpg",
        "cli.help.out": "папка для копий с точками (по умолчанию рядом с каждым снимком)",
        "cli.draw.saved": "Сохранено: {path}",
        "cli.draw.unreadable": "не удаётся прочитать картинку {path}",
        "cli.draw.no_record": "для {path} нет строки в analysis.jsonl рядом с ним",
        "cli.no_config_file": "нет файла настроек {path}",
        "cli.defaults": "config.yaml не найден, использую настройки по умолчанию (встроенная камера Mac)",
        "cli.started": "Источник: {source}, анализ {fps:.1f} кадр/с. Остановить - Ctrl+C{quit}",
        "cli.quit_preview": " или q в окне превью",
        "cli.status": "{time} кадр: {side} (score {score}; {method}) | подтверждено: {confirmed}, {duration}",
        "cli.stopped": "Остановлено, данные сохранены в {path}",
        "alert.stint": "Пора повернуть малыша: голова повёрнута {side} уже {duration}. Поверните её {turn}.",
        "alert.imbalance": (
            "Пора повернуть малыша: за последние {hours} ч голова была повёрнута {side} {more}, "
            "а {turn} всего {less}. Поверните её {turn}."
        ),
        "bot.help": (
            "noggin-compass\n"
            "/now - где сейчас голова и не пора ли поворачивать\n"
            "/stats - время по сторонам за последние 24 часа\n"
            "/sleep - сон за последние 24 часа\n"
            "/nap - записать сон вне кроватки (на руках, в коляске)\n"
            "/naps - изменить или удалить записанный сон\n"
            "/chart - график за последние 24 часа и влево/вправо по суткам\n"
            "/photo - снимок с камеры прямо сейчас\n"
            "/marked - тот же снимок с тем, что нашла программа\n"
            "Сигналы \"пора повернуть\" приходят сюда сами."
        ),
        "bot.cmd.now": "где сейчас голова",
        "bot.cmd.stats": "время по сторонам за 24 часа",
        "bot.cmd.photo": "снимок с камеры сейчас",
        "bot.cmd.marked": "снимок с найденными точками",
        "bot.photos_off": "Снимки выключены. Чтобы разрешить /photo и /marked, укажите telegram.photos: true в конфиге.",
        "bot.photo_caption": "{time}, голова: {side} (score {score})",
        "bot.no_data": "Данных пока нет: программа только запустилась.",
        "bot.now_side": "Голова повёрнута {side}, на этой стороне уже {duration}.",
        "bot.now_center": "Голова прямо.",
        "bot.now_unknown": "Лицо сейчас не видно.",
        "bot.now_absent": "Малыша нет в кроватке.",
        "bot.now_balance": "За последние {hours} ч: влево {left}, вправо {right}.",
        "bot.turn": "Пора повернуть её {turn}.",
        "bot.ok": "Поворачивать пока не нужно.",
        "bot.stats": (
            "За последние 24 часа:\n"
            "влево {left} ({left_pct})\n"
            "вправо {right} ({right_pct})\n"
            "прямо {center} ({center_pct})\n"
            "не видно {unknown}\n"
            "поворотов: {turns}"
        ),
        "sleep.title": "Сон (сутки с {start}):",
        "sleep.day": "{total}, засыпаний: {count}, самый долгий: {longest}",
        "sleep.day_title": "Сон за {date}:",
        "sleep.period": "{start}-{end}  {duration}",
        "sleep.ongoing": "{start}-сейчас  {duration}",
        "sleep.none": "За эти дни сна не найдено. Сон определяется по движению малыша, а старые версии программы его не записывали.",
        "sleep.none_yet": "Сна пока не найдено: он засчитывается, когда малыш какое-то время полежит спокойно.",
        "bot.cmd.sleep": "сон за 24 часа",
        "bot.sleep": "Сон за последние 24 часа: {total}, засыпаний: {count}, самый долгий: {longest}",
        "bot.now_asleep": "Спит уже {duration}, с {since}.",
        "bot.now_awake": "Сейчас не спит.",
        "bot.stats_sleep": "сон {total}, засыпаний: {count}",
        "chart.title_recent": "Голова за последние 24 часа",
        "chart.title_day": "Голова за сутки {date}",
        "chart.timeline": "Когда",
        "chart.head": "голова",
        "chart.sleep": "сон",
        "chart.days": "{left} и {right} по суткам",
        "bot.cmd.chart": "график за 24 часа",
        "bot.chart_caption": "Данные на {time}",
        "bot.cmd.nap": "записать сон вне кроватки",
        "bot.cmd.naps": "изменить записанный сон",
        "sleep.manual": "{line} (вне кроватки)",
        "nap.ask": "Сколько малыш спал вне кроватки? Сон закончился только что.",
        "nap.hours": "{h} ч",
        "nap.entry": "{when}, {duration}",
        "nap.added": "Записан сон вне кроватки: {entry}.",
        "nap.list": "Сон вне кроватки, сначала последний. Нажмите на запись, чтобы изменить или удалить её.",
        "nap.list_empty": "Сон вне кроватки ещё не записывали. Записать: /nap",
        "nap.gone": "Этой записи уже нет.",
        "nap.ask_again": "Записано: {entry}. Сколько малыш спал на самом деле?",
        "nap.changed": "Исправлено: {entry}.",
        "nap.deleted": "Удалено: {entry}.",
        "nap.btn.change": "Изменить",
        "nap.btn.delete": "Удалить",
        "nap.btn.back": "Назад",
        "nap.btn.list": "Изменить записанный сон",
        "tg.started": "Telegram: сигналы и команды для чатов: {count}",
        "tg.unknown_chat": (
            "Telegram: сообщение из чата {chat} пропущено. Чтобы разрешить его, добавьте {chat} в "
            "telegram.chat_ids или NOGGIN_TELEGRAM_CHAT_IDS."
        ),
        "tg.error": "Telegram недоступен: {error}",
        "err.unknown_option": "Неизвестный параметр в конфиге: {key}",
        "err.not_section": "{key} должен быть секцией",
        "err.fallback_direction": "head.fallback_head_direction: up, down, left или right",
        "err.rotate": "camera.rotate должен быть 0, 90, 180 или 270",
        "err.video": "Не удалось открыть видео {path}",
        "err.password": (
            "Пароль камеры не найден. Задайте его одним из способов:\n"
            "  - переменная окружения {env}, например строка {env}=... в файле .env\n"
            "    рядом с config.yaml;\n"
            "  - camera.password в config.yaml;\n"
            "  - Связка ключей macOS: security add-generic-password -s {service} -a ЛОГИН_КАМЕРЫ -w"
        ),
        "warn.no_frames": "Нет кадров с {source}, переподключаюсь через {delay:.0f} с",
    },
}

_language = "en"


def set_language(language: str) -> None:
    global _language
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
    _language = language


def get_language() -> str:
    return _language


def t(key: str, /, **kwargs) -> str:
    text = MESSAGES[_language].get(key) or MESSAGES["en"][key]
    return text.format(**kwargs) if kwargs else text
