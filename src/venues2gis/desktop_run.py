"""Рабочий процесс настольного приложения и четыре выгрузки."""
from __future__ import annotations

import json
import logging
import re
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .browser import BrowserOptions, BrowserSession
from .config import Config
from .export import build_records
from .lead_checks import category, inspect_company, on_domain, public_url
from .logging_setup import logger
from .orchestrator import prepare
from .store import Store
from .worker import run_worker

FILES = {
    1: "01_без_сайта.xlsx",
    2: "02_сайт_без_записи.xlsx",
    3: "03_сайт_с_записью.xlsx",
    4: "04_запись_внутри_2гис.xlsx",
}
COLUMNS = (
    ("name", "Компания", 28), ("firm_id", "ID филиала", 24),
    ("city", "Город", 18), ("address", "Адрес", 38),
    ("phone", "Телефон", 23), ("source_queries_all", "Поисковые запросы", 28),
    ("2gis_url", "Карточка 2ГИС", 42), ("website", "Сайт", 35),
    ("website_source", "Где найден сайт", 24), ("social_links", "Соцсети и мессенджеры", 40),
    ("booking_status", "Онлайн-запись", 22), ("booking_url", "Ссылка на запись", 38),
    ("external_booking", "Внешняя запись", 36), ("bot_status", "Статус бота", 23),
    ("bot_url", "Признак бота", 35), ("reason", "Основание", 46),
    ("issues", "Ограничения проверки", 46), ("checked_at", "Дата проверки", 25),
)


def write_excel(directory: Path, groups: dict[int, list], partial: bool) -> None:
    for number, filename in FILES.items():
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Компании"
        sheet.append([header for _, header, _ in COLUMNS])
        for i, (_, _, width) in enumerate(COLUMNS, 1):
            cell = sheet.cell(1, i)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="244967")
            sheet.column_dimensions[get_column_letter(i)].width = width
        for row_number, record in enumerate(groups[number], 2):
            for i, (key, _, _) in enumerate(COLUMNS, 1):
                value = record.get(key)
                if value is None:
                    continue
                cell = sheet.cell(row_number, i, value)
                # Текст организаций не должен превращаться в формулы Excel.
                if isinstance(value, str):
                    cell.data_type = "s"
                    url = public_url(value)
                    if key in ("2gis_url", "booking_url", "external_booking", "bot_url") and url:
                        cell.hyperlink = url
                        cell.style = "Hyperlink"
        sheet.freeze_panes = "C2"
        sheet.auto_filter.ref = sheet.dimensions
        if partial:
            summary = workbook.create_sheet("Статус")
            summary["A1"] = "Частичная выгрузка. Подробности в журнале запуска."
            summary.column_dimensions["A"].width = 62
        temporary = directory / (filename + ".tmp")
        workbook.save(temporary)
        temporary.replace(directory / filename)
        workbook.close()


def create_run_folder(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=datetime.now().strftime("2GIS_%Y-%m-%d_%H-%M_"), dir=output))


def config_for_run(directory: Path, city: str, queries: list[str], pages: int) -> Config:
    return Config.model_validate({
        "collect": {
            "regions": [city], "queries": queries, "layers": [], "rubric_sets": [],
            "max_pages_per_task": pages, "nav_timeout_ms": 18000, "card_response_timeout_ms": 4500,
            "firm_batch": 1, "max_attempts": 2, "antibot_max_hits": 1,
        },
        "browser": {"profile_dir": "browser"},
        "paths": {"raw_dir": "raw", "db_path": "state.sqlite", "regions_cache": "regions.json"},
    }).resolve(directory)


def desk_records(cfg) -> tuple[list[dict], dict, dict]:
    with Store(Path(cfg.paths.db_path), Path(cfg.paths.raw_dir)) as store:
        records = {}
        for record in build_records(store):
            if record["firm_id"]:
                records[record["firm_id"]] = record
        return list(records.values()), store.task_stats(), store.firm_stats()


ALTEGIO_LOOKUP = re.compile(
    r"/api/v1/booking/(?:search/(?:services|staff|dates|timeslots)|forms/\d+/attendances/validate)"
)


