"""Значок в системном трее (pystray + Pillow). Необязательная зависимость:
если библиотек нет или трей недоступен, available == False и приложение работает без него.

Колбэки pystray вызываются из другого потока, поэтому on_show/on_quit должны быть
потокобезопасными (в приложении они просто кладут сообщение в очередь).
"""


def _make_icon_image():
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(37, 99, 235, 255))
    d.line([(16, 34), (28, 46), (49, 20)], fill=(255, 255, 255, 255), width=7, joint="curve")
    return img


class Tray:
    def __init__(self, on_show, on_quit, title="Туду-лист"):
        self.on_show = on_show
        self.on_quit = on_quit
        self.title = title
        self.available = False
        self.error = None
        self._icon = None

    def start(self):
        try:
            import pystray
        except Exception as e:  # нет библиотеки или нет графического окружения
            self.error = f"pystray недоступен: {e}"
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Открыть", lambda icon, item: self.on_show(), default=True),
                pystray.MenuItem("Выйти", lambda icon, item: self.on_quit()),
            )
            self._icon = pystray.Icon("todo_app", _make_icon_image(), self.title, menu)
            self._icon.run_detached()  # трей работает в отдельном потоке, не мешая Tk
            self.available = True
        except Exception as e:
            self.error = f"Не удалось создать значок в трее: {e}"
            self._icon = None
            self.available = False

    def notify(self, title, message):
        """Всплывающее уведомление от значка (если платформа поддерживает)."""
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
            self.available = False
