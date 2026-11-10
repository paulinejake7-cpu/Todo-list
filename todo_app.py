"""Туду-лист: задачи и подзадачи, категории по срокам, дедлайны, напоминания,
связи между задачами, описания и отчёты о завершении, поиск,
синхронизация с Google Диском.

Запуск:  python todo_app.py
"""
import copy
import json
import os
import queue
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, simpledialog, ttk

import gdrive_sync
from datepicker import pick_date
from tray import Tray
from store import (CATEGORIES, MONTHS, REMIND_FMT, TaskStore, closed_date, compute_report, due_status,
                   fmt_date, fmt_remind, merge_tasks, parse_date, parse_datetime, period_range)

DATA_DIR = Path(os.environ.get("TODO_APP_DIR", Path.home() / ".todo_app"))
APP_DIR = Path(__file__).resolve().parent
CAT_KEYS = list(CATEGORIES)
CAT_NAMES = [CATEGORIES[k] for k in CAT_KEYS]
ARCHIVE_KEY = "archive"
TAB_KEYS = CAT_KEYS + [ARCHIVE_KEY]          # вкладки: четыре категории и архив
TAB_NAMES = {**CATEGORIES, ARCHIVE_KEY: "Архив"}
DEFAULT_ARCHIVE_DAYS = 1
ARCHIVE_CHOICES = (("Сразу после завершения", 0), ("На следующий день", 1), ("Через 3 дня", 3),
                   ("Через неделю", 7), ("Через месяц", 30), ("Никогда", -1))
REMINDER_CHECK_MS = 30_000
MAX_REMINDER_WINDOWS = 5

# ---- оформление. Настройки хранятся локально (не синхронизируются между устройствами)
SETTINGS_PATH = DATA_DIR / "settings.json"
MIN_FONT, MAX_FONT = 8, 32
STD_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont")
THEME_CHOICES = (("Как в системе", "system"), ("Светлая", "light"), ("Тёмная", "dark"))
STATS_KINDS = (("День", "day"), ("Неделя", "week"), ("Месяц", "month"), ("Год", "year"), ("Всё время", "all"))
THEMES = {
    "light": dict(bg="#f5f5f7", fg="#1c1c1e", field="#ffffff", sel_bg="#2563eb", sel_fg="#ffffff",
                  muted="#666666", border="#c8c8cc", button="#e6e6ea", button_active="#d8d8de",
                  done="#8a8a8a", overdue="#c0392b", today="#b9770e", ok="#2e8b57"),
    "dark": dict(bg="#1e1f22", fg="#e6e6e6", field="#2b2d30", sel_bg="#3b6fd8", sel_fg="#ffffff",
                 muted="#9a9a9a", border="#43454a", button="#35373b", button_active="#43454a",
                 done="#7d7d7d", overdue="#ff7b72", today="#e3b341", ok="#3fb950"),
}


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # не удалось сохранить настройки — не критично


def fmt_dt(iso):
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y %H:%M")


def short_date(iso):
    d = date.fromisoformat(iso)
    return d.strftime("%d.%m") if d.year == date.today().year else d.strftime("%d.%m.%y")


