"""Хранилище задач: модель данных, подзадачи, связи, сроки, напоминания, поиск,
слияние для синхронизации."""
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CATEGORIES = {
    "today": "Сегодня",
    "week": "На этой неделе",
    "month": "В этом месяце",
    "year": "В этом году",
}

# Поля, добавленные позже: старые файлы и данные с других устройств их не содержат
FIELD_DEFAULTS = {
    "due": None,         # срок выполнения, "YYYY-MM-DD"
    "remind": None,      # время напоминания (локальное), "YYYY-MM-DD HH:MM"
    "reminded": False,   # напоминание уже показано
    "failed": False,     # закрыта как проваленная (при этом done=True)
}

DATE_FMT = "%d.%m.%Y"
DT_FMT = "%d.%m.%Y %H:%M"
REMIND_FMT = "%Y-%m-%d %H:%M"


def now_iso() -> str:
    # Единый формат, строки можно сравнивать напрямую
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize(t: dict) -> dict:
    for k, v in FIELD_DEFAULTS.items():
        t.setdefault(k, v)
    return t


# ---------------------------------------------------------------- даты
_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def parse_date(text: str, today: date | None = None) -> str | None:
    """Разбирает дату из ввода пользователя. Пустая строка -> None.
    Понимает: 05.11.2026, 05.11.26, 05.11, 2026-11-05, сегодня, завтра, послезавтра, +3 (дня)."""
    today = today or date.today()
    s = text.strip().lower()
    if not s:
        return None
    if s in _WORDS:
        return (today + timedelta(days=_WORDS[s])).isoformat()
    m = re.fullmatch(r"\+(\d{1,4})", s)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?", s)
        if not m:
            raise ValueError(f"Не удалось разобрать дату «{text}». Пример: 05.11.2026, завтра, +3")
        d, mo = int(m.group(1)), int(m.group(2))
        y = m.group(3)
        y = today.year if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        raise ValueError(f"Такой даты не существует: «{text}»") from None


