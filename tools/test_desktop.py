"""Проверка классификации, браузерных сценариев и файлов приложения."""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from venues2gis.browser import BrowserOptions, BrowserSession
from venues2gis.desktop_run import (
    FILES, config_for_run, desk_records, execute_run, result_row, write_excel,
)
from venues2gis.store import Store
from venues2gis.lead_checks import Findings, bot_link, category, inspect_company, public_url

CARD = "https://2gis.kz/astana/firm/1"
SITE = "https://test-company.com/"
BOOKING = "https://test-company.com/booking"
TELEGRAM = "https://t.me/appointment_example_bot"
WHATSAPP = "https://wa.me/77000000000"
FORM = '<form aria-label="Онлайн-запись"><label>Дата<input type="date"></label></form>'


class DesktopTest(unittest.TestCase):
    def test_file_rules(self):
        examples = [
            (1, Findings(external=BOOKING)),
            (2, Findings(websites={SITE: "Карточка"})),
            (3, Findings(websites={SITE: "Карточка"}, external=BOOKING)),
            (4, Findings(internal=CARD, bot=TELEGRAM, issues={"Недоступен сайт"})),
            (0, Findings(bot=TELEGRAM)),
            (0, Findings(issues={"Недоступен сайт"})),
        ]
        for expected, result in examples:
            with self.subTest(result=result):
                self.assertEqual(category(result)[0], expected)

    def test_contacts_and_wrappers(self):
        self.assertFalse(bot_link(WHATSAPP, "Записаться"))
        self.assertTrue(bot_link(WHATSAPP, "Открыть чат-бот"))
        self.assertTrue(bot_link(TELEGRAM))
        self.assertFalse(bot_link("https://t.me/example"))
        self.assertFalse(bot_link("https://instagram.com/example"))
        self.assertEqual(public_url("http://127.0.0.1/login"), "")
        self.assertEqual(public_url("https://l.instagram.com/?u=https%3A%2F%2Ftest-company.com%2F"), SITE)
        for metadata, expected in ((b"", BOOKING), (b'\nhttps://2gis.com\n{"adsBanner":{}}', "")):
            encoded = base64.urlsafe_b64encode(BOOKING.encode() + metadata).decode()
            self.assertEqual(public_url(f"https://link.2gis.com/4.2/AA/{encoded}"), expected)

    def test_repeated_branch_has_one_record(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config_for_run(Path(folder), "nur_sultan", ["маникюр", "педикюр"], 1)
            with Store(Path(cfg.paths.db_path), Path(cfg.paths.raw_dir)) as store:
                for key, source in (("one", "маникюр"), ("two", "педикюр")):
                    store.write_raw("nur_sultan", {
                        "source_query": source,
                        "response": {"result": {"items": [{"id": f"70000001000000008_{key}", "name": "Салон"}]}},
                    })
            records, _, _ = desk_records(cfg)
            self.assertEqual([record["firm_id"] for record in records], ["70000001000000008"])

    def test_workbooks_and_cancelled_run(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            stop = Event()

            def cancel_midway(cfg, **kwargs):
                stop.set()

            with patch("venues2gis.desktop_run.run_worker", side_effect=cancel_midway):
                result = execute_run(directory, "nur_sultan", ["тест"], 1, stop)
            groups = {number: [] for number in FILES}
            groups[4].append(result_row(
                {"name": "=1+1", "firm_id": "70000001000000008", "2gis_url": CARD},
                Findings(internal=CARD, bot=TELEGRAM), "Встроенная запись",
            ))
            self.assertTrue(result["partial"])
            write_excel(directory, groups, result["partial"])
            self.assertEqual(len(list(directory.glob("*.xlsx"))), 4)
            for number, filename in FILES.items():
                workbook = load_workbook(directory / filename)
                with self.subTest(number=number):
                    self.assertIn("Статус", workbook.sheetnames)
                    sheet = workbook["Компании"]
                    self.assertEqual(sheet.max_row, 2 if number == 4 else 1)
                    if number == 4:
                        self.assertEqual(sheet["A2"].data_type, "s")
                        self.assertEqual(sheet["A2"].value, "=1+1")
                        self.assertEqual(sheet["G2"].hyperlink.target, CARD)
                workbook.close()


class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(folder.cleanup)
        session = BrowserSession(BrowserOptions(profile_dir=Path(folder.name) / "browser", headless=True))
        cls.addClassCleanup(session.stop)
        session.start()
        cls.context = session.browser.new_context(service_workers="block")
        cls.addClassCleanup(cls.context.close)
        warmup = cls.context.new_page()
        warmup.goto("data:text/html,<title>Ready</title>", wait_until="load")
        warmup.close()

    def check(self, pages, *, site="", socials=None):
        def handle(route):
            path = route.request.url
            route.fulfill(status=200 if path in pages else 404, content_type="text/html; charset=utf-8",
                          body=pages.get(path, "404"))

        self.context.route("**/*", handle)
        card = self.context.new_page()
        web = self.context.new_page()
        try:
            return inspect_company(card, web, {"2gis_url": CARD, "website": site,
                                               "social_links": json.dumps(socials or {})}, lambda: False)
        finally:
            card.close()
            web.close()
            self.context.unroute("**/*", handle)

    def test_internal_external_and_plain_messenger(self):
        base = "<h1>Организация</h1>"
        internal = self.check({
            CARD: base + '<button type="button" onclick="document.querySelector(\'#booking\').hidden=false">'
                         'Записаться</button><div id="booking" hidden>' + FORM + '</div>',
        }, socials={"telegram": [TELEGRAM]})
        self.assertEqual(category(internal)[0], 4, str(internal))
        self.assertEqual(internal.bot, TELEGRAM)

        external = self.check({CARD: base + f'<a href="{BOOKING}">Онлайн-запись</a>',
                               SITE: base, BOOKING: FORM}, site=SITE)
        self.assertEqual(category(external)[0], 3)
        self.assertFalse(external.internal)
        self.assertEqual(external.external, BOOKING)

        plain = self.check({CARD: base + f'<a href="{WHATSAPP}">Записаться в WhatsApp</a>'})
        self.assertEqual(category(plain)[0], 1)
        self.assertIn(WHATSAPP, plain.socials)
        self.assertFalse(plain.external or plain.bot)

    def test_profile_website_and_unreachable_website(self):
        profile = "https://t.me/example"
        result = self.check({CARD: "<h1>Организация</h1>", profile: f'<a href="{SITE}">Сайт</a>',
                             SITE: "<h1>Сайт организации</h1>"}, socials={"telegram": [profile]})
        self.assertEqual(category(result)[0], 2)
        self.assertIn(SITE, result.websites)
        result = self.check({CARD: "<h1>Организация</h1>"}, site=SITE)
        self.assertEqual(category(result)[0], 0)
        self.assertTrue(result.issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