def short_remind(s):
    dt = datetime.strptime(s, REMIND_FMT)
    return dt.strftime("%H:%M") if dt.date() == date.today() else dt.strftime("%d.%m %H:%M")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Туду-лист")
        root.geometry("1150x740")
        root.minsize(900, 560)

        self.store = TaskStore(DATA_DIR / "tasks.json")
        self.current_id = None      # задача, показанная в форме справа
        self._loading = False
        self._suppress_filter = False
        self._link_ids = []
        self._last_day = date.today()
        self.sync_queue = queue.Queue()
        self.tray_queue = queue.Queue()  # команды из потока трея -> главный поток Tk
        self.tray = Tray(on_show=lambda: self.tray_queue.put("show"),
                         on_quit=lambda: self.tray_queue.put("quit"))
        self.tray.start()

        self.settings = load_settings()
        self._view_win = None
        self._stats_win = None
        self._archived = set()  # id основных задач, показываемых во вкладке «Архив»
        self._native_theme = ttk.Style(root).theme_use()
        self._init_fonts()

        self._build_ui()
        self.apply_appearance()
        self.refresh()
        self.load_form(None)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Control-s>", lambda e: self.save_form())
        self.root.bind("<Control-q>", lambda e: self.quit_app())
        self.root.bind("<Control-f>", lambda e: (self.search_entry.focus_set(), "break")[1])
        self.root.after(200, self._poll_sync)
        self.root.after(1500, self._tick)  # напоминания, в том числе пропущенные, пока приложение было закрыто

    # ------------------------------------------------------------ интерфейс
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Мои задачи", font=self.font_header).pack(side="left")

        ttk.Button(top, text="Выход", command=self.quit_app).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Вид", command=self.open_view_settings).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="📊 Отчёт", command=self.open_stats).pack(side="right", padx=(6, 0))
        if self.tray.available:
            ttk.Button(top, text="В трей", command=self.hide_to_tray).pack(side="right", padx=(6, 0))
        self.sync_btn = ttk.Button(top, text="⟳ Синхронизировать", command=self.do_sync)
        self.sync_btn.pack(side="right")
        self.status = ttk.Label(top, text="", foreground="#666")
        self.status.pack(side="right", padx=10)

        search = ttk.Frame(top)
        search.pack(side="left", padx=(24, 0))
        ttk.Label(search, text="Поиск:").pack(side="left")
        self.q_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.q_var, width=24)
        self.search_entry.pack(side="left", padx=4)
        self.search_entry.bind("<Escape>", lambda e: self.q_var.set(""))
        ttk.Button(search, text="✕", width=3, command=lambda: self.q_var.set("")).pack(side="left")
        self.hide_done_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(search, text="Скрыть завершённые", variable=self.hide_done_var).pack(side="left", padx=8)
        self.q_var.trace_add("write", self.on_filter_change)
        self.hide_done_var.trace_add("write", self.on_filter_change)

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # --- левая часть: вкладки с деревьями задач
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        self.nb = ttk.Notebook(left)
        self.nb.pack(fill="both", expand=True)
        self.trees = {}
        for key in TAB_KEYS:
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=TAB_NAMES[key])
            tree = ttk.Treeview(frame, show="tree", selectmode="browse")
            sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            tree.tag_configure("done", font=self.done_font, foreground="#8a8a8a")
            tree.tag_configure("overdue", foreground="#c0392b")
            tree.tag_configure("duetoday", foreground="#b9770e")
            tree.tag_configure("failed", foreground="#c0392b")
            tree.bind("<<TreeviewSelect>>", self.on_select)
            for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):  # правая кнопка (Windows/Linux/macOS)
                tree.bind(seq, self.on_tree_context)
            self.trees[key] = tree
        self.nb.bind("<<NotebookTabChanged>>", self.on_select)

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="+ Задача", command=self.add_task).pack(side="left")
        ttk.Button(btns, text="+ Подзадача", command=self.add_subtask).pack(side="left", padx=4)
        move_btn = ttk.Menubutton(btns, text="Перенести в… ▾")
        self.move_menu = tk.Menu(move_btn, tearoff=0, postcommand=lambda: self._fill_move_menu(self.move_menu))
        move_btn["menu"] = self.move_menu
        move_btn.pack(side="left")
        ttk.Button(btns, text="Удалить", command=self.delete_task).pack(side="right")

        # --- правая часть: карточка задачи
        right = ttk.Frame(paned, padding=(10, 0, 0, 0))
        paned.add(right, weight=2)
        right.columnconfigure(0, weight=1)

        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(right, textvariable=self.title_var, font=self.font_title)
        self.title_entry.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.meta_label = ttk.Label(right, text="", foreground="#666")
        self.meta_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))

        row = ttk.Frame(right)
        row.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Label(row, text="Категория:").pack(side="left")
        self.cat_var = tk.StringVar()
        self.cat_box = ttk.Combobox(row, textvariable=self.cat_var, values=CAT_NAMES, state="readonly", width=18)
        self.cat_box.pack(side="left", padx=6)
        self.cat_box.bind("<<ComboboxSelected>>", lambda e: self.on_category_selected())
        self.done_btn = ttk.Button(row, text="✔ Завершить", command=self.toggle_done)
        self.done_btn.pack(side="right")
        self.fail_btn = ttk.Button(row, text="✘ Провалена", command=self.mark_failed)
        self.fail_btn.pack(side="right", padx=(0, 6))

        # дедлайн
        drow = ttk.Frame(right)
        drow.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(drow, text="Дедлайн:", width=11).pack(side="left")
        self.due_var = tk.StringVar()
        self.due_entry = ttk.Entry(drow, textvariable=self.due_var, width=14)
        self.due_entry.pack(side="left")
        self.due_entry.bind("<Return>", lambda e: self.save_form())
        self.due_btns = [
            ttk.Button(drow, text="📅", width=3, command=self.pick_due),
            ttk.Button(drow, text="Сегодня", width=9, command=lambda: self.quick_due(0)),
            ttk.Button(drow, text="Завтра", width=8, command=lambda: self.quick_due(1)),
            ttk.Button(drow, text="+7 дн.", width=7, command=lambda: self.quick_due(7)),
            ttk.Button(drow, text="✕", width=3, command=lambda: self.quick_due(None)),
        ]
        for b in self.due_btns:
            b.pack(side="left", padx=(4, 0))
        self.cal_btn = self.due_btns[0]

        # напоминание
        rrow = ttk.Frame(right)
        rrow.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(rrow, text="Напомнить:", width=11).pack(side="left")
        self.remind_var = tk.StringVar()
        self.remind_entry = ttk.Entry(rrow, textvariable=self.remind_var, width=17)
        self.remind_entry.pack(side="left")
        self.remind_entry.bind("<Return>", lambda e: self.save_form())
        self.remind_btns = [
            ttk.Button(rrow, text="Через 1 ч", width=9, command=lambda: self.quick_remind(hours=1)),
            ttk.Button(rrow, text="Завтра 9:00", width=11, command=lambda: self.quick_remind(tomorrow_9=True)),
            ttk.Button(rrow, text="✕", width=3, command=lambda: self.quick_remind()),
        ]
        for b in self.remind_btns:
            b.pack(side="left", padx=(4, 0))

        ttk.Label(right, text="Описание").grid(row=5, column=0, sticky="w", pady=(10, 2))
        self.desc = tk.Text(right, height=7, wrap="word", undo=True)
        self.desc.grid(row=6, column=0, columnspan=2, sticky="nsew")

        ttk.Label(right, text="Отчёт (итог или причина провала)").grid(row=7, column=0, sticky="w", pady=(10, 2))
        self.report = tk.Text(right, height=5, wrap="word", undo=True)
        self.report.grid(row=8, column=0, columnspan=2, sticky="nsew")

        ttk.Label(right, text="Связанные задачи (двойной щелчок — перейти)").grid(
            row=9, column=0, sticky="w", pady=(10, 2))
        self.links_list = tk.Listbox(right, height=4, activestyle="none")
        self.links_list.grid(row=10, column=0, sticky="nsew")
        self.links_list.bind("<Double-Button-1>", self.open_link)
        lbtn = ttk.Frame(right)
        lbtn.grid(row=10, column=1, sticky="n", padx=(6, 0))
        self.link_add_btn = ttk.Button(lbtn, text="Связать…", command=self.add_link)
        self.link_add_btn.pack(fill="x")
        self.link_del_btn = ttk.Button(lbtn, text="Убрать связь", command=self.remove_link)
        self.link_del_btn.pack(fill="x", pady=(4, 0))

        self.save_btn = ttk.Button(right, text="Сохранить (Ctrl+S)", command=self.save_form)
        self.save_btn.grid(row=11, column=0, columnspan=2, sticky="e", pady=(10, 0))

        right.rowconfigure(6, weight=3)
        right.rowconfigure(8, weight=2)
        right.rowconfigure(10, weight=1)

    # ------------------------------------------------------------ вид: шрифт и тема
    def _font_settings(self):
        family = self.settings.get("font_family") or self._default_family
        try:
            size = int(self.settings.get("font_size") or self._default_size)
        except (TypeError, ValueError):
            size = self._default_size
        return family, max(MIN_FONT, min(MAX_FONT, size))

    def _init_fonts(self):
        base = tkfont.nametofont("TkDefaultFont")
        self._default_family = base.actual("family")
        size = int(base.cget("size"))
        if size < 0:  # на некоторых системах размер задан в пикселях
            size = round(-size * 72 / self.root.winfo_fpixels("1i"))
        self._default_size = max(MIN_FONT, size)
        self.done_font = tkfont.Font(root=self.root, overstrike=True)  # перечёркнутый шрифт
        self.font_header = tkfont.Font(root=self.root, weight="bold")
        self.font_title = tkfont.Font(root=self.root, weight="bold")
        self.font_big = tkfont.Font(root=self.root)
        self._apply_fonts()

    def _apply_fonts(self):
        family, size = self._font_settings()
        for name in STD_FONTS:
            try:
                tkfont.nametofont(name).configure(family=family, size=size)
            except tk.TclError:
                pass
        for f, extra in ((self.done_font, 0), (self.font_header, 3), (self.font_title, 1), (self.font_big, 1)):
            f.configure(family=family, size=size + extra)

    def apply_appearance(self):
        """Применяет шрифт и тему из self.settings ко всему окну (на лету)."""
        self._apply_fonts()
        self._apply_theme()
        # высота строк зависит от шрифта; задаётся после выбора темы, т.к. стили привязаны к теме
        ttk.Style(self.root).configure(
            "Treeview", rowheight=tkfont.nametofont("TkDefaultFont").metrics("linespace") + 10)

    def _apply_theme(self):
        name = self.settings.get("theme", "system")
        if name not in ("system", *THEMES):
            name = "system"
        style = ttk.Style(self.root)
        if name == "system":
            style.theme_use(self._native_theme)
            pal = dict(THEMES["light"])
            pal.update(bg=style.lookup("TFrame", "background") or pal["bg"], fg="#000000", field="#ffffff")
        else:
            pal = THEMES[name]
            self._style_clam(style, pal)
        self.pal = pal

        self.root.configure(bg=pal["bg"])
        # tk-виджеты (не ttk) красим вручную
        text_opts = dict(bg=pal["field"], fg=pal["fg"], insertbackground=pal["fg"],
                         selectbackground=pal["sel_bg"], selectforeground=pal["sel_fg"],
                         highlightthickness=1, highlightbackground=pal["border"],
                         highlightcolor=pal["sel_bg"], relief="flat")
        self.desc.configure(font="TkTextFont", **text_opts)
        self.report.configure(font="TkTextFont", **text_opts)
        list_opts = {k: v for k, v in text_opts.items() if k != "insertbackground"}  # у Listbox нет курсора
        self.links_list.configure(font="TkDefaultFont", **list_opts)
        # окна, которые будут созданы позже (диалоги, напоминания)
        for pattern, value in (
            ("*Toplevel.background", pal["bg"]), ("*Frame.background", pal["bg"]),
            ("*Label.background", pal["bg"]), ("*Label.foreground", pal["fg"]),
            ("*Button.background", pal["button"]), ("*Button.foreground", pal["fg"]),
            ("*Entry.background", pal["field"]), ("*Entry.foreground", pal["fg"]),
            ("*Entry.insertBackground", pal["fg"]),
            ("*Text.background", pal["field"]), ("*Text.foreground", pal["fg"]),
            ("*Text.insertBackground", pal["fg"]), ("*Text.selectBackground", pal["sel_bg"]),
            ("*Text.selectForeground", pal["sel_fg"]), ("*Text.font", "TkTextFont"),
            ("*Listbox.background", pal["field"]), ("*Listbox.foreground", pal["fg"]),
            ("*Listbox.selectBackground", pal["sel_bg"]), ("*Listbox.selectForeground", pal["sel_fg"]),
            ("*TCombobox*Listbox.background", pal["field"]), ("*TCombobox*Listbox.foreground", pal["fg"]),
            ("*TCombobox*Listbox.selectBackground", pal["sel_bg"]),
            ("*TCombobox*Listbox.selectForeground", pal["sel_fg"]),
            ("*Menu.background", pal["field"]), ("*Menu.foreground", pal["fg"]),
            ("*Menu.activeBackground", pal["sel_bg"]), ("*Menu.activeForeground", pal["sel_fg"]),
        ):
            self.root.option_add(pattern, value, "interactive")
        for tree in self.trees.values():
            tree.tag_configure("done", font=self.done_font, foreground=pal["done"])
            tree.tag_configure("overdue", foreground=pal["overdue"])
            tree.tag_configure("duetoday", foreground=pal["today"])
            tree.tag_configure("failed", foreground=pal["overdue"])
        self.meta_label.configure(foreground=pal["muted"])
        self.status.configure(foreground=pal["muted"])
        try:
            if self._view_win is not None and self._view_win.winfo_exists():
                self._view_win.configure(bg=pal["bg"])
        except tk.TclError:
            pass
        if self._stats_alive():
            self._stats_win.configure(bg=pal["bg"])
            self._stats_bar.configure(bg=pal["bg"])
            self._render_stats()

    @staticmethod
    def _style_clam(style, p):
        """Полная раскраска ttk в теме clam (она единственная из стандартных поддерживает свои цвета везде)."""
        style.theme_use("clam")
        style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                        bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                        troughcolor=p["bg"], insertcolor=p["fg"],
                        selectbackground=p["sel_bg"], selectforeground=p["sel_fg"])
        style.configure("TButton", background=p["button"], lightcolor=p["button"], darkcolor=p["button"],
                        padding=(8, 3))
        style.map("TButton", background=[("disabled", p["bg"]), ("pressed", p["button_active"]),
                                         ("active", p["button_active"])],
                  foreground=[("disabled", p["muted"])])
        for entry in ("TEntry", "TSpinbox"):
            style.configure(entry, fieldbackground=p["field"], foreground=p["fg"], insertcolor=p["fg"],
                            lightcolor=p["field"], darkcolor=p["field"], arrowcolor=p["fg"],
                            background=p["button"])
            style.map(entry, fieldbackground=[("disabled", p["bg"])], foreground=[("disabled", p["muted"])])
        style.configure("TCombobox", fieldbackground=p["field"], background=p["button"], foreground=p["fg"],
                        arrowcolor=p["fg"], lightcolor=p["field"], darkcolor=p["field"], insertcolor=p["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
                  foreground=[("disabled", p["muted"])],
                  selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])])
        style.configure("TNotebook", background=p["bg"], lightcolor=p["bg"], darkcolor=p["bg"])
        style.configure("TNotebook.Tab", background=p["button"], foreground=p["fg"],
                        lightcolor=p["button"], darkcolor=p["button"], padding=(10, 4))
        style.map("TNotebook.Tab", background=[("selected", p["field"]), ("active", p["button_active"])])
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["fg"],
                        lightcolor=p["field"], darkcolor=p["field"])
        style.map("Treeview", background=[("selected", p["sel_bg"])], foreground=[("selected", p["sel_fg"])])
        style.configure("TScrollbar", background=p["button"], arrowcolor=p["fg"],
                        lightcolor=p["button"], darkcolor=p["button"])
        style.map("TScrollbar", background=[("active", p["button_active"])])
        for check in ("TCheckbutton", "TRadiobutton"):
            style.configure(check, indicatorbackground=p["field"], indicatorforeground=p["sel_fg"])
            style.map(check, background=[("active", p["bg"])],
                      indicatorbackground=[("selected", p["sel_bg"]), ("!selected", p["field"])])

    def open_view_settings(self):
        try:
            if self._view_win is not None and self._view_win.winfo_exists():
                self._view_win.lift()
                return
        except tk.TclError:
            pass
        win = tk.Toplevel(self.root)
        self._view_win = win
        win.title("Вид")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg=self.pal["bg"])

        family, size = self._font_settings()
        v_family = tk.StringVar(value=family)
        v_size = tk.StringVar(value=str(size))
        v_theme = tk.StringVar(value=self.settings.get("theme", "system"))
        archive_value = dict(ARCHIVE_CHOICES)
        archive_name = {value: name for name, value in ARCHIVE_CHOICES}
        v_archive = tk.StringVar(value=archive_name[self._archive_setting()])
        families = sorted({f for f in tkfont.families(self.root) if not f.startswith("@")}, key=str.casefold)

        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Шрифт:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(body, textvariable=v_family, values=families, state="readonly",
                     width=32).grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ttk.Label(body, text="Размер:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(body, from_=MIN_FONT, to=MAX_FONT, textvariable=v_size,
                    width=6).grid(row=1, column=1, sticky="w", padx=(10, 0))
        ttk.Label(body, text="Тема:").grid(row=2, column=0, sticky="nw", pady=4)
        themes = ttk.Frame(body)
        themes.grid(row=2, column=1, sticky="w", padx=(10, 0))
        for text, value in THEME_CHOICES:
            ttk.Radiobutton(themes, text=text, value=value, variable=v_theme).pack(anchor="w", pady=1)
        ttk.Label(body, text="В архив:").grid(row=3, column=0, sticky="w", pady=(10, 4))
        ttk.Combobox(body, textvariable=v_archive, values=[name for name, _ in ARCHIVE_CHOICES],
                     state="readonly", width=32).grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Label(body, text="Выполненные и проваленные задачи уходят из категорий во вкладку «Архив».",
                  foreground=self.pal["muted"], wraplength=380, justify="left"
                  ).grid(row=4, column=0, columnspan=2, sticky="w")

        def changed(*_):
            try:
                new_size = int(v_size.get())
            except ValueError:
                return  # пока вводят число
            if not MIN_FONT <= new_size <= MAX_FONT:
                return
            self.settings.update(font_family=v_family.get(), font_size=new_size, theme=v_theme.get(),
                                 archive_days=archive_value[v_archive.get()])
            save_settings(self.settings)
            self.apply_appearance()
            self.save_form(silent=True)  # правки карточки не должны потеряться при перерисовке списка
            self.refresh_keep()          # срок архивации мог измениться

        for var in (v_family, v_size, v_theme, v_archive):
            var.trace_add("write", changed)

        def reset():
            v_family.set(self._default_family)
            v_size.set(str(self._default_size))
            v_theme.set("system")
            v_archive.set(archive_name[DEFAULT_ARCHIVE_DAYS])

        bar = ttk.Frame(body)
        bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(bar, text="По умолчанию", command=reset).pack(side="left")
        ttk.Button(bar, text="Закрыть", command=win.destroy).pack(side="right")

    # ------------------------------------------------------------ дерево
    def current_tab(self):
        return TAB_KEYS[self.nb.index(self.nb.select())]

    def current_cat(self):
        """Категория для новых задач: активная вкладка, а на вкладке «Архив» — «Сегодня»."""
        key = self.current_tab()
        return key if key in CATEGORIES else "today"

    def selected_id(self):
        sel = self.trees[self.current_tab()].selection()
        return sel[0] if sel else None

    # ---- архив
    def _archive_setting(self):
        v = self.settings.get("archive_days", DEFAULT_ARCHIVE_DAYS)
        valid = {value for _, value in ARCHIVE_CHOICES}
        return v if (isinstance(v, int) and not isinstance(v, bool) and v in valid) else DEFAULT_ARCHIVE_DAYS

    def _archive_days(self):
        v = self._archive_setting()
        return None if v < 0 else v  # None — архив выключен

    def _root_id(self, t):
        for _ in range(100):  # защита от битых данных с циклом
            parent = self.store.tasks.get(t["parent_id"]) if t["parent_id"] else None
            if parent is None:
                break
            t = parent
        return t["id"]

    def _tab_for(self, t):
        """Вкладка, на которой сейчас показывается задача."""
        return ARCHIVE_KEY if self._root_id(t) in self._archived else t["category"]

    def refresh(self):
        self._loading = True
        visible = self.store.search(self.q_var.get(), self.hide_done_var.get())
        self._visible = visible
        self._archived = self.store.archived_ids(self._archive_days())
        alive = self.store.alive()
        for i, key in enumerate(CAT_KEYS):
            tree = self.trees[key]
            tree.delete(*tree.get_children())
            for t in self.store.roots(key, exclude=self._archived):
                self._insert(tree, "", t)
            in_cat = [t for t in alive if t["category"] == key and (visible is None or t["id"] in visible)]
            left = sum(1 for t in in_cat if not t["done"])
            overdue = sum(1 for t in in_cat if due_status(t) == "overdue")
            label = f"{CATEGORIES[key]} ({left})" + (f" ⚠{overdue}" if overdue else "")
            self.nb.tab(i, text=label)

        # архив: закрытые задачи, сгруппированные по месяцу закрытия
        tree = self.trees[ARCHIVE_KEY]
        tree.delete(*tree.get_children())
        groups = {}
        for t in self.store.archive_roots(self._archived):
            if visible is not None and t["id"] not in visible:
                continue
            cd = closed_date(t)
            groups.setdefault((cd.year, cd.month) if cd else (0, 0), []).append(t)
        shown = 0
        for (y, m), items in sorted(groups.items(), reverse=True):
            header = f"month:{y}-{m}"  # заголовок месяца: не задача, выбрать его нельзя в смысле карточки
            tree.insert("", "end", iid=header, open=True,
                        text=(f"{MONTHS[m - 1]} {y}" if y else "Без даты") + f"  ({len(items)})")
            for t in items:
                self._insert(tree, header, t, archive=True)
            shown += len(items)
        self.nb.tab(len(CAT_KEYS), text=f"Архив ({shown})")
        self._loading = False
        self._render_stats()  # если окно отчёта открыто — обновить цифры

    def _insert(self, tree, parent, t, archive=False):
        if self._visible is not None and t["id"] not in self._visible:
            return
        text = self._mark(t) + t["title"]
        if archive:
            cd = closed_date(t)
            text += f"   · {CATEGORIES[t['category']]}" + (f", {cd:%d.%m.%y}" if cd else "")
        if t["due"]:
            text += f"   📅 {short_date(t['due'])}"
        if t["remind"] and not t["reminded"] and not t["done"]:
            text += f"   ⏰ {short_remind(t['remind'])}"
        if t["links"]:
            text += "   🔗"
        status = due_status(t)
        if t["failed"]:
            tag = "failed"
        elif t["done"]:
            tag = "done"
        else:
            tag = {"overdue": "overdue", "today": "duetoday"}.get(status)
        tree.insert(parent, "end", iid=t["id"], text=text, open=True, tags=(tag,) if tag else ())
        for c in self.store.children(t["id"]):
            self._insert(tree, t["id"], c)

    @staticmethod
    def _mark(t):
        return "✘ " if t["failed"] else ("✔ " if t["done"] else "○ ")

    def goto(self, tid, clear_filter=True):
        t = self.store.get(tid)
        if not t:
            return
        tree = self.trees[self._tab_for(t)]
        if not tree.exists(tid):
            if not clear_filter:
                return
            self._clear_filter()  # задача скрыта поиском — сбрасываем фильтр
        key = self._tab_for(t)  # после сброса фильтра архив мог измениться
        tree = self.trees[key]
        self.nb.select(TAB_KEYS.index(key))
        tree.selection_set(tid)
        tree.focus(tid)
        tree.see(tid)

    def _clear_filter(self):
        self._suppress_filter = True
        self.q_var.set("")
        self.hide_done_var.set(False)
        self._suppress_filter = False
        self.refresh()

    def refresh_keep(self):
        cur = self.current_id
        self.refresh()
        t = self.store.get(cur) if cur else None
        if t:
            key = self._tab_for(t)
            if key == ARCHIVE_KEY and self.current_tab() != ARCHIVE_KEY:
                t = None  # задачу только что убрали в архив: на текущей вкладке её больше нет
            elif not self.trees[key].exists(cur):
                t = None  # скрыта поиском
        if t:
            self.goto(cur, clear_filter=False)
            self.load_form(cur)
        else:
            self.load_form(None)

    def on_filter_change(self, *_):
        if self._suppress_filter:
            return
        self.save_form()
        self.refresh_keep()

    def on_select(self, _event=None):
        if self._loading:
            return
        self.save_form()
        self.load_form(self.selected_id())

    # ------------------------------------------------------------ карточка
    def _set_form_enabled(self, enabled, is_sub=False):
        st = "normal" if enabled else "disabled"
        for w in (self.title_entry, self.desc, self.report, self.due_entry, self.remind_entry):
            w.configure(state=st)
        self.cat_box.configure(state="readonly" if enabled else "disabled")
        for b in (self.done_btn, self.fail_btn, self.save_btn, self.link_add_btn, self.link_del_btn,
                  *self.due_btns, *self.remind_btns):
            b.configure(state=st)

    def load_form(self, tid):
        t = self.store.get(tid) if tid else None
        self.current_id = t["id"] if t else None
        self._set_form_enabled(True, is_sub=bool(t and t["parent_id"]))  # чтобы можно было записать текст
        self.title_var.set(t["title"] if t else "")
        self.due_var.set(fmt_date(t["due"]) if t else "")
        self.remind_var.set(fmt_remind(t["remind"]) if t else "")
        for w, key in ((self.desc, "description"), (self.report, "report")):
            w.delete("1.0", "end")
            w.insert("1.0", t[key] if t else "")
        self.links_list.delete(0, "end")
        self._link_ids = []
        if not t:
            self.cat_var.set("")
            self.meta_label.configure(text="Выберите задачу слева или создайте новую")
            self.done_btn.configure(text="✔ Завершить")
            self._set_form_enabled(False)
            return
        self.cat_var.set(CATEGORIES[t["category"]])
        meta = f"Создана: {fmt_dt(t['created'])}"
        if t["failed"]:
            meta += f"   ·   ✘ Провалена: {fmt_dt(t['done_at'])}"
        elif t["done"]:
            meta += f"   ·   Завершена: {fmt_dt(t['done_at'])}"
        status = due_status(t)
        if status == "overdue":
            meta += "   ·   ⚠ просрочена"
        parent = self.store.get(t["parent_id"]) if t["parent_id"] else None
        if parent:
            meta = f"Подзадача «{parent['title']}»   ·   " + meta
        self.meta_label.configure(text=meta)
        self.done_btn.configure(text="↺ Вернуть в работу" if t["done"] else "✔ Завершить")
        for lid in t["links"]:
            lt = self.store.get(lid)
            if lt:
                self._link_ids.append(lid)
                self.links_list.insert("end", self._mark(lt) + self._path_title(lt)
                                       + f"   [{CATEGORIES[lt['category']]}]")
        self._set_form_enabled(True, is_sub=bool(parent))
        self.fail_btn.configure(state="disabled" if t["failed"] else "normal")

    def save_form(self, silent=False):
        """Сохраняет форму, только если что-то реально изменилось.
        silent=True — не показывать окна с ошибками (для фоновых вызовов)."""
        tid = self.current_id
        t = self.store.get(tid) if tid else None
        if not t:
            return
        title = self.title_var.get().strip() or t["title"]
        desc = self.desc.get("1.0", "end-1c")
        report = self.report.get("1.0", "end-1c")
        cat_name = self.cat_var.get()
        category = CAT_KEYS[CAT_NAMES.index(cat_name)] if cat_name in CAT_NAMES else t["category"]
        if t["parent_id"]:
            category = t["category"]

        errors = []
        due, remind = t["due"], t["remind"]
        due_text = self.due_var.get().strip()
        if due_text != fmt_date(t["due"]):
            try:
                due = parse_date(due_text)
            except ValueError as e:
                errors.append(str(e))
        remind_text = self.remind_var.get().strip()
        if remind_text != fmt_remind(t["remind"]):
            try:
                remind = parse_datetime(remind_text)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            if not silent:
                messagebox.showerror("Неверный ввод", "\n\n".join(errors), parent=self.root)
            # некорректные поля возвращаем к сохранённому значению
            self.due_var.set(fmt_date(due))
            self.remind_var.set(fmt_remind(remind))

        changes = {}
        for key, new in (("title", title), ("description", desc), ("report", report),
                         ("category", category), ("due", due), ("remind", remind)):
            if new != t[key]:
                changes[key] = new
        if not changes:
            return
        relabel = bool(set(changes) & {"title", "category", "due", "remind"})
        self.store.update(tid, **changes)
        if relabel:
            self.refresh_keep()

    # ---- быстрые кнопки дедлайна и напоминания
    def quick_due(self, days):
        if not self.current_id:
            return
        self.due_var.set("" if days is None else fmt_date((date.today() + timedelta(days=days)).isoformat()))
        self.save_form()

    def pick_due(self):
        """Календарь для выбора дедлайна; прошедшие дни недоступны."""
        if not self.current_id:
            return
        initial = None
        try:
            iso = parse_date(self.due_var.get())
            initial = date.fromisoformat(iso) if iso else None
        except ValueError:
            pass  # в поле пока не дата — календарь откроется на текущем месяце
        pick_date(self.root, self.cal_btn, initial, date.today(), self.pal, self._on_due_picked)

    def _on_due_picked(self, day):
        if self.current_id:
            self.due_var.set(fmt_date(day.isoformat()))
            self.save_form()

    def quick_remind(self, hours=None, tomorrow_9=False):
        if not self.current_id:
            return
        if hours:
            value = (datetime.now() + timedelta(hours=hours)).replace(second=0, microsecond=0)
            self.remind_var.set(fmt_remind(value.strftime(REMIND_FMT)))
        elif tomorrow_9:
            self.remind_var.set("завтра 9:00")
        else:
            self.remind_var.set("")
        self.save_form()

    # ------------------------------------------------------------ действия
    def _path_title(self, t):
        p = self.store.get(t["parent_id"]) if t["parent_id"] else None
        return f"{p['title']} › {t['title']}" if p else t["title"]

    def add_task(self):
        self.save_form()
        title = simpledialog.askstring("Новая задача", "Название задачи:", parent=self.root)
        if title and title.strip():
            t = self.store.add(title.strip(), self.current_cat())
            self.refresh()
            self.goto(t["id"])
            self.load_form(t["id"])

    def add_subtask(self):
        self.save_form()
        pid = self.current_id
        if not pid:
            messagebox.showinfo("Подзадача", "Сначала выберите задачу, к которой добавить подзадачу.")
            return
        title = simpledialog.askstring("Новая подзадача", "Название подзадачи:", parent=self.root)
        if title and title.strip():
            t = self.store.add(title.strip(), None, parent_id=pid)
            self.refresh()
            self.goto(t["id"])
            self.load_form(t["id"])

    def delete_task(self):
        tid = self.current_id
        t = self.store.get(tid) if tid else None
        if not t:
            return
        n = len(self.store.descendants(tid))
        msg = f"Удалить задачу «{t['title']}»?"
        if n:
            msg += f"\nВместе с ней будут удалены подзадачи: {n}."
        if messagebox.askyesno("Удаление", msg, parent=self.root):
            self.current_id = None
            self.store.delete(tid)
            self.refresh()
            self.load_form(None)

    def toggle_done(self):
        self.save_form()
        tid = self.current_id
        t = self.store.get(tid) if tid else None
        if not t:
            return
        if t["done"]:
            self.store.set_done(tid, False)
        else:
            report = self._ask_report(t)
            if report is None:  # нажали «Отмена»
                return
            self.store.set_done(tid, True, report)
        self.refresh_keep()

    def mark_failed(self):
        self.save_form()
        tid = self.current_id
        t = self.store.get(tid) if tid else None
        if not t or t["failed"]:
            return
        report = self._ask_report(t, failed=True)
        if report is None:  # нажали «Отмена»
            return
        self.store.set_failed(tid, report)
        self.refresh_keep()

    def _ask_report(self, t, failed=False):
        """Окно с отчётом о завершении (или причиной провала). Возвращает текст или None при отмене."""
        win = tk.Toplevel(self.root)
        win.title("Провал задачи" if failed else "Завершение задачи")
        win.transient(self.root)
        heading = (f"Задача «{t['title']}» провалена.\nПричина, выводы (можно оставить пустым):" if failed
                   else f"Задача «{t['title']}» выполнена!\nОтчёт (можно оставить пустым):")
        ttk.Label(win, text=heading, padding=10).pack(anchor="w")
        text = tk.Text(win, wrap="word", height=8)
        text.pack(fill="both", expand=True, padx=10)
        text.insert("1.0", t["report"])
        text.focus_set()
        result = {"value": None}

        def ok():
            result["value"] = text.get("1.0", "end-1c")
            win.destroy()

        bar = ttk.Frame(win, padding=10)
        bar.pack(side="bottom", fill="x", before=text)  # кнопки занимают место первыми и не обрезаются
        ttk.Button(bar, text="Отметить проваленной" if failed else "Завершить", command=ok).pack(side="right")
        ttk.Button(bar, text="Отмена", command=win.destroy).pack(side="right", padx=6)
        self._fit_dialog(win, 460, 300)
        win.grab_set()
        self.root.wait_window(win)
        return result["value"]

    @staticmethod
    def _fit_dialog(win, min_w, min_h):
        """Размер окна подбирается под содержимое (зависит от выбранного шрифта),
        меньше необходимого окно сделать нельзя, увеличивать можно."""
        win.update_idletasks()
        need_w, need_h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.minsize(need_w, need_h)
        win.geometry(f"{max(min_w, need_w)}x{max(min_h, need_h)}")

    # ---- отчёт: статистика за период
    def _stats_alive(self):
        try:
            return self._stats_win is not None and self._stats_win.winfo_exists()
        except tk.TclError:
            return False

    def open_stats(self):
        if self._stats_alive():
            self._stats_win.lift()
            self._render_stats()
            return
        win = tk.Toplevel(self.root)
        self._stats_win = win
        win.title("Отчёт по задачам")
        win.transient(self.root)
        win.configure(bg=self.pal["bg"])
        self._stats_kind = tk.StringVar(value="Месяц")
        self._stats_offset = 0
        self._stats_subs = tk.BooleanVar(value=False)
        self._stats_num = {}
        self._stats = None

        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        # нижняя панель создаётся первой, чтобы кнопка «Закрыть» никогда не обрезалась
        bottom = ttk.Frame(body)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(bottom, text="Закрыть", command=win.destroy).pack(side="right")
        ttk.Label(bottom, wraplength=480, foreground=self.pal["muted"], justify="left",
                  text="В период задача попадает по дедлайну; если дедлайна нет — по дате закрытия "
                       "(для закрытых) или по дате создания (для открытых).").pack(side="left", fill="x", expand=True)

        ctl = ttk.Frame(body)
        ctl.pack(fill="x")
        ttk.Label(ctl, text="Период:").pack(side="left")
        kinds = ttk.Combobox(ctl, textvariable=self._stats_kind, values=[n for n, _ in STATS_KINDS],
                             state="readonly", width=10)
        kinds.pack(side="left", padx=6)
        kinds.bind("<<ComboboxSelected>>", lambda e: self._stats_shift(0, reset=True))
        self._stats_prev = ttk.Button(ctl, text="◀", width=3, command=lambda: self._stats_shift(-1))
        self._stats_prev.pack(side="left")
        self._stats_label = ttk.Label(ctl, font=self.font_title, anchor="center", width=24)
        self._stats_label.pack(side="left", padx=6)
        self._stats_next = ttk.Button(ctl, text="▶", width=3, command=lambda: self._stats_shift(1))
        self._stats_next.pack(side="left")
        ttk.Checkbutton(ctl, text="Считать подзадачи", variable=self._stats_subs,
                        command=self._render_stats).pack(side="right")

        nums = ttk.Frame(body)
        nums.pack(fill="x", pady=(14, 6))
        for col, (key, title) in enumerate((("total", "Всего"), ("done", "Выполнено"),
                                            ("failed", "Провалено"), ("open", "В работе"))):
            nums.columnconfigure(col, weight=1, uniform="stat")
            cell = ttk.Frame(nums)
            cell.grid(row=0, column=col, sticky="ew")
            number = ttk.Label(cell, text="0", font=self.font_header)
            number.pack()
            ttk.Label(cell, text=title).pack()
            pct = ttk.Label(cell, text="")
            pct.pack()
            self._stats_num[key] = (number, pct)

        self._stats_bar = tk.Canvas(body, height=18, highlightthickness=0, bd=0, bg=self.pal["bg"])
        self._stats_bar.pack(fill="x", pady=(2, 6))
        self._stats_bar.bind("<Configure>", lambda e: self._draw_stats_bar())
        self._stats_info = ttk.Label(body, text="", wraplength=600, justify="left")
        self._stats_info.pack(anchor="w")

        ttk.Label(body, text="По категориям", font=self.font_title).pack(anchor="w", pady=(12, 2))
        cats = ttk.Treeview(body, columns=("total", "done", "failed", "open"), height=len(CAT_KEYS) + 1,
                            selectmode="none")
        cats.heading("#0", text="Категория")
        cats.column("#0", width=190)
        for col, title in (("total", "Всего"), ("done", "Выполнено"), ("failed", "Провалено"), ("open", "В работе")):
            cats.heading(col, text=title)
            cats.column(col, width=95, anchor="center")
        cats.pack(fill="x")
        self._stats_cats = cats

        ttk.Label(body, text="Проваленные задачи (двойной щелчок — открыть)",
                  font=self.font_title).pack(anchor="w", pady=(12, 2))
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True)
        failed = ttk.Treeview(wrap, columns=("date", "reason"), height=6, selectmode="browse")
        failed.heading("#0", text="Задача")
        failed.heading("date", text="Дата")
        failed.heading("reason", text="Причина")
        failed.column("#0", width=220)
        failed.column("date", width=90, anchor="center")
        failed.column("reason", width=300)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=failed.yview)
        failed.configure(yscrollcommand=sb.set)
        failed.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        failed.bind("<Double-Button-1>", self._stats_open_failed)
        self._stats_failed = failed

        self._render_stats()
        self._fit_dialog(win, 700, 640)

    def _stats_shift(self, delta, reset=False):
        self._stats_offset = 0 if reset else self._stats_offset + delta
        self._render_stats()

    def _render_stats(self):
        if not self._stats_alive():
            return
        kind = dict(STATS_KINDS)[self._stats_kind.get()]
        start, end, label = period_range(kind, self._stats_offset)
        self._stats_label.configure(text=label)
        nav = "disabled" if kind == "all" else "normal"
        self._stats_prev.configure(state=nav)
        self._stats_next.configure(state=nav)

        d = compute_report(self.store.tasks.values(), start, end, self._stats_subs.get())
        self._stats = d
        total = d["total"]
        colors = {"done": self.pal["ok"], "failed": self.pal["overdue"]}
        for key, (number, pct) in self._stats_num.items():
            number.configure(text=str(d[key]), foreground=colors.get(key, self.pal["fg"]))
            pct.configure(text="" if key == "total" else (f"{round(100 * d[key] / total)}%" if total else "—"))

        if not total:
            info = "В этом периоде задач нет."
        elif d["success_rate"] is None:
            info = "Закрытых задач в этом периоде пока нет."
        else:
            info = (f"Успешность: {d['success_rate']}% — выполнено {d['done']} "
                    f"из {d['done'] + d['failed']} закрытых.")
        if d["overdue"]:
            info += f"  Просрочено в работе: {d['overdue']}."
        self._stats_info.configure(text=info)

        cats = self._stats_cats
        cats.delete(*cats.get_children())
        for key in CAT_KEYS:
            c = d["by_category"][key]
            cats.insert("", "end", iid=key, text=CATEGORIES[key],
                        values=(c["total"], c["done"], c["failed"], c["open"]))
        cats.insert("", "end", iid="_total", text="Итого",
                    values=(total, d["done"], d["failed"], d["open"]))

        failed = self._stats_failed
        failed.delete(*failed.get_children())
        for t in d["failed_tasks"]:
            when = datetime.fromisoformat(t["done_at"]).astimezone().strftime("%d.%m.%Y") if t["done_at"] else ""
            reason = " ".join(t["report"].split())
            if len(reason) > 90:
                reason = reason[:89] + "…"
            failed.insert("", "end", iid=t["id"], text=self._path_title(t), values=(when, reason))
        self._draw_stats_bar()

    def _draw_stats_bar(self):
        """Полоса: зелёное — выполнено, красное — провалено, серое — в работе."""
        d = getattr(self, "_stats", None)
        if not d or not self._stats_alive():
            return
        c = self._stats_bar
        c.delete("all")
        width, height = c.winfo_width(), 18
        if width <= 1:
            return
        total = d["total"]
        if not total:
            c.create_rectangle(0, 0, width, height, fill=self.pal["border"], outline="")
            return
        x = 0.0
        for key, color in (("done", self.pal["ok"]), ("failed", self.pal["overdue"]), ("open", self.pal["border"])):
            seg = width * d[key] / total
            if d[key]:
                c.create_rectangle(x, 0, x + seg, height, fill=color, outline="")
            x += seg

    def _stats_open_failed(self, _event=None):
        sel = self._stats_failed.selection()
        if sel:
            self.root.deiconify()
            self.goto(sel[0])

    # ---- перенос между категориями
    def _fill_move_menu(self, menu):
        menu.delete(0, "end")
        t = self.store.get(self.current_id) if self.current_id else None
        for key in CAT_KEYS:
            here = t is not None and t["category"] == key
            menu.add_command(label=CATEGORIES[key] + ("  (сейчас здесь)" if here else ""),
                             state="disabled" if (t is None or here) else "normal",
                             command=lambda k=key: self.move_current(k))

    def on_tree_context(self, event):
        tree = event.widget
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        self.on_select()  # сохранить прежнюю задачу и открыть выбранную
        menu = tk.Menu(self.root, tearoff=0)
        self._fill_move_menu(menu)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def on_category_selected(self):
        name = self.cat_var.get()
        if name in CAT_NAMES:
            self.move_current(CAT_KEYS[CAT_NAMES.index(name)])

    def move_current(self, category):
        tid = self.current_id
        t = self.store.get(tid) if tid else None
        if not t:
            return
        self.cat_var.set(CATEGORIES[t["category"]])  # откат списка; реальное значение выставит перенос
        if t["category"] == category:
            return
        self.save_form()
        if t["parent_id"]:
            parent = self.store.get(t["parent_id"])
            if not messagebox.askyesno(
                    "Перенос подзадачи",
                    f"«{t['title']}» — подзадача" + (f" задачи «{parent['title']}»" if parent else "") +
                    f".\nПри переносе она станет самостоятельной задачей в категории «{CATEGORIES[category]}».\n\n"
                    "Продолжить?", parent=self.root):
                return
        self.store.move_task(tid, category)
        self.refresh_keep()  # откроет вкладку новой категории с перенесённой задачей
        self.status.configure(text=f"Перенесено: «{CATEGORIES[category]}»")

    # ---- связи
    def add_link(self):
        self.save_form()
        tid = self.current_id
        t = self.store.get(tid) if tid else None
        if not t:
            return
        candidates = [x for x in self.store.alive() if x["id"] != tid and x["id"] not in t["links"]]
        candidates.sort(key=lambda x: (CAT_KEYS.index(x["category"]), x["created"]))
        if not candidates:
            messagebox.showinfo("Связи", "Нет задач, с которыми можно связать.")
            return
        win = tk.Toplevel(self.root)
        win.title("Связать с задачей")
        win.transient(self.root)
        ttk.Label(win, text="Выберите задачу (двойной щелчок):", padding=10).pack(anchor="w")
        lb = tk.Listbox(win, activestyle="none")
        lb.pack(fill="both", expand=True, padx=10)
        for x in candidates:
            lb.insert("end", f"[{CATEGORIES[x['category']]}] {self._path_title(x)}")

        def ok(_e=None):
            sel = lb.curselection()
            if sel:
                self.store.link(tid, candidates[sel[0]]["id"])
                win.destroy()

        lb.bind("<Double-Button-1>", ok)
        bar = ttk.Frame(win, padding=10)
        bar.pack(side="bottom", fill="x", before=lb)
        ttk.Button(bar, text="Связать", command=ok).pack(side="right")
        ttk.Button(bar, text="Отмена", command=win.destroy).pack(side="right", padx=6)
        self._fit_dialog(win, 420, 340)
        win.grab_set()
        self.root.wait_window(win)
        self.refresh_keep()

    def remove_link(self):
        sel = self.links_list.curselection()
        if sel and self.current_id:
            self.store.unlink(self.current_id, self._link_ids[sel[0]])
            self.refresh_keep()

    def open_link(self, _event=None):
        sel = self.links_list.curselection()
        if sel:
            self.goto(self._link_ids[sel[0]])  # сработает on_select и откроет задачу

    # ------------------------------------------------------------ напоминания
    def _tick(self):
        """Раз в 30 секунд: показывает сработавшие напоминания, обновляет подсветку при смене суток."""
        try:
            changed = False
            if date.today() != self._last_day:
                self._last_day = date.today()
                changed = True  # просрочка и «сегодня» пересчитаются при обновлении дерева
            for i, t in enumerate(self.store.due_reminders()[:MAX_REMINDER_WINDOWS]):
                self.store.mark_reminded(t["id"])  # пометка до показа: окно не повторится
                self._show_reminder(t["id"], i)
                changed = True
            if changed:
                self.save_form(silent=True)
                self.refresh_keep()
        except Exception as e:  # сбой напоминаний не должен ронять приложение
            self.status.configure(text=f"Ошибка напоминаний: {e}")
        finally:
            self.root.after(REMINDER_CHECK_MS, self._tick)

    def _show_reminder(self, tid, index):
        t = self.store.get(tid)
        if not t:
            return
        win = tk.Toplevel(self.root)
        win.title("Напоминание")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        ttk.Label(win, text="⏰ Напоминание", font=self.font_title,
                  padding=(16, 14, 16, 0)).pack(anchor="w")
        ttk.Label(win, text=self._path_title(t), wraplength=380, font=self.font_big,
                  padding=(16, 8, 16, 0)).pack(anchor="w")
        info = f"Запланировано на {fmt_remind(t['remind'])}"
        if t["due"]:
            info += f"\nДедлайн: {fmt_date(t['due'])}"
        ttk.Label(win, text=info, foreground=self.pal["muted"], padding=(16, 6, 16, 0)).pack(anchor="w")

        def close():
            win.destroy()

        def open_task():
            win.destroy()
            self.root.deiconify()
            self.root.lift()
            self.goto(tid)

        def snooze(minutes):
            self.save_form(silent=True)  # сначала сохранить форму, иначе устаревший текст затрёт отсрочку
            self.store.snooze(tid, minutes)
            win.destroy()
            self.refresh_keep()

        bar = ttk.Frame(win, padding=16)
        bar.pack(fill="x")
        ttk.Button(bar, text="Открыть задачу", command=open_task).pack(side="left")
        ttk.Button(bar, text="Через 10 мин", command=lambda: snooze(10)).pack(side="left", padx=4)
        ttk.Button(bar, text="Через 1 ч", command=lambda: snooze(60)).pack(side="left")
        ttk.Button(bar, text="Закрыть", command=close).pack(side="right")

        win.update_idletasks()
        x = self.root.winfo_rootx() + 80 + index * 30
        y = self.root.winfo_rooty() + 80 + index * 30
        win.geometry(f"+{x}+{y}")
        win.lift()
        self.root.bell()
        if self._window_hidden():
            self.tray.notify("Напоминание", self._path_title(t))

    # ------------------------------------------------------------ трей
    def _window_hidden(self):
        try:
            return self.root.state() in ("withdrawn", "iconic")
        except tk.TclError:
            return False

    def hide_to_tray(self):
        self.save_form(silent=True)
        self.root.withdraw()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self):
        self.save_form(silent=True)
        self.tray.stop()
        self.root.destroy()

    # ------------------------------------------------------------ синхронизация
    def do_sync(self):
        self.save_form()
        cred = gdrive_sync.find_credentials(DATA_DIR, APP_DIR)
        if not cred:
            messagebox.showinfo(
                "Синхронизация",
                "Не найден файл credentials.json.\n\n"
                "Как его получить — в README.md (раздел «Google Диск»).\n"
                f"Положите файл в папку:\n{DATA_DIR}\nили рядом с todo_app.py.",
            )
            return
        self.sync_btn.configure(state="disabled")
        self.status.configure(text="Синхронизация… (может открыться браузер)")
        snapshot = copy.deepcopy(self.store.tasks)

        def worker():
            try:
                merged = gdrive_sync.sync(snapshot, DATA_DIR, cred, merge_tasks)
                self.sync_queue.put(("ok", merged))
            except Exception as e:  # показываем любую ошибку пользователю
                self.sync_queue.put(("err", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_sync(self):
        try:
            while True:
                cmd = self.tray_queue.get_nowait()
                if cmd == "show":
                    self.show_window()
                elif cmd == "quit":
                    self.quit_app()
                    return  # окно уничтожено, дальше опрос не нужен
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.sync_queue.get_nowait()
                self.sync_btn.configure(state="normal")
                if kind == "ok":
                    self.save_form()
                    self.store.replace_with_merge(payload)  # учтёт правки, сделанные за время синхронизации
                    self.refresh_keep()
                    self.status.configure(text=f"Синхронизировано в {datetime.now():%H:%M}")
                else:
                    self.status.configure(text="Ошибка синхронизации")
                    messagebox.showerror("Синхронизация", payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_sync)

    def on_close(self):
        """Крестик окна: при наличии трея — свернуть в трей, иначе выйти."""
        if self.tray.available:
            self.hide_to_tray()
        else:
            self.quit_app()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
