"""Календарь для выбора даты. Только стандартный tkinter, без сторонних библиотек.
Дни раньше min_date показаны серыми и недоступны для выбора."""
import calendar
import tkinter as tk
from datetime import date
from tkinter import ttk

from store import MONTHS

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def shift_month(year: int, month: int, delta: int):
    y, m0 = divmod(year * 12 + month - 1 + delta, 12)
    return y, m0 + 1


def can_go_prev(year: int, month: int, min_date: date) -> bool:
    """Есть ли смысл листать назад: в прошлых месяцах нет доступных дней."""
    return (year, month) > (min_date.year, min_date.month)


def month_grid(year, month, min_date, selected=None, today=None):
    """Сетка месяца: 6 недель по 7 дней, для стабильной высоты окна.
    Элемент — None (пустая ячейка) или (число, состояние);
    состояние: disabled (раньше min_date) | selected | today | normal."""
    today = today or date.today()
    weeks = calendar.Calendar(0).monthdayscalendar(year, month)  # неделя с понедельника
    while len(weeks) < 6:
        weeks.append([0] * 7)
    grid = []
    for week in weeks:
        row = []
        for d in week:
            if not d:
                row.append(None)
                continue
            day = date(year, month, d)
            if day < min_date:
                state = "disabled"
            elif selected is not None and day == selected:
                state = "selected"
            elif day == today:
                state = "today"
            else:
                state = "normal"
            row.append((d, state))
        grid.append(row)
    return grid


def pick_date(root, anchor, initial, min_date, pal, on_pick):
    """Открывает окно календаря под виджетом anchor.
    initial — уже выбранная дата (или None), min_date — самая ранняя доступная,
    pal — палитра темы, on_pick(date) вызывается при выборе дня."""
    today = date.today()
    selected = initial if (initial is not None and initial >= min_date) else None
    start = selected or max(today, min_date)
    view = {"y": start.year, "m": start.month}

    win = tk.Toplevel(root)
    win.title("Выбор даты")
    win.transient(root)
    win.resizable(False, False)
    win.configure(bg=pal["bg"])
    win.bind("<Escape>", lambda e: win.destroy())

    frame = tk.Frame(win, bg=pal["bg"], padx=10, pady=10)
    frame.pack()

    def choose(day):
        win.destroy()
        on_pick(day)

    # ---- заголовок: ◀ Месяц Год ▶
    head = tk.Frame(frame, bg=pal["bg"])
    head.pack(fill="x")
    prev_lbl = tk.Label(head, text="◀", bg=pal["bg"], fg=pal["fg"], width=3, pady=2)
    prev_lbl.pack(side="left")
    next_lbl = tk.Label(head, text="▶", bg=pal["bg"], fg=pal["fg"], width=3, pady=2, cursor="hand2")
    next_lbl.pack(side="right")
    title = tk.Label(head, text="", bg=pal["bg"], fg=pal["fg"], pady=2)
    title.pack(side="left", expand=True, fill="x")

    grid_frame = tk.Frame(frame, bg=pal["bg"])
    grid_frame.pack(pady=(6, 0))

    def go(delta):
        if delta < 0 and not can_go_prev(view["y"], view["m"], min_date):
            return
        view["y"], view["m"] = shift_month(view["y"], view["m"], delta)
        render()

    prev_lbl.bind("<Button-1>", lambda e: go(-1))
    next_lbl.bind("<Button-1>", lambda e: go(1))

    def render():
        y, m = view["y"], view["m"]
        title.configure(text=f"{MONTHS[m - 1]} {y}")
        enabled = can_go_prev(y, m, min_date)
        prev_lbl.configure(fg=pal["fg"] if enabled else pal["muted"], cursor="hand2" if enabled else "")
        for w in grid_frame.winfo_children():
            w.destroy()
        for c, name in enumerate(WEEKDAYS):
            tk.Label(grid_frame, text=name, bg=pal["bg"], fg=pal["muted"], width=4).grid(row=0, column=c)
        for r, row in enumerate(month_grid(y, m, min_date, selected, today), start=1):
            for c, cell in enumerate(row):
                if cell is None:
                    tk.Label(grid_frame, text="", bg=pal["bg"], width=4).grid(row=r, column=c)
                    continue
                d, state = cell
                bg, fg, edge = pal["bg"], pal["fg"], pal["bg"]
                if state == "disabled":
                    fg = pal["muted"]
                elif state == "selected":
                    bg, fg, edge = pal["sel_bg"], pal["sel_fg"], pal["sel_bg"]
                elif state == "today":
                    edge = pal["sel_bg"]  # рамка вокруг сегодняшнего дня
                lbl = tk.Label(grid_frame, text=str(d), width=4, pady=3, bg=bg, fg=fg,
                               highlightthickness=1, highlightbackground=edge)
                lbl.grid(row=r, column=c, padx=1, pady=1)
                if state != "disabled":
                    lbl.configure(cursor="hand2")
                    day = date(y, m, d)
                    lbl.bind("<Button-1>", lambda e, day=day: choose(day))
                    if state != "selected":
                        lbl.bind("<Enter>", lambda e, w=lbl: w.configure(bg=pal["button_active"]))
                        lbl.bind("<Leave>", lambda e, w=lbl, b=bg: w.configure(bg=b))

    render()

    bar = tk.Frame(frame, bg=pal["bg"])
    bar.pack(fill="x", pady=(10, 0))
    ttk.Button(bar, text="Сегодня", command=lambda: choose(max(today, min_date))).pack(side="left")
    ttk.Button(bar, text="Закрыть", command=win.destroy).pack(side="right")

    # расположить под кнопкой, не выходя за экран
    win.update_idletasks()
    x = anchor.winfo_rootx()
    y = anchor.winfo_rooty() + anchor.winfo_height() + 2
    x = max(0, min(x, win.winfo_screenwidth() - win.winfo_reqwidth()))
    y = max(0, min(y, win.winfo_screenheight() - win.winfo_reqheight()))
    win.geometry(f"+{x}+{y}")
    try:
        win.wait_visibility()
        win.grab_set()  # пока календарь открыт, главное окно не реагирует
    except tk.TclError:
        pass
    win.focus_set()
    return win