def parse_datetime(text: str, now: datetime | None = None) -> str | None:
    """Время напоминания. Пустая строка -> None.
    Примеры: 05.11.2026 18:30, 05.11 18:30, завтра 9:00, 18:30 (сегодня, а если уже прошло — завтра)."""
    now = now or datetime.now()
    s = text.strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(?:(.+?)\s+)?(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError(f"Не удалось разобрать время «{text}». Пример: 05.11.2026 18:30, завтра 9:00, 18:30")
    date_part, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    if hh > 23 or mm > 59:
        raise ValueError(f"Неверное время: «{text}»")
    if date_part:
        day = date.fromisoformat(parse_date(date_part, now.date()))
    else:
        day = now.date()
    result = datetime(day.year, day.month, day.day, hh, mm)
    if not date_part and result <= now:
        result += timedelta(days=1)
    return result.strftime(REMIND_FMT)


def fmt_date(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime(DATE_FMT) if iso else ""


def fmt_remind(s: str | None) -> str:
    return datetime.strptime(s, REMIND_FMT).strftime(DT_FMT) if s else ""


def due_status(t: dict, today: date | None = None) -> str | None:
    """'overdue' — срок прошёл, 'today' — срок сегодня, иначе None. Для завершённых — None."""
    if t.get("done") or not t.get("due"):
        return None
    today = (today or date.today()).isoformat()
    if t["due"] < today:
        return "overdue"
    if t["due"] == today:
        return "today"
    return None


MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def period_range(kind: str, offset: int = 0, today: date | None = None):
    """Границы периода для отчёта: (начало, конец включительно, подпись).
    kind: day | week | month | year | all; offset — сдвиг в периодах (-1 = предыдущий).
    Для 'all' границы None."""
    today = today or date.today()
    if kind == "day":
        d = today + timedelta(days=offset)
        label = d.strftime(DATE_FMT) + (" (сегодня)" if offset == 0 else "")
        return d, d, label
    if kind == "week":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        end = start + timedelta(days=6)
        label = f"{start:%d.%m} – {end:%d.%m.%Y}" + (" (эта неделя)" if offset == 0 else "")
        return start, end, label
    if kind == "month":
        y, m0 = divmod(today.year * 12 + today.month - 1 + offset, 12)
        start = date(y, m0 + 1, 1)
        end = date(y + (m0 == 11), (m0 + 1) % 12 + 1, 1) - timedelta(days=1)
        return start, end, f"{MONTHS[m0]} {y}"
    if kind == "year":
        y = today.year + offset
        return date(y, 1, 1), date(y, 12, 31), f"{y} год"
    if kind == "all":
        return None, None, "За всё время"
    raise ValueError(f"Неизвестный период: {kind}")


def task_ref_date(t: dict) -> date:
    """К какому периоду относится задача: по дедлайну; если его нет — по дате закрытия
    (для закрытых) или по дате создания (для открытых)."""
    if t.get("due"):
        return date.fromisoformat(t["due"])
    src = t["done_at"] if t["done"] and t.get("done_at") else t["created"]
    return datetime.fromisoformat(src).astimezone().date()


def compute_report(tasks, start, end, include_subtasks=False, today: date | None = None) -> dict:
    """Статистика по задачам периода [start, end] (None = без границы).
    Статусы: выполнена (done и не failed), провалена (failed), в работе (остальные)."""
    today = today or date.today()
    res = {
        "total": 0, "done": 0, "failed": 0, "open": 0, "overdue": 0,
        "by_category": {k: {"total": 0, "done": 0, "failed": 0, "open": 0} for k in CATEGORIES},
        "failed_tasks": [],
    }
    for t in tasks:
        if t["deleted"] or (t["parent_id"] and not include_subtasks):
            continue
        ref = task_ref_date(t)
        if (start is not None and ref < start) or (end is not None and ref > end):
            continue
        status = "failed" if t.get("failed") else ("done" if t["done"] else "open")
        res["total"] += 1
        res[status] += 1
        cat = res["by_category"].get(t["category"])
        if cat is not None:
            cat["total"] += 1
            cat[status] += 1
        if status == "open" and due_status(t, today) == "overdue":
            res["overdue"] += 1
        if status == "failed":
            res["failed_tasks"].append(t)
    res["failed_tasks"].sort(key=task_ref_date)
    closed = res["done"] + res["failed"]
    res["success_rate"] = round(100 * res["done"] / closed) if closed else None
    return res


def merge_tasks(local: dict, remote: dict) -> dict:
    """Слияние двух наборов задач: для каждой задачи побеждает более новая правка.
    Удалённые задачи хранятся как «надгробия» (deleted=True), чтобы удаление тоже синхронизировалось."""
    merged = {}
    for tid in set(local) | set(remote):
        a, b = local.get(tid), remote.get(tid)
        if a is None:
            merged[tid] = normalize(b)
        elif b is None:
            merged[tid] = normalize(a)
        else:
            merged[tid] = normalize(a if a["updated"] >= b["updated"] else b)
    return merged


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.tasks: dict[str, dict] = {}
        self.load()

    # ---------- файл ----------
    def load(self):
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                self.tasks = json.load(f).get("tasks", {})
            for t in self.tasks.values():
                normalize(t)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasks": self.tasks}, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def replace_with_merge(self, remote_tasks: dict):
        self.tasks = merge_tasks(self.tasks, remote_tasks)
        self.save()

    # ---------- чтение ----------
    def get(self, tid):
        t = self.tasks.get(tid)
        return None if (t is None or t["deleted"]) else t

    def alive(self):
        return [t for t in self.tasks.values() if not t["deleted"]]

    @staticmethod
    def _order(t):
        # невыполненные выше; внутри — по сроку (без срока — в конце), затем по дате создания
        return (t["done"], t["due"] or "9999-12-31", t["created"])

    def roots(self, category):
        return sorted(
            (t for t in self.alive() if t["parent_id"] is None and t["category"] == category),
            key=self._order,
        )

    def children(self, tid):
        return sorted((t for t in self.alive() if t["parent_id"] == tid), key=self._order)

    def descendants(self, tid):
        out = []
        for c in self.children(tid):
            out.append(c["id"])
            out.extend(self.descendants(c["id"]))
        return out

    def search(self, query: str, hide_done: bool = False) -> set[str] | None:
        """Возвращает id задач, которые нужно показать: совпавшие + их родители.
        Все слова запроса должны встретиться в названии, описании или отчёте.
        Пустой запрос и hide_done=False -> None (фильтра нет)."""
        words = query.casefold().split()
        if not words and not hide_done:
            return None
        visible = set()
        for t in self.alive():
            if hide_done and t["done"]:
                continue
            hay = f"{t['title']}\n{t['description']}\n{t['report']}".casefold()
            if all(w in hay for w in words):
                tid = t["id"]
                while tid and tid not in visible:  # добавляем цепочку родителей
                    visible.add(tid)
                    p = self.tasks.get(tid)
                    tid = p["parent_id"] if p else None
        return visible

    # ---------- напоминания ----------
    def due_reminders(self, now: datetime | None = None):
        now_s = (now or datetime.now()).strftime(REMIND_FMT)
        return sorted(
            (t for t in self.alive()
             if t["remind"] and not t["reminded"] and not t["done"] and t["remind"] <= now_s),
            key=lambda t: t["remind"],
        )

    def mark_reminded(self, tid):
        t = self.tasks[tid]
        t["reminded"] = True
        self._touch(t)
        self.save()

    def snooze(self, tid, minutes: int, now: datetime | None = None):
        t = self.tasks[tid]
        t["remind"] = ((now or datetime.now()) + timedelta(minutes=minutes)).strftime(REMIND_FMT)
        t["reminded"] = False
        self._touch(t)
        self.save()

    # ---------- изменение ----------
    def _touch(self, t):
        t["updated"] = now_iso()

    def add(self, title, category, parent_id=None):
        if parent_id:
            category = self.tasks[parent_id]["category"]  # подзадача наследует категорию
        ts = now_iso()
        t = {
            "id": uuid.uuid4().hex,
            "title": title,
            "description": "",
            "report": "",
            "category": category,
            "parent_id": parent_id,
            "done": False,
            "done_at": None,
            "links": [],
            "created": ts,
            "updated": ts,
            "deleted": False,
        }
        normalize(t)
        self.tasks[t["id"]] = t
        self.save()
        return t

    def update(self, tid, **fields):
        t = self.tasks[tid]
        category = fields.pop("category", None)
        if "remind" in fields and fields["remind"] != t["remind"]:
            fields["reminded"] = False  # новое время — напомнить заново
        t.update(fields)
        self._touch(t)
        if category and category != t["category"] and t["parent_id"] is None:
            t["category"] = category
            for cid in self.descendants(tid):
                self.tasks[cid]["category"] = category
                self._touch(self.tasks[cid])
        self.save()

    def move_task(self, tid, category):
        """Переносит задачу вместе со всеми подзадачами в другую категорию.
        Если это подзадача, она отделяется от родителя и становится самостоятельной задачей."""
        if category not in CATEGORIES:
            raise ValueError(f"Неизвестная категория: {category}")
        t = self.tasks[tid]
        if t["parent_id"] is None and t["category"] == category:
            return
        t["parent_id"] = None
        t["category"] = category
        self._touch(t)
        for cid in self.descendants(tid):
            self.tasks[cid]["category"] = category
            self._touch(self.tasks[cid])
        self.save()

    def set_done(self, tid, done, report=None):
        t = self.tasks[tid]
        t["done"] = done
        t["failed"] = False  # «выполнена» и «вернуть в работу» снимают пометку о провале
        t["done_at"] = now_iso() if done else None
        if report is not None:
            t["report"] = report
        self._touch(t)
        self.save()

    def set_failed(self, tid, report=None):
        """Закрывает задачу как проваленную (причину можно записать в отчёт)."""
        t = self.tasks[tid]
        t["done"] = True
        t["failed"] = True
        t["done_at"] = now_iso()
        if report is not None:
            t["report"] = report
        self._touch(t)
        self.save()

    def delete(self, tid):
        for x in [tid] + self.descendants(tid):
            t = self.tasks[x]
            for other in list(t["links"]):
                self._unlink_one(other, x)
            t["links"] = []
            t["deleted"] = True
            self._touch(t)
        self.save()

    def _unlink_one(self, owner_id, target_id):
        o = self.tasks.get(owner_id)
        if o and target_id in o["links"]:
            o["links"].remove(target_id)
            self._touch(o)

    def link(self, a, b):
        if a == b:
            return
        for x, y in ((a, b), (b, a)):
            t = self.tasks[x]
            if y not in t["links"]:
                t["links"].append(y)
                self._touch(t)
        self.save()

    def unlink(self, a, b):
        self._unlink_one(a, b)
        self._unlink_one(b, a)
        self.save()
