"""Telegram transport and Russian UI. Python 3.11+, standard library only."""
import argparse
import fcntl
import html
import json
import logging
import os
import signal
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from core import RuleError, Store

LOG = logging.getLogger("padel")
TZ = ZoneInfo("Asia/Dubai")
DEFAULT_LEVELS = ["D", "D+", "C-", "C", "C+", "B-", "B", "B+", "A"]
LABELS = {"confirmed": "Иду", "ready": "Жду место", "offered": "Предложено место",
          "unsure": "Пока не уверен", "declined": "Не иду"}
STATUSES = {"draft": "Черновик", "published": "Запись открыта", "closed": "Запись закрыта",
            "completed": "Завершён", "cancelled": "Отменён"}
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
FIELDS = [
    ("photo", "Пришлите одну фотографию корта. Для черновика можно отправить «-»."),
    ("court", "Название корта (до 120 символов):"),
    ("format", "Формат: Americano, Mexicano или другой (до 120 символов):"),
    ("start", "Дата и время начала по Дубаю: ДД.ММ.ГГГГ ЧЧ:ММ"),
    ("duration", "Продолжительность в минутах, например 90:"),
    ("capacity", "Максимум игроков: 4 / 8 / 12 / 16 / 20 / 24"),
    ("location", "Пришлите pin локации Telegram или https-ссылку на карту:"),
    ("price", "Стоимость с человека, например 150 или 150.50:"),
    ("currency", "Валюта — три буквы, например AED:"),
    ("included", "Что включено (до 250 символов):"),
    ("levels", "Допустимые уровни через запятую или «-» для всех:"),
    ("close_at", "Окончание записи: ДД.ММ.ГГГГ ЧЧ:ММ. «-» — до начала игры."),
]


def esc(v):
    return html.escape(str(v), quote=True)


def money(cents):
    return f"{cents / 100:.2f}"


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def player(r):
    uid = r.get("user_id", r.get("id"))
    name = "@" + r["username"] if r.get("username") else r["display"]
    text = f'<a href="tg://user?id={uid}">{esc(name)}</a>'
    if r.get("full_name"):
        text += " · " + esc(r["full_name"])
    return text + " · " + esc(r.get("level") or "—")


class APIError(Exception):
    def __init__(self, code, description, retry_after=None):
        self.code, self.description, self.retry_after = code, description, retry_after
        super().__init__(f"Telegram {code}: {description}")


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, **params):
        raw = json.dumps(params).encode()
        req = urllib.request.Request(self.base + method, raw, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as res:
                result = json.load(res)
        except urllib.error.HTTPError as exc:
            try:
                result = json.load(exc)
            except Exception:
                raise APIError(exc.code, "HTTP error") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Do not expose URLs containing the bot token.
            raise APIError(0, "Сетевая ошибка") from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0), result.get("description", "Ошибка API"),
                           result.get("parameters", {}).get("retry_after"))
        return result["result"]

    def send(self, chat, text, keyboard=None, **extra):
        return self.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                         link_preview_options={"is_disabled": True}, **({"reply_markup": keyboard} if keyboard else {}), **extra)


