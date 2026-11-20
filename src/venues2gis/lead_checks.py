"""Проверка публичных признаков сайта, записи и ботов."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import Error as PlaywrightError

BOOKING = re.compile(r"записаться|онлайн[ -]?запись|забронировать|book (?:now|online|an appointment)|appointment|резерв|жазылу", re.I)
PROFILE_WEBSITE = re.compile(r"^(?:сайт|веб-сайт|website|официальный сайт|site)$", re.I)
CONTACTS = re.compile(r"контакты|contacts?|байланыс", re.I)
PRICES = re.compile(r"^(?:цены|услуги|prices|services)$", re.I)
STEPS = re.compile(r"(?:выбер(?:ите|и)|выбрать) (?:услуг[уи]|мастера|специалиста|дату|время)|дата и время|выбор (?:даты|времени|мастера|услуги)|available (?:times|slots)|select (?:a |your )?(?:date|time|service)", re.I)
BOT = re.compile(r"чат[ -]?бот|бот[ -]?помощник|напишите (?:нашему )?боту|chat[ -]?bot|automated (?:assistant|bot)", re.I)
PLATFORMS = {
    "telegram": ("t.me", "telegram.me", "telegram.org"),
    "whatsapp": ("wa.me", "whatsapp.com"),
    "instagram": ("instagram.com",),
}
SOCIAL = (*[host for hosts in PLATFORMS.values() for host in hosts],
          "facebook.com", "vk.com", "tiktok.com", "youtube.com", "linktr.ee", "taplink.cc", "taplink.ru")
PROVIDERS = ("zapis.kz", "yclients.com", "alteg.io", "dikidi.net", "appointer.kz", "calendly.com", "simplybook.me")
GIS = ("2gis.kz", "2gis.ru", "2gis.com")
REDIRECT = ("link.2gis.com", "link.2gis.ru", "l.instagram.com", "l.facebook.com")
ACCESS_WALL = re.compile(r"just a moment|access denied|verify (?:that )?you are human|unusual traffic|проверка браузера|доступ ограничен", re.I)

SNAPSHOT = """() => {
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const links = [...document.querySelectorAll('a[href], iframe[src]')].filter(visible).map(e => [
        e.href || e.src, (e.innerText || e.title || e.getAttribute('aria-label') || '').trim(),
        e.tagName === 'IFRAME'
    ]);
    const form = [...document.forms].filter(visible).some(f =>
        /запис|брони|appointment|booking|жазыл/i.test(
            [f.innerText, f.id, f.getAttribute('aria-label')].join(' ')) &&
        [...f.querySelectorAll('input:not([type=hidden]), select, textarea')].some(visible)
    );
    const calendar = [...document.querySelectorAll(
        'input[type=date], [role=grid], [class*="calendar" i], [class*="timeslot" i]'
    )].some(visible);
    return {links, form, calendar, text: (document.body?.innerText || '').trim(),
            title: document.title};
}"""
OPEN_CONTROLS = 'button[type=button], button:not([type]), [role=button], [role=tab]'


def on_domain(url: str, domains: tuple[str, ...]) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def public_url(value: str) -> str:
    """Оставляет публичные HTTP(S)-адреса; раскрывает ссылки-переходники."""
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value and re.match(r"^[\w-]+(?:\.[\w-]+)+(?:[/?#]|$)", value):
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".")
        if parts.scheme not in ("http", "https") or "." not in host or parts.username:
            return ""
        if host.endswith((".local", ".localhost", ".internal", ".test", ".example")):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        if host in ("link.2gis.com", "link.2gis.ru"):
            path_parts = unquote(parts.path).split("/")
            for part in reversed(path_parts):
                if not part.startswith(("aHR0cD", "aHR0cHM")):
                    continue
                raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
                dest, _, metadata = raw.partition(b"\n")
                if b'"adsBanner":' in metadata:
                    return ""
                decoded = dest.decode("utf-8").strip()
                return "" if on_domain(decoded, REDIRECT) else public_url(decoded)
        if host in REDIRECT:
            target = parse_qs(parts.query).get("u", [""])[0]
            return public_url(target) if target and not on_domain(target, REDIRECT) else ""
        return value
    except (ValueError, UnicodeError, binascii.Error):
        return ""


def platform(url: str) -> str:
    return next((name for name, domains in PLATFORMS.items() if on_domain(url, domains)), "")


def bot_link(url: str, label: str = "") -> bool:
    channel = platform(url)
    handle = urlsplit(url).path.strip("/").rsplit("/", 1)[-1]
    telegram_bot = channel == "telegram" and bool(re.fullmatch(r"[A-Za-z0-9_]{2,}bot", handle, re.I))
    return bool(channel and (telegram_bot or BOT.search(label)))


def booking_form(snapshot: dict | None, contextual: bool = False) -> bool:
    if not snapshot:
        return False
    text = snapshot["text"]
    return bool(snapshot["form"] or (
        STEPS.search(text) and (
            snapshot["calendar"] or contextual and len(STEPS.findall(text)) >= 2
        )
    ))


def has_booking_form(page, snapshot: dict, *, contextual: bool) -> bool:
    if booking_form(snapshot, contextual):
        return True
    for iframe in page.locator("iframe[src]").all()[:12]:
        try:
            src = public_url(iframe.get_attribute("src") or "")
            if not src or not iframe.is_visible():
                continue
            if contextual or on_domain(src, PROVIDERS) or BOOKING.search(src):
                handle = iframe.element_handle(timeout=1000)
                frame = handle.content_frame() if handle else None
                if frame and booking_form(frame.evaluate(SNAPSHOT), contextual=True):
                    return True
        except PlaywrightError:
            continue
    return False


@dataclass
class Findings:
    websites: dict[str, str] = field(default_factory=dict)
    socials: set[str] = field(default_factory=set)
    internal: str = ""
    external: str = ""
    bot: str = ""
    issues: set[str] = field(default_factory=set)


def category(result: Findings) -> tuple[int, str]:
    if result.internal:
        return 4, "Встроенная форма записи в 2ГИС"
    if result.bot:
        return 0, "Есть публичный признак бота"
    if result.issues:
        return 0, "Требуется проверка: " + ", ".join(sorted(result.issues))
    if not result.websites:
        return 1, "Сайт и бот не найдены"
    if result.external:
        return 3, "Подтверждена внешняя онлайн-запись"
    return 2, "Сайт есть, онлайн-запись и бот не найдены"


def snapshot_page(page) -> dict:
    for attempt in range(2):
        try:
            if snapshot := page.evaluate(SNAPSHOT):
                return snapshot
        except PlaywrightError:
            if attempt:
                raise
        page.wait_for_timeout(350)
    raise ValueError("Страница сменилась во время проверки")


def browse(page, url: str, *, settle: int = 650) -> dict:
    response = page.goto(url, wait_until="domcontentloaded", timeout=12000)
    if response is None or response.status >= 400:
        raise ValueError("Страница недоступна")
    if settle:
        page.wait_for_timeout(max(settle, 3300) if on_domain(page.url, PROVIDERS) else settle)
    snapshot = snapshot_page(page)
    if (ACCESS_WALL.search(snapshot["title"] + "\n" + snapshot["text"][:400])
            or on_domain(page.url, SOCIAL) and re.search(r"/(?:accounts/login|login|challenge)(?:[/?]|$)", urlsplit(page.url).path)):
        raise ValueError("Страница ограничила доступ")
    return snapshot


def links(snapshot: dict):
    for value, label, is_frame in snapshot["links"]:
        url = public_url(value)
        if url:
            yield url, " ".join(label.split()), is_frame


def booking_candidates(snapshot: dict):
    return (url for url, label, frame in links(snapshot)
            if not on_domain(url, (*GIS, *SOCIAL)) and
            (BOOKING.search(label) or on_domain(url, PROVIDERS) or
             frame and BOOKING.search(urlsplit(url).path)))


def card_booking(page, card_url: str, snapshot: dict, result: Findings, queue: deque) -> None:
    queue.extend((url, "booking") for url in booking_candidates(snapshot))
    if any(value.lower().startswith(("dgis:", "2gis:")) and BOOKING.search(label)
           for value, label, _ in snapshot["links"]):
        result.issues.add("запись открывается в мобильном приложении 2ГИС")
    if has_booking_form(page, snapshot, contextual=True):
        result.internal = page.url
        return

    # Сначала смотрим саму карточку, затем публичную вкладку цен и услуг.
    for section in (None, PRICES):
        if section is not None:
            tab = page.locator(f'a, {OPEN_CONTROLS}').filter(has_text=section)
            try:
                if not tab.count() or not tab.first.is_visible():
                    continue
                href = tab.first.get_attribute("href")
                if href and not on_domain(public_url(href), GIS):
                    continue
                tab.first.click(timeout=1800)
                page.wait_for_timeout(650)
                queue.extend((url, "booking") for url in booking_candidates(snapshot_page(page)))
            except (PlaywrightError, ValueError):
                result.issues.add("вкладка цен 2ГИС недоступна")
                continue
        controls = page.locator(OPEN_CONTROLS).filter(has_text=re.compile(
            r"^\s*(?:онлайн[ -]?запись|записаться(?: онлайн| на при[её]м| на услугу)?|забронировать)\s*$", re.I))
        for control in controls.all()[:4]:
            if not control.is_visible() or control.evaluate("e => Boolean(e.closest('form'))"):
                continue
            popup = []

            def opened(new_page):
                popup.append(new_page)

            page.on("popup", opened)
            try:
                control.click(timeout=1800)
                page.wait_for_timeout(950)
                for new_page in popup:
                    target = public_url(new_page.url)
                    if target:
                        if on_domain(target, GIS):
                            embedded = snapshot_page(new_page)
                            if has_booking_form(new_page, embedded, contextual=True):
                                result.internal = target
                        else:
                            queue.append((target, "booking"))
                if result.internal:
                    return
                target = public_url(page.url)
                if target and not on_domain(target, GIS):
                    queue.append((target, "booking"))
                    browse(page, card_url)
                    break
                opened_form = snapshot_page(page)
                if has_booking_form(page, opened_form, contextual=True):
                    result.internal = page.url
                    return
                candidates = list(booking_candidates(opened_form))
                queue.extend((url, "booking") for url in candidates)
                if not candidates and not popup:
                    result.issues.add("кнопка записи 2ГИС, форма не подтверждена")
            except (PlaywrightError, ValueError):
                result.issues.add("кнопка записи 2ГИС недоступна")
            finally:
                page.remove_listener("popup", opened)
                for new_page in popup:
                    new_page.close()


def inspect_company(card, web, row: dict, cancelled) -> Findings:
    result = Findings()
    sources = deque()
    for value in (row.get("website") or "").split(";"):
        url = public_url(value)
        if not url:
            continue
        if on_domain(url, SOCIAL):
            sources.append((url, "profile"))
        elif on_domain(url, PROVIDERS):
            sources.append((url, "booking"))
        elif not on_domain(url, GIS):
            result.websites[url] = "Карточка 2ГИС"
            sources.append((url, "website"))
    for urls in json.loads(row.get("social_links") or "{}").values():
        sources.extend((url, "profile") for value in urls if (url := public_url(value)))

    try:
        snapshot = browse(card, row["2gis_url"])
        if not card.locator("h1").count():
            raise ValueError("Не загрузилась карточка")
        for url, label, _ in links(snapshot):
            if platform(url):
                sources.append((url, "profile"))
                if bot_link(url, label):
                    result.bot = url
        card_booking(card, row["2gis_url"], snapshot, result, sources)
    except (ValueError, PlaywrightError):
        result.issues.add("карточка 2ГИС недоступна")

    seen: set[tuple[str, str]] = set()
    while sources and not cancelled():
        url, kind = sources.popleft()
        if (url, kind) in seen:
            continue
        if len(seen) >= 18:
            result.issues.add("достигнут лимит проверки внешних страниц")
            break
        seen.add((url, kind))
        if kind == "profile":
            result.socials.add(url)
        if bot_link(url):
            result.bot = url
        if (kind == "profile" and result.websites and not platform(url)
                and not on_domain(url, ("linktr.ee", "taplink.cc", "taplink.ru"))):
            continue
        if (platform(url) == "whatsapp" or bot_link(url)
                or platform(url) == "instagram" and "/direct/" in urlsplit(url).path):
            continue
        try:
            snapshot = browse(web, url)
        except (PlaywrightError, ValueError):
            result.issues.add(f"страница недоступна ({urlsplit(url).hostname})")
            continue
        final_url = public_url(web.url)
        if not final_url:
            result.issues.add("переход по внешней ссылке не подтверждён")
            continue
        if kind == "website" and on_domain(final_url, (*SOCIAL, *PROVIDERS)):
            result.websites.pop(url, None)
            sources.append((final_url, "profile" if on_domain(final_url, SOCIAL) else "booking"))
            continue
        if kind == "profile" and not on_domain(final_url, SOCIAL) and not on_domain(final_url, PROVIDERS):
            result.websites.setdefault(final_url, f"Переход из профиля {urlsplit(url).hostname}")
        if platform(final_url) and BOT.search(snapshot["text"][:1000]):
            result.bot = final_url
        if has_booking_form(web, snapshot, contextual=kind == "booking"):
            result.external = final_url
        hidden_booking = False
        for dest, label, frame in links(snapshot):
            channel = platform(dest)
            if channel:
                result.socials.add(dest)
                if bot_link(dest, label):
                    result.bot = dest
                if kind in ("website", "contact"):
                    sources.append((dest, "profile"))
            elif on_domain(dest, GIS):
                continue
            elif BOOKING.search(label) or on_domain(dest, PROVIDERS) or frame and BOOKING.search(dest):
                if dest != final_url and not on_domain(dest, SOCIAL):
                    hidden_booking = True
                    sources.append((dest, "booking"))
            elif kind == "website" and CONTACTS.search(label) and urlsplit(dest).hostname == urlsplit(final_url).hostname:
                sources.append((dest, "contact"))
            elif kind == "profile" and not on_domain(dest, SOCIAL) and (
                PROFILE_WEBSITE.fullmatch(label) or urlsplit(dest).hostname.removeprefix("www.") in label.lower()
            ):
                result.websites.setdefault(dest, f"Профиль {urlsplit(final_url).hostname}")
                sources.append((dest, "website"))
            elif kind == "profile" and on_domain(dest, ("linktr.ee", "taplink.cc", "taplink.ru")):
                sources.append((dest, "profile"))
        if kind == "booking" and not result.external and not hidden_booking:
            result.issues.add(f"форма записи не подтверждена ({urlsplit(final_url).hostname})")
        if kind in ("website", "contact") and not result.external and not hidden_booking:
            for control in web.locator(OPEN_CONTROLS).filter(has_text=BOOKING).all()[:3]:
                if not control.is_visible() or control.evaluate("e => Boolean(e.closest('form'))"):
                    continue
                try:
                    control.click(timeout=1500)
                    web.wait_for_timeout(650)
                    updated = snapshot_page(web)
                    if has_booking_form(web, updated, contextual=True):
                        result.external = web.url
                        break
                    candidates = list(booking_candidates(updated))
                    if public_url(web.url) and web.url != final_url:
                        candidates.append(web.url)
                    sources.extend((dest, "booking") for dest in candidates)
                    if not candidates:
                        result.issues.add(f"кнопка записи без подтверждённой формы ({urlsplit(final_url).hostname})")
                except (PlaywrightError, ValueError):
                    result.issues.add(f"кнопка записи недоступна ({urlsplit(final_url).hostname})")
    return result