def read_only(route):
    request = route.request
    path = urlsplit(request.url).path.rstrip("/")
    provider_lookup = (request.method == "POST" and
                       on_domain(request.url, ("alteg.io",)) and
                       ALTEGIO_LOOKUP.fullmatch(path))
    if (request.method not in ("GET", "HEAD", "OPTIONS") and not provider_lookup
            or not public_url(request.url) or request.resource_type in ("image", "media", "font")):
        route.abort()
    else:
        route.continue_()


def result_row(row, findings, reason):
    links = sorted(findings.socials)
    links.extend(url for values in json.loads(row.get("social_links") or "{}").values()
                 for value in values if (url := public_url(value)) and url not in links)
    return {
        **row,
        "website": "\n".join(findings.websites),
        "website_source": "\n".join(dict.fromkeys(findings.websites.values())),
        "social_links": "\n".join(links),
        "booking_url": findings.internal or findings.external,
        "booking_status": ("Внутри 2ГИС" if findings.internal else "Внешняя запись" if findings.external
                           else "Не проверено" if findings.issues else "Не найдена"),
        "external_booking": findings.external,
        "bot_status": ("Найден" if findings.bot else "Не проверено" if findings.issues else "Не найден"),
        "bot_url": findings.bot, "reason": reason,
        "issues": ", ".join(sorted(findings.issues)),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def report_collection(stats: dict) -> None:
    logger.info("Карточек получено: %d из %d. Неудачных попыток: %d.",
                stats["карточек"], stats["найдено_заведений"], stats["неудач"])


def execute_run(directory: Path, city: str, queries: list[str], pages: int, stop: Event) -> dict:
    work = directory / "_данные"
    cfg = config_for_run(work, city, queries, pages)
    groups = {number: [] for number in FILES}
    handler = logging.FileHandler(directory / "журнал.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    collected = checked = review = bots = 0
    partial = False
    try:
        logger.info("Сбор карточек 2ГИС. Город: %s. Запросы: %s", city, ", ".join(queries))
        try:
            prepare(cfg)
            if not stop.is_set():
                run_worker(cfg, own_signals=False, cancel=stop.is_set, progress=report_collection)
        except Exception:
            partial = True
            logger.exception("Сбор прерван")
        rows, task_stats, firm_stats = desk_records(cfg)
        collected = len(rows)
        logger.info("Собрано уникальных филиалов: %d. Карточки: %s. Запросы: %s.",
                    collected, firm_stats, task_stats)
        partial |= any(status in counts for counts in (task_stats, firm_stats)
                       for status in ("failed", "pending", "running"))
        if rows and not stop.is_set():
            logger.info("Проверяем сайты, запись и ботов")
            try:
                with ExitStack() as stack:
                    session = BrowserSession(BrowserOptions(profile_dir=work / "browser-check"))
                    stack.callback(session.stop)
                    session.start()
                    web_context = session.browser.new_context(accept_downloads=False, service_workers="block")
                    stack.callback(web_context.close)
                    web_context.route("**/*", read_only)
                    web = web_context.new_page()
                    for index, row in enumerate(rows, 1):
                        if stop.is_set():
                            break
                        logger.info("Проверяем %d/%d · %s", index, collected, row["name"])
                        try:
                            findings = inspect_company(session.page, web, row, stop.is_set)
                        except Exception:
                            review += 1
                            partial = True
                            logger.exception("Ошибка проверки · %s", row["2gis_url"])
                            continue
                        if stop.is_set():
                            break
                        checked += 1
                        number, reason = category(findings)
                        if number:
                            groups[number].append(result_row(row, findings, reason))
                        else:
                            review += not bool(findings.bot)
                            bots += bool(findings.bot)
                            logger.info("%s · %s · %s", reason, row["name"], row["2gis_url"])
            except Exception:
                partial = True
                logger.exception("Проверки прерваны")
        partial |= stop.is_set()
        write_excel(directory, groups, partial)
        counts = {number: len(items) for number, items in groups.items()}
        logger.info("Готово. По файлам: %s. На проверку: %d. Ботов вне файла 4: %d%s.",
                    counts, review, bots, ". Частичная выгрузка" if partial else "")
        return {"counts": counts, "collected": collected, "checked": checked,
                "review": review, "bots": bots, "partial": partial}
    finally:
        logger.removeHandler(handler)
        handler.close()