class App:
    def __init__(self, store, api, owner, admins=(), group=None, username=None, levels=None):
        self.s, self.api, self.owner = store, api, owner
        self.admins = set(admins) | {owner}
        self.group = group
        self.username = username or "padel_escape_bot"
        self.levels = levels or DEFAULT_LEVELS

    def admin(self, uid):
        if uid not in self.admins:
            raise RuleError("Доступ только для администрации.")

    def group_id(self):
        g = self.s.get_setting("group_id", self.group)
        if not g:
            raise RuleError("Сначала владелец должен выполнить /bind в тестовой группе.")
        return int(g)

    def send(self, uid, text, keyboard=None):
        return self.api.send(uid, text, keyboard)

    def menu(self, uid):
        rows = [[("Ближайшие турниры", "events"), ("Мои регистрации", "mine")],
                [("Моя очередь", "waiting"), ("Мой профиль", "profile")]]
        if uid in self.admins:
            rows.append([("Админка", "admin")])
        return self.send(uid, "🎾 <b>PADEL ESCAPE</b>\nВыберите действие:", kb(rows))

    def card_text(self, e):
        start = datetime.fromtimestamp(e["start"], TZ)
        end = start + timedelta(minutes=e["duration"])
        entries = self.s.entries(e["id"])
        n = sum(r["status"] == "confirmed" for r in entries)
        reserved = sum(r["status"] == "offered" for r in entries)
        waiting = sum(r["status"] in ("ready", "unsure", "offered") for r in entries)
        no = sum(r["status"] == "declined" for r in entries)
        levels = ", ".join(e["levels"]) if e.get("levels") else "Все уровни"
        close = datetime.fromtimestamp(e["close_at"], TZ).strftime("%d.%m %H:%M")
        text = (f"<b>PADEL ESCAPE · {esc(e['format'])}</b> · №{e['id']}\n"
                f"📅 {DAYS[start.weekday()]}, {start:%d.%m.%Y}\n"
                f"🕒 {start:%H:%M}–{end:%H:%M} · {e['duration']} мин · Дубай\n"
                f"📍 {esc(e['court'])}\n"
                f'<a href="{esc(e["location"])}">🗺 Открыть карту</a>\n'
                f"👥 {e['capacity']} игроков · {e['capacity']//4} корт(а/ов)\n"
                f"Уровни: {esc(levels)}\n"
                f"💰 {money(e['price'])} {esc(e['currency'])} с человека\n"
                f"✅ Включено: {esc(e['included'])}\n"
                f"Запись до {close} (Дубай)\n"
                f"<b>{STATUSES[e['status']]}</b>\n"
                f"Идут: {n}/{e['capacity']} · Резерв: {reserved}\nОчередь: {waiting} · Не идут: {no}")
        # Keep public summaries within Telegram's message size. Full names remain in paginated lists.
        for title, states in (("Идут", ("confirmed",)), ("Очередь", ("offered", "ready", "unsure")), ("Не идут", ("declined",))):
            selected = [r for r in entries if r["status"] in states]
            if not selected:
                continue
            heading = f"\n\n<b>{title}</b>"
            if len(text + heading) > 3600:
                break
            text += heading
            for index, r in enumerate(selected, 1):
                compact = dict(r)
                compact["full_name"] = compact["full_name"][:30]
                compact["display"] = compact["display"][:30]
                line = f"\n{index}. {player(compact)}"
                if r["status"] in ("unsure", "offered"):
                    line += " · " + LABELS[r["status"]]
                if len(text + line) > 3600:
                    text += "\n… Полный список — по кнопке ниже."
                    break
                text += line
        return text

    def event_keyboard(self, eid):
        return kb([[("Иду", f"go:{eid}"), ("Не иду", f"no:{eid}"), ("Очередь", f"queue:{eid}")],
                  [("Списки игроков", f"list:{eid}:0")]])

    def roster_text(self, eid, page=0):
        entries = self.s.entries(eid)
        pages = max(1, (len(entries) + 14) // 15)
        page = min(max(0, page), pages - 1)
        lines = [f"<b>Состав турнира №{eid}</b> · {page+1}/{pages}"]
        selected = entries[page*15:(page+1)*15]
        for index, r in enumerate(selected, page*15+1):
            lines.append(f"{index}. {player(r)} — {LABELS[r['status']]}")
        if not entries:
            lines.append("Пока никто не записался.")
        rows = []
        nav = []
        if page:
            nav.append(("←", f"list:{eid}:{page-1}"))
        if page+1 < pages:
            nav.append(("→", f"list:{eid}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([("Открыть турнир", f"event:{eid}")])
        return "\n".join(lines), kb(rows)

    def show_event(self, uid, eid):
        e = self.s.event(eid)
        if e["status"] == "draft" and uid not in self.admins:
            raise RuleError("Турнир ещё не опубликован.")
        if e.get("photo"):
            # A separate text message keeps full details and rosters out of photo caption limits.
            self.api.call("sendPhoto", chat_id=uid, photo=e["photo"])
        return self.send(uid, self.card_text(e), self.event_keyboard(eid) if e["status"] != "draft" else None)

    def refresh(self):
        for e in self.s.events():
            if e["status"] == "draft" or not e["dirty"]:
                continue
            try:
                group = self.group_id()
                if not e["photo_msg"]:
                    photo = self.api.call("sendPhoto", chat_id=group, photo=e["photo"])
                    with self.s.db() as c:
                        c.execute("UPDATE events SET photo_msg=? WHERE id=?", (photo["message_id"], e["id"]))
                else:
                    self.edit("editMessageMedia", chat_id=group, message_id=e["photo_msg"],
                              media={"type": "photo", "media": e["photo"]})
                if not e["roster_msg"]:
                    r = self.send(group, self.card_text(e), self.event_keyboard(e["id"]))
                    with self.s.db() as c:
                        c.execute("UPDATE events SET roster_msg=?,dirty=0 WHERE id=?", (r["message_id"], e["id"]))
                else:
                    self.edit("editMessageText", chat_id=group, message_id=e["roster_msg"],
                              text=self.card_text(e), parse_mode="HTML",
                              link_preview_options={"is_disabled": True}, reply_markup=self.event_keyboard(e["id"]))
                    with self.s.db() as c:
                        c.execute("UPDATE events SET dirty=0 WHERE id=?", (e["id"],))
            except (APIError, RuleError) as exc:
                LOG.warning("Card update failed for event %s: %s", e["id"], exc)

    def edit(self, method, **data):
        try:
            return self.api.call(method, **data)
        except APIError as exc:
            if exc.code == 400 and "message is not modified" in exc.description:
                return None
            raise

    def event_list(self, uid, mode, page=0):
        now = self.s.clock()
        events = []
        for e in self.s.events():
            if mode == "admin_events":
                if e["status"] not in ("completed", "cancelled"):
                    events.append(e)
                continue
            if e["status"] not in ("published", "closed") or e["start"] + e["duration"]*60 <= now:
                continue
            en = self.s.entry(e["id"], uid)
            if mode == "mine" and (not en or en["status"] != "confirmed"):
                continue
            if mode == "waiting" and (not en or en["status"] not in ("ready", "offered", "unsure")):
                continue
            events.append(e)
        pages = max(1, (len(events)+9)//10)
        page = min(max(page, 0), pages-1)
        rows = []
        for e in events[page*10:(page+1)*10]:
            dt = datetime.fromtimestamp(e["start"], TZ)
            prefix = "ae" if mode == "admin_events" else "event"
            rows.append([(f"№{e['id']} · {dt:%d.%m %H:%M} · {e['court'][:30]}", f"{prefix}:{e['id']}")])
        nav = []
        if page:
            nav.append(("←", f"eventpage:{mode}:{page-1}"))
        if page+1 < pages:
            nav.append(("→", f"eventpage:{mode}:{page+1}"))
        if nav:
            rows.append(nav)
        self.send(uid, f"Турниры · {page+1}/{pages}" if events else "В этом разделе пока нет турниров.", kb(rows))

    def start_registration(self, uid, pending=None):
        self.s.session(uid, {"kind": "register", "step": "level", "pending": pending})
        self.level_prompt(uid)

    def level_prompt(self, uid):
        self.send(uid, "Выберите уровень игры. После регистрации менять его сможет только администрация.",
                  kb([[(level, f"level:{i}")] for i, level in enumerate(self.levels)]))

    def admin_menu(self, uid):
        self.admin(uid)
        self.send(uid, "<b>Административная панель</b>", kb([
            [("Создать турнир", "new"), ("Турниры", "admin_events")],
            [("Участники", "users:0"), ("Задачи", "tasks")],
            [("Рассылка", "broadcast"), ("Закреп регистрации", "registration_post")],
            [("Настройки", "settings")],
        ]))

    def admin_event(self, uid, eid):
        self.admin(uid)
        e = self.s.event(eid)
        entries = self.s.entries(eid)
        active = [r for r in entries if r["status"] == "confirmed"]
        paid = [r for r in active if r["paid"]]
        received = sum(r["paid_amount"] for r in entries if r["paid"])
        remaining = sum(e["price"] for r in active if not r["paid"])
        other_paid = sum(r["paid"] and r["status"] != "confirmed" for r in entries)
        self.send(uid, self.card_text(e) +
                  f"\n\n<b>Только для администрации</b>\nОплатили: {len(paid)} из {len(active)}"
                  f"\nПолучено (включая отмены): {money(received)} {esc(e['currency'])}"
                  f"\nОсталось по текущему составу: {money(remaining)} {esc(e['currency'])}"
                  f"\nОплаты вне основного состава для проверки: {other_paid}",
                  kb([
                      [("Предпросмотр", f"preview:{eid}"), ("Редактировать", f"edit:{eid}")],
                      [("Игроки / оплаты", f"participants:{eid}:0"), ("Копировать", f"copy:{eid}")],
                      [("Опубликовать", f"publish:{eid}"), ("Закрыть запись", f"close:{eid}")],
                      [("Завершить", f"complete:{eid}"), ("Отменить турнир", f"cancel_event:{eid}")],
                      [("Рассылка игрокам", f"broadcast_event:{eid}"), ("Закрепить", f"pin:{eid}")],
                      [("Добавить игрока", f"add:{eid}")],
                  ]))

    def users_page(self, uid, page, eid=None):
        self.admin(uid)
        if eid is None:
            with self.s.db() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM users WHERE level IS NOT NULL ORDER BY id")]
        else:
            rows = self.s.entries(eid)
        pages = max(1, (len(rows)+9)//10)
        page = min(max(page, 0), pages-1)
        buttons = []
        for r in rows[page*10:(page+1)*10]:
            rid = r.get("user_id", r.get("id"))
            label = (r.get("full_name") or r.get("username") or r["display"])[:35]
            if eid is not None:
                label += " · " + ("✓ Оплачено" if r["paid"] else "Не оплачено")
            buttons.append([(label, f"person:{rid}" if eid is None else f"participant:{eid}:{rid}")])
        nav = []
        target = "users" if eid is None else f"participants:{eid}"
        if page:
            nav.append(("←", f"{target}:{page-1}"))
        if page+1 < pages:
            nav.append(("→", f"{target}:{page+1}"))
        if nav:
            buttons.append(nav)
        self.send(uid, f"Участники · {page+1}/{pages} · всего {len(rows)}", kb(buttons))

    def person(self, uid, target, eid=None):
        self.admin(uid)
        u = self.s.user(target)
        if not u:
            raise RuleError("Игрок не найден.")
        text = player(u) + f"\nID: <code>{target}</code>"
        rows = [[("Изменить уровень", f"changelevel:{target}")]]
        if u["dm_error"]:
            text += "\n⚠ Личные сообщения не доставлены."
        if eid is not None:
            en = self.s.entry(eid, target)
            if not en:
                raise RuleError("Нет записи игрока.")
            text += f"\nСтатус: {LABELS[en['status']]}\nОплата: " + ("Оплачено" if en["paid"] else "Не оплачено")
            rows += [[("Оплачено", f"paid:{eid}:{target}:1"), ("Не оплачено", f"paid:{eid}:{target}:0")],
                     [("В основной состав", f"move:{eid}:{target}:go")],
                     [("В неуверенные", f"move:{eid}:{target}:queue"), ("Не идёт", f"move:{eid}:{target}:no")]]
        self.send(uid, text, kb(rows))

    def wizard_prompt(self, uid, session):
        index = session["index"]
        if index >= len(FIELDS):
            self.save_wizard(uid, session)
            return
        key, prompt = FIELDS[index]
        if key == "levels":
            prompt += "\nВарианты: " + ", ".join(self.levels)
        if session.get("eid"):
            value = session["data"].get(key)
            prompt += "\nОтправьте «=», чтобы оставить текущее значение."
            if key not in ("photo",):
                prompt += "\nСейчас: " + esc(value)
        self.s.session(uid, session)
        self.send(uid, f"Шаг {index+1}/{len(FIELDS)}\n{prompt}\n/cancel — выйти, черновик не изменится.")

    def parse_field(self, key, message, data):
        text = message.get("text", "").strip()
        if text == "=" and key in data:
            return data[key]
        if key == "photo":
            if message.get("photo"):
                return message["photo"][-1]["file_id"]
            if text == "-":
                return None
            raise RuleError("Пришлите фото как изображение или «-».")
        if key in ("start", "close_at"):
            if key == "close_at" and text == "-":
                return data["start"]
            try:
                dt = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
            except ValueError:
                raise RuleError("Формат даты: 20.11.2026 19:00") from None
            if dt.timestamp() <= self.s.clock():
                raise RuleError("Укажите дату и время в будущем.")
            if key == "close_at" and dt.timestamp() > data["start"]:
                raise RuleError("Закрытие записи не позже начала игры.")
            return dt.timestamp()
        if key in ("duration", "capacity"):
            try:
                value = int(text)
            except ValueError:
                raise RuleError("Введите целое число.") from None
            if key == "capacity" and value not in (4, 8, 12, 16, 20, 24):
                raise RuleError("Вместимость: 4, 8, 12, 16, 20 или 24.")
            if key == "duration" and not 15 <= value <= 1440:
                raise RuleError("Продолжительность: 15–1440 минут.")
            return value
        if key == "price":
            try:
                value = Decimal(text.replace(",", "."))
                if not value.is_finite() or value < 0 or value > 1000000 or value.as_tuple().exponent < -2:
                    raise InvalidOperation
                return int(value * 100)
            except (InvalidOperation, ValueError):
                raise RuleError("Введите сумму от 0 до 1000000, максимум два знака после точки.") from None
        if key == "location" and "location" in message:
            loc = message["location"]
            return f"https://maps.google.com/?q={loc['latitude']},{loc['longitude']}"
        if key == "location":
            if not text.startswith("https://") or len(text) > 250 or any(x.isspace() for x in text):
                raise RuleError("Нужна https-ссылка без пробелов или pin локации.")
        elif key == "currency":
            text = text.upper()
            if len(text) != 3 or not text.isascii() or not text.isalpha():
                raise RuleError("Например AED.")
        elif key == "levels":
            if text == "-":
                return []
            levels = [s.strip().replace("−", "-") for s in text.split(",")]
            if not levels or any(s not in self.levels for s in levels):
                raise RuleError("Выберите уровни из указанного перечня.")
            return list(dict.fromkeys(levels))
        elif not 1 <= len(text) <= (250 if key == "included" else 120):
            raise RuleError("Поле пустое или слишком длинное.")
        return text

    def save_wizard(self, uid, session):
        eid = self.s.save_event(uid, session["data"], session.get("eid"))
        self.s.session(uid, clear=True)
        self.send(uid, f"Турнир №{eid} сохранён. Проверьте предпросмотр перед публикацией.")
        self.admin_event(uid, eid)

    def handle_message(self, m):
        uid, chat = m["from"]["id"], m["chat"]["id"]
        private = m["chat"]["type"] == "private"
        self.s.touch_user(m["from"], private)
        text = m.get("text", "").strip()
        cmd = text.split()[0].split("@")[0] if text else ""
        if cmd == "/id":
            self.send(chat, f"Ваш Telegram ID: <code>{uid}</code>\nID чата: <code>{chat}</code>")
            return
        if not private:
            if cmd == "/bind":
                if uid != self.owner:
                    raise RuleError("Привязать группу может только владелец.")
                old = self.s.get_setting("group_id", self.group)
                if old and int(old) != chat:
                    with self.s.db() as c:
                        exists = c.execute("SELECT 1 FROM events WHERE photo_msg IS NOT NULL LIMIT 1").fetchone()
                    if exists:
                        raise RuleError("Уже есть опубликованные карточки в другой группе. Перенос требует отдельной настройки.")
                self.s.setting("group_id", chat)
                self.send(chat, "Группа подключена. Создайте тестовый турнир в личной админке бота.")
            return
        if cmd == "/cancel":
            self.s.session(uid, clear=True)
            self.menu(uid)
            return
        if cmd == "/start":
            self.s.session(uid, clear=True)
            args = text.split(maxsplit=1)
            pending = None
            if len(args) == 2 and args[1].startswith("e"):
                try:
                    action, eid = args[1][1:].split("_")
                    if action in ("go", "queue", "no", "view"):
                        pending = [action, int(eid)]
                except ValueError:
                    pass
            if not self.s.user(uid)["level"]:
                self.start_registration(uid, pending)
            else:
                self.menu(uid)
                if pending:
                    if pending[0] == "view":
                        self.show_event(uid, pending[1])
                    else:
                        self.choice(uid, pending[1], pending[0])
            return
        if cmd == "/admin":
            self.admin_menu(uid)
            return
        if cmd in ("/tournaments", "/my"):
            self.event_list(uid, "events" if cmd == "/tournaments" else "mine")
            return
        if cmd == "/help":
            self.send(uid, "/start — меню\n/admin — админка\n/id — ваш ID\n/cancel — выйти из текущего ввода")
            return
        session = self.s.session(uid)
        if not session:
            self.menu(uid)
            return
        kind = session["kind"]
        if kind == "register":
            if session["step"] == "name":
                if not 1 <= len(text) <= 100:
                    raise RuleError("Введите имя до 100 символов или нажмите «Пропустить».")
                session.update(step="level", name=text)
                self.s.session(uid, session)
                self.level_prompt(uid)
            else:
                self.level_prompt(uid)
            return
        self.admin(uid)
        if kind == "wizard":
            key = FIELDS[session["index"]][0]
            value = self.parse_field(key, m, session["data"])
            session["data"][key] = value
            session["index"] += 1
            self.wizard_prompt(uid, session)
        elif kind == "copy":
            source = self.s.event(session["eid"])
            new_start = self.parse_field("start", m, {})
            data = json.loads(source["data"])
            gap = source["start"] - source["close_at"]
            data.update(start=new_start, close_at=new_start-gap)
            eid = self.s.save_event(uid, data)
            self.s.session(uid, clear=True)
            self.admin_event(uid, eid)
        elif kind == "add":
            try:
                target = int(text)
            except ValueError:
                raise RuleError("Введите числовой Telegram ID зарегистрированного игрока.") from None
            state = self.s.join(session["eid"], target, "go", force=True, actor=uid)
            self.s.session(uid, clear=True)
            self.send(uid, f"Игрок добавлен: {LABELS[state]}.")
            self.person(uid, target, session["eid"])
        elif kind == "offer_minutes":
            try:
                minutes = int(text)
            except ValueError:
                raise RuleError("Введите число минут.") from None
            if not 1 <= minutes <= 1440:
                raise RuleError("От 1 до 1440 минут.")
            self.s.setting("offer_minutes", minutes)
            self.s.session(uid, clear=True)
            self.send(uid, "Срок новых предложений обновлён.")
        elif kind == "broadcast":
            if not 1 <= len(text) <= 3000:
                raise RuleError("Рассылка: текст до 3000 символов.")
            session.update(kind="broadcast_confirm", text=text)
            self.s.session(uid, session)
            self.send(uid, "<b>Предпросмотр рассылки</b>\n" + esc(text),
                      kb([[("Отправить", "broadcast_confirm"), ("Отмена", "discard")]]))
        else:
            self.send(uid, "Используйте кнопки подтверждения или /cancel.")

    def choice(self, uid, eid, action, confirmed=False):
        u = self.s.user(uid)
        if not u or not u["level"]:
            self.start_registration(uid, [action, eid])
            return
        old = self.s.entry(eid, uid)
        if old and old["status"] == "confirmed" and action in ("no", "queue") and not confirmed:
            self.send(uid, "Освободить ваше подтверждённое место?",
                      kb([[("Да", f"confirm_choice:{eid}:{action}"), ("Оставить место", "menu")]]))
            return
        if old and old["status"] == "offered" and action == "go":
            self.send(uid, "У вас зарезервировано место. Подтвердите участие:",
                      kb([[("Подтвердить", f"accept:{eid}:{old['offer_version']}"),
                           ("Отказаться", f"reject:{eid}:{old['offer_version']}")]]))
            return
        status = self.s.join(eid, uid, action)
        self.send(uid, f"Турнир №{eid}: <b>{LABELS[status]}</b>.",
                  kb([[("Открыть турнир", f"event:{eid}"), ("Меню", "menu")]]))

    def callback(self, q):
        uid = q["from"]["id"]
        private = q.get("message", {}).get("chat", {}).get("type") == "private"
        self.s.touch_user(q["from"], False)
        parts = q.get("data", "").split(":")
        action = parts[0]
        # Public buttons never trigger private registration/admin state changes directly.
        if not private:
            if action not in ("go", "no", "queue", "list", "event"):
                raise RuleError("Откройте личный чат бота.")
            eid = int(parts[1])
            parameter = f"e{action}_{eid}" if action in ("go", "no", "queue") else f"eview_{eid}"
            self.api.call("answerCallbackQuery", callback_query_id=q["id"],
                          url=f"https://t.me/{self.username}?start={parameter or 'menu'}")
            return
        self.api.call("answerCallbackQuery", callback_query_id=q["id"])
        if action == "menu":
            self.menu(uid)
        elif action in ("events", "mine", "waiting"):
            self.event_list(uid, action)
        elif action == "eventpage":
            if parts[1] == "admin_events":
                self.admin(uid)
            elif parts[1] not in ("events", "mine", "waiting"):
                raise RuleError("Неизвестный раздел.")
            self.event_list(uid, parts[1], int(parts[2]))
        elif action == "profile":
            u = self.s.user(uid)
            if not u["level"]:
                self.start_registration(uid)
            else:
                self.send(uid, player(u) + "\nДля изменения уровня обратитесь к администратору.")
        elif action == "skip_name":
            s = self.s.session(uid)
            if not s or s["kind"] != "register" or s["step"] != "name":
                raise RuleError("Кнопка устарела. Откройте /start.")
            s.update(step="level", name="")
            self.s.session(uid, s)
            self.level_prompt(uid)
        elif action == "level":
            s = self.s.session(uid)
            if not s or s["kind"] != "register" or s["step"] != "level":
                raise RuleError("Анкета уже заполнена или кнопка устарела.")
            index = int(parts[1])
            if not 0 <= index < len(self.levels):
                raise RuleError("Неверный уровень.")
            self.s.register(uid, s.get("name", ""), self.levels[index])
            self.s.session(uid, clear=True)
            self.send(uid, "Профиль сохранён. Повторная регистрация не потребуется.")
            self.menu(uid)
            if s.get("pending"):
                if s["pending"][0] == "view":
                    self.show_event(uid, s["pending"][1])
                else:
                    self.choice(uid, s["pending"][1], s["pending"][0])
        elif action in ("go", "no", "queue"):
            self.choice(uid, int(parts[1]), action)
        elif action == "confirm_choice":
            self.choice(uid, int(parts[1]), parts[2], True)
        elif action in ("accept", "reject"):
            self.s.answer_offer(int(parts[1]), uid, int(parts[2]), action == "accept")
            self.send(uid, "Участие подтверждено!" if action == "accept" else "Вы отказались от места.")
        elif action == "event":
            self.show_event(uid, int(parts[1]))
        elif action == "list":
            eid = int(parts[1])
            if self.s.event(eid)["status"] == "draft" and uid not in self.admins:
                raise RuleError("Турнир ещё не опубликован.")
            text, keyboard = self.roster_text(eid, int(parts[2]))
            self.edit("editMessageText", chat_id=uid, message_id=q["message"]["message_id"],
                      text=text, parse_mode="HTML", reply_markup=keyboard)
        else:
            self.admin_callback(uid, parts)

    def admin_callback(self, uid, p):
        self.admin(uid)
        a = p[0]
        if a == "admin":
            self.admin_menu(uid)
        elif a == "admin_events":
            self.event_list(uid, a)
        elif a == "new":
            self.wizard_prompt(uid, {"kind": "wizard", "index": 0, "data": {}})
        elif a == "ae":
            self.admin_event(uid, int(p[1]))
        elif a == "preview":
            self.show_event(uid, int(p[1]))
        elif a == "edit":
            e = self.s.event(int(p[1]))
            self.wizard_prompt(uid, {"kind": "wizard", "index": 0, "eid": e["id"], "data": json.loads(e["data"])})
        elif a == "publish":
            eid = int(p[1])
            group = self.group_id()
            member = self.api.call("getChatMember", chat_id=group, user_id=self.bot_id)
            if member["status"] not in ("administrator", "creator"):
                raise RuleError("Дайте боту права администратора в подключённой группе.")
            self.s.publish(uid, eid)
            self.send(uid, "Турнир опубликован. Карточка отправляется в группу.")
        elif a in ("close", "complete", "cancel_event"):
            eid = int(p[1])
            self.send(uid, "Подтвердить изменение статуса турнира?",
                      kb([[("Подтвердить", f"status_yes:{eid}:{a}"), ("Отмена", "admin")]]))
        elif a == "status_yes":
            status = {"close": "closed", "complete": "completed", "cancel_event": "cancelled"}[p[2]]
            self.s.change_status(uid, int(p[1]), status)
            self.admin_event(uid, int(p[1]))
        elif a == "copy":
            self.s.session(uid, {"kind": "copy", "eid": int(p[1])})
            self.send(uid, "Новая дата и время по Дубаю: ДД.ММ.ГГГГ ЧЧ:ММ\nСостав и оплаты не копируются.")
        elif a == "participants":
            self.users_page(uid, int(p[2]), int(p[1]))
        elif a == "users":
            self.users_page(uid, int(p[1]))
        elif a == "person":
            self.person(uid, int(p[1]))
        elif a == "participant":
            self.person(uid, int(p[2]), int(p[1]))
        elif a == "changelevel":
            target = int(p[1])
            self.send(uid, "Новый уровень:", kb([[(v, f"setlevel:{target}:{i}")] for i,v in enumerate(self.levels)]))
        elif a == "setlevel":
            index = int(p[2])
            if not 0 <= index < len(self.levels):
                raise RuleError("Неизвестный уровень.")
            self.s.set_level(uid, int(p[1]), self.levels[index])
            self.person(uid, int(p[1]))
        elif a == "paid":
            self.s.paid(uid, int(p[1]), int(p[2]), p[3] == "1")
            self.person(uid, int(p[2]), int(p[1]))
        elif a == "move":
            state = self.s.join(int(p[1]), int(p[2]), p[3], force=True, actor=uid)
            self.send(uid, f"Новый статус: {LABELS[state]}.")
            self.person(uid, int(p[2]), int(p[1]))
        elif a == "add":
            self.s.session(uid, {"kind": "add", "eid": int(p[1])})
            self.send(uid, "Введите Telegram ID зарегистрированного игрока. Найти его можно в разделе «Участники».")
        elif a == "pin":
            e = self.s.event(int(p[1]))
            if not e["roster_msg"]:
                raise RuleError("Карточка ещё не отправлена в группу.")
            self.api.call("pinChatMessage", chat_id=self.group_id(), message_id=e["roster_msg"], disable_notification=True)
            self.send(uid, "Карточка закреплена.")
        elif a == "registration_post":
            keyboard = {"inline_keyboard": [[{"text": "Регистрация игрока", "url": f"https://t.me/{self.username}?start=register"}]]}
            m = self.send(self.group_id(), "🎾 <b>PADEL ESCAPE</b>\nПройдите регистрацию один раз: имя по желанию и уровень игры.\nЗатем сможете записываться на турниры и получать личные напоминания.\nНик/имя и уровень будут видны в списках игроков.", keyboard)
            self.api.call("pinChatMessage", chat_id=self.group_id(), message_id=m["message_id"], disable_notification=True)
            self.send(uid, "Сообщение регистрации опубликовано и закреплено.")
        elif a in ("broadcast", "broadcast_event"):
            target = "all" if a == "broadcast" else p[1]
            self.s.session(uid, {"kind": "broadcast", "target": target})
            self.send(uid, "Введите текст рассылки. Перед отправкой будет предпросмотр и подтверждение.\n/cancel — отменить.")
        elif a == "broadcast_confirm":
            s = self.s.session(uid)
            if not s or s["kind"] != "broadcast_confirm":
                raise RuleError("Эта рассылка уже отправлена или отменена.")
            n = self.s.queue_broadcast(uid, s["target"], esc(s["text"]))
            self.s.session(uid, clear=True)
            self.send(uid, f"Рассылка поставлена в очередь: {n} получателей.")
        elif a == "discard":
            self.s.session(uid, clear=True)
            self.admin_menu(uid)
        elif a == "settings":
            n = self.s.get_setting("offer_minutes", 30)
            self.send(uid, f"Часовой пояс: Дубай\nСрок подтверждения места: {n} мин\nГотовые игроки идут по времени готовности.\nУровни: {esc(', '.join(self.levels))}",
                      kb([[("Изменить срок", "offer_minutes")]]))
        elif a == "offer_minutes":
            self.s.session(uid, {"kind": "offer_minutes"})
            self.send(uid, "Сколько минут давать для подтверждения места? От 1 до 1440.")
        elif a == "tasks":
            with self.s.db() as c:
                failed = c.execute("SELECT count(*) FROM outbox WHERE state='failed'").fetchone()[0]
                dm = c.execute("SELECT count(*) FROM users WHERE dm_error!=''").fetchone()[0]
                offers = c.execute("SELECT count(*) FROM entries WHERE status='offered'").fetchone()[0]
                review = c.execute("SELECT count(*) FROM entries WHERE paid=1 AND status!='confirmed'").fetchone()[0]
            self.send(uid, f"<b>Задачи администратора</b>\nАктивных предложений: {offers}\nОплат вне состава: {review}\nИгроков с ошибками доставки: {dm}\nНе доставлено сообщений: {failed}")
        else:
            raise RuleError("Кнопка устарела. Откройте /admin.")

    def handle(self, update):
        uid = None
        private = False
        q = update.get("callback_query")
        m = update.get("message")
        try:
            if q:
                uid = q["from"]["id"]
                private = q.get("message", {}).get("chat", {}).get("type") == "private"
                self.callback(q)
            elif m and not m["from"].get("is_bot"):
                uid = m["from"]["id"]
                private = m["chat"]["type"] == "private"
                self.handle_message(m)
        except (RuleError, ValueError, KeyError, IndexError) as exc:
            error = str(exc) if isinstance(exc, RuleError) else "Кнопка или ввод устарели. Откройте /start."
            if q and not private:
                self.api.call("answerCallbackQuery", callback_query_id=q["id"], text=error[:180], show_alert=True)
            elif uid and private:
                self.send(uid, esc(error))
            elif m:
                self.send(m["chat"]["id"], esc(error))
        except APIError as exc:
            LOG.warning("Telegram action failed: %s", exc)
            if uid and private:
                try:
                    self.send(uid, "Не удалось выполнить действие в Telegram. Повторите позже; сохранённые данные не потеряны.")
                except APIError:
                    pass

    def deliver(self):
        for n in self.s.due_notifications():
            if not self.s.notification_valid(n):
                self.s.notification_result(n, "cancelled")
                continue
            try:
                keyboard = json.loads(n["keyboard"]) if n["keyboard"] else kb([[("Турниры", "events")]])
                self.send(n["user_id"], esc(n["text"]) if n["kind"] != "broadcast" else n["text"], keyboard)
                self.s.notification_result(n, "sent")
            except APIError as exc:
                permanent = exc.code in (400, 403) or n["attempts"] >= 7
                self.s.notification_result(n, "failed" if permanent else "pending",
                                           f"Telegram {exc.code}", exc.retry_after)
                if exc.code == 429:
                    break
            time.sleep(0.06)


def load_env(path):
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate local configuration without Telegram connection")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    load_env(args.env)
    token = os.environ.get("BOT_TOKEN", "")
    owner = os.environ.get("OWNER_ID", "")
    if not token or ":" not in token or not owner.isdigit() or int(owner) <= 0:
        raise SystemExit("Заполните BOT_TOKEN и OWNER_ID в .env. Токен нельзя публиковать.")
    admins = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
    group = os.environ.get("GROUP_ID")
    db_path = Path(os.environ.get("DB_PATH", "data/padel.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(db_path.parent, 0o700)
    levels = [x.strip() for x in os.environ.get("LEVELS", ",".join(DEFAULT_LEVELS)).split(",") if x.strip()]
    if not levels or len(levels) > 20 or len(set(levels)) != len(levels):
        raise SystemExit("LEVELS: от 1 до 20 уникальных уровней.")
    lock = open(str(db_path) + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Этот бот уже запущен с данной базой.") from None
    store = Store(db_path)
    os.chmod(db_path, 0o600)
    if args.check:
        print("Конфигурация и база данных готовы. Соединение с Telegram не проверялось.")
        return
    api = Telegram(token)
    info = api.call("getMe")
    # Polling and webhooks cannot run together. Never silently remove an existing webhook.
    webhook = api.call("getWebhookInfo")
    if webhook.get("url"):
        raise SystemExit("У бота уже настроен webhook. Отключите прежний сервис до запуска polling.")
    app = App(store, api, int(owner), admins, int(group) if group else None, info["username"], levels)
    app.bot_id = info["id"]
    api.call("setMyCommands", commands=[
        {"command": "start", "description": "Меню и регистрация"},
        {"command": "tournaments", "description": "Ближайшие турниры"},
        {"command": "my", "description": "Мои регистрации"},
        {"command": "admin", "description": "Админка"},
        {"command": "id", "description": "Мой Telegram ID"},
        {"command": "cancel", "description": "Отменить текущий ввод"},
    ])
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("Bot started: @%s", info["username"])
    last_refresh = 0
    last_backup = 0
    while running:
        try:
            updates = api.call("getUpdates", offset=store.get_setting("offset", 0),
                               timeout=10, allowed_updates=["message", "callback_query"])
            for u in updates:
                app.handle(u)
                store.setting("offset", u["update_id"]+1)
            store.tick()
            app.deliver()
            if time.time() - last_refresh >= 3:
                app.refresh()
                last_refresh = time.time()
            if time.time() - last_backup >= 86400:
                folder = db_path.parent / "backups"
                folder.mkdir(mode=0o700, exist_ok=True)
                target = folder / (datetime.now(TZ).strftime("%Y-%m-%d") + ".sqlite3")
                store.backup(target)
                os.chmod(target, 0o600)
                for p in sorted(folder.glob("*.sqlite3"))[:-7]:
                    p.unlink()
                last_backup = time.time()
        except APIError as exc:
            LOG.warning("Telegram connection: %s", exc)
            time.sleep(min(exc.retry_after or 3, 30))
        except Exception:
            LOG.exception("Unexpected failure; restarting loop")
            time.sleep(3)
    LOG.info("Stopped cleanly")


if __name__ == "__main__":
    main()
