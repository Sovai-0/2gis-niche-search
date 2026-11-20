"""Настольное приложение для сбора компаний по нишам."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from venues2gis.desktop_run import create_run_folder, execute_run
from venues2gis.logging_setup import logger
from venues2gis.regions import FALLBACK_KZ_REGIONS


class WindowLog(logging.Handler):
    def __init__(self, events: Queue):
        super().__init__()
        self.events = events

    def emit(self, record):
        self.events.put(("log", self.format(record)))


class App:
    def __init__(self, window: tk.Tk):
        self.window = window
        self.events: Queue = Queue()
        self.stop = Event()
        self.active = False
        self.closing = False
        self.directory: Path | None = None
        self.cities = {region["name"]: region["code"] for region in FALLBACK_KZ_REGIONS}

        window.title("Поиск компаний в 2ГИС")
        window.geometry("790x650")
        window.minsize(665, 555)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.rowconfigure(0, weight=1)
        window.columnconfigure(0, weight=1)

        body = ttk.Frame(window, padding=20)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(9, weight=1)
        ttk.Label(body, text="Поиск компаний в 2ГИС", font=("TkDefaultFont", 19, "bold")).grid(
            row=0, columnspan=3, sticky="w", pady=(0, 16))
        self.city = tk.StringVar(value="Астана")
        self.pages = tk.StringVar(value="3")
        default_output = Path.home() / "Documents"
        self.output = tk.StringVar(value=str((default_output if default_output.is_dir() else Path.home()) / "2GIS"))
        self.status = tk.StringVar(value="Готов к запуску")

        ttk.Label(body, text="Город").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Combobox(body, textvariable=self.city, values=sorted(self.cities),
                     state="readonly", width=24).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(body, text="По одному запросу на строку").grid(row=2, columnspan=3, sticky="w", pady=(9, 4))
        self.queries = tk.Text(body, height=4, wrap="word", font="TkDefaultFont", padx=6, pady=5)
        self.queries.grid(row=3, columnspan=3, sticky="ew")
        self.queries.focus_set()

        ttk.Label(body, text="Страниц на запрос").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=8)
        ttk.Spinbox(body, from_=1, to=500, textvariable=self.pages, width=7).grid(row=4, column=1, sticky="w")
        ttk.Label(body, text="Папка выгрузки").grid(row=5, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(body, textvariable=self.output).grid(row=5, column=1, sticky="ew")
        ttk.Button(body, text="Выбрать…", command=self.choose).grid(row=5, column=2, padx=(8, 0))

        actions = ttk.Frame(body)
        actions.grid(row=6, columnspan=3, sticky="w", pady=(16, 8))
        self.start_button = ttk.Button(actions, text="Запустить", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Остановить", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="Открыть результаты", command=self.open_results, state="disabled")
        self.open_button.pack(side="left")
        ttk.Label(body, textvariable=self.status, wraplength=625).grid(row=7, columnspan=3, sticky="w", pady=(0, 4))
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.grid(row=8, columnspan=3, sticky="ew", pady=(0, 9))

        log_box = ttk.Frame(body)
        log_box.grid(row=9, columnspan=3, sticky="nsew")
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)
        self.log = tk.Text(log_box, state="disabled", height=9, wrap="word", font="TkDefaultFont", padx=6, pady=5)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        window.after(80, self.poll)

    def choose(self):
        selected = filedialog.askdirectory(parent=self.window)
        if selected:
            self.output.set(selected)

    def start(self):
        queries = list(dict.fromkeys(line.strip() for line in self.queries.get("1.0", "end").splitlines()
                                     if line.strip()))
        try:
            pages = int(self.pages.get())
            if not queries:
                raise ValueError("Введи хотя бы один поисковый запрос.")
            if not 1 <= pages <= 500:
                raise ValueError("Укажи от 1 до 500 страниц на запрос.")
            if not self.output.get().strip():
                raise ValueError("Выбери папку выгрузки.")
            directory = create_run_folder(Path(self.output.get().strip()).expanduser())
        except (ValueError, OSError) as exc:
            messagebox.showerror("Проверь настройки", str(exc), parent=self.window)
            return

        self.directory = directory
        self.stop.clear()
        self.active = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.status.set("Запускаем сбор…")
        self.progress.start(12)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        Thread(target=self.run, args=(directory, self.cities[self.city.get()], queries, pages)).start()

    def run(self, directory, city, queries, pages):
        handler = WindowLog(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        try:
            result = execute_run(directory, city, queries, pages, self.stop)
        except Exception as exc:
            self.events.put(("error", str(exc)))
        else:
            self.events.put(("result", result))
        finally:
            logger.removeHandler(handler)

    def request_stop(self):
        self.stop.set()
        self.stop_button.configure(state="disabled")
        self.status.set("Останавливаемся и сохраняем проверенные записи…")

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", value + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                    if self.active and not self.stop.is_set():
                        self.status.set(value.splitlines()[0])
                    continue
                self.active = False
                self.progress.stop()
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.open_button.configure(state="normal")
                if self.closing:
                    self.window.destroy()
                    return
                if kind == "result":
                    counts = " · ".join(f"Файл {n} — {count}" for n, count in value["counts"].items())
                    status = "Частичная выгрузка" if value["partial"] else "Готово"
                    self.status.set(f"{status}\n{counts}\nНа проверку: {value['review']} · Боты вне файла 4: {value['bots']}")
                else:
                    self.status.set("Ошибка запуска: " + value)
        except Empty:
            self.window.after(80, self.poll)

    def open_results(self):
        if self.directory is None:
            return
        try:
            if os.name == "nt":
                os.startfile(self.directory)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.directory)])
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self.window)

    def close(self):
        if self.active:
            self.closing = True
            self.request_stop()
        else:
            self.window.destroy()


def main():
    window = tk.Tk()
    App(window)
    window.mainloop()


if __name__ == "__main__":
    main()
