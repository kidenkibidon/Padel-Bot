"""Domain rules, persistent state, and durable notification queue. No external dependencies."""
import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager


class RuleError(Exception):
    pass


class Store:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        with self.db() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY, username TEXT, display TEXT NOT NULL,
              full_name TEXT DEFAULT '', level TEXT, private_started INTEGER DEFAULT 0,
              dm_error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1,
              photo_msg INTEGER, roster_msg INTEGER, dirty INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS entries(
              event_id INTEGER NOT NULL REFERENCES events(id), user_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL, ready_at REAL, joined_at REAL NOT NULL,
              offer_until REAL, offer_version INTEGER NOT NULL DEFAULT 0,
              paid INTEGER NOT NULL DEFAULT 0, paid_amount INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(event_id,user_id));
            CREATE TABLE IF NOT EXISTS event_results(
              event_id INTEGER NOT NULL REFERENCES events(id),
              user_id INTEGER NOT NULL REFERENCES users(id), place INTEGER NOT NULL CHECK(place>0),
              PRIMARY KEY(event_id,user_id));
            CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
              id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE NOT NULL,
              user_id INTEGER NOT NULL, text TEXT NOT NULL, keyboard TEXT,
              event_id INTEGER, revision INTEGER, kind TEXT NOT NULL,
              due REAL NOT NULL, expires REAL, attempts INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'pending', error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
              actor INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def get_setting(self, key, default=None):
        with self.db() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def setting(self, key, value):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def session(self, uid, data=None, clear=False):
        with self.db() as c:
            if clear:
                c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                return
            if data is not None:
                c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?)", (uid, json.dumps(data)))
                return
            r = c.execute("SELECT data FROM sessions WHERE user_id=?", (uid,)).fetchone()
            return json.loads(r[0]) if r else None

    def touch_user(self, u, private=False):
        with self.db() as c:
            c.execute("""INSERT INTO users(id,username,display,private_started) VALUES(?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET username=excluded.username,display=excluded.display,
              private_started=MAX(users.private_started,excluded.private_started)""",
              (u["id"], u.get("username"), " ".join(filter(None, [u.get("first_name"), u.get("last_name")])) or str(u["id"]), int(private)))
            if private:
                c.execute("UPDATE users SET dm_error='' WHERE id=?", (u["id"],))

    def user(self, uid):
        with self.db() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return dict(r) if r else None

    def tournaments_played(self, uid):
        """Count confirmed entries in completed events, including existing history."""
        with self.db() as c:
            return c.execute("""
                SELECT count(*) FROM entries en
                JOIN events e ON e.id=en.event_id
                WHERE en.user_id=? AND en.status='confirmed' AND e.status='completed'
            """, (uid,)).fetchone()[0]

    def register(self, uid, name, level):
        with self.db() as c:
            c.execute("UPDATE users SET full_name=?,level=? WHERE id=? AND level IS NULL",
                      (name, level, uid))

    def set_level(self, actor, uid, level):
        with self.db() as c:
            if not c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                raise RuleError("Игрок не найден.")
            c.execute("UPDATE users SET level=? WHERE id=?", (level, uid))
            c.execute("UPDATE events SET dirty=1 WHERE id IN (SELECT event_id FROM entries WHERE user_id=?)", (uid,))
            self.audit(c, actor, "level", [uid, level])

    def audit(self, c, actor, action, detail):
        c.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
                  (self.clock(), actor, action, json.dumps(detail, ensure_ascii=False)))

    def _event(self, c, eid):
        r = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not r:
            raise RuleError("Турнир не найден.")
        data = json.loads(r["data"])
        defaults = dict(photo=None, court="", format="", start=0, close_at=0,
                        duration=0, capacity=0, location="", price=0, currency="", included="", levels=[])
        defaults.update({k: v for k, v in data.items() if v is not None})
        if not defaults["close_at"]:
            defaults["close_at"] = defaults["start"]
        return {**dict(r), **defaults}

    def event(self, eid):
        with self.db() as c:
            return self._event(c, eid)

    def events(self):
        with self.db() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM events")]
            return sorted([self._event(c, i) for i in ids], key=lambda e: e.get("start") or 0)

    def validate(self, data):
        labels = {"court": "Название корта", "format": "Формат", "start": "Дата и время",
                  "duration": "Продолжительность", "capacity": "Количество игроков"}
        missing = [label for key, label in labels.items() if not data.get(key)]
        if missing:
            raise RuleError("Перед публикацией заполните: " + ", ".join(missing))
        for k in ("court", "format", "included"):
            if data.get(k) and (not isinstance(data[k], str) or len(data[k]) > (250 if k == "included" else 120)):
                raise RuleError("Некорректное поле: " + k)
        if data["capacity"] not in (4, 8, 12, 16, 20, 24):
            raise RuleError("Вместимость: 4, 8, 12, 16, 20 или 24.")
        if not 15 <= data["duration"] <= 1440:
            raise RuleError("Продолжительность: от 15 до 1440 минут.")
        price = data.get("price")
        if price is not None and not 0 <= price <= 100000000:
            raise RuleError("Некорректная стоимость.")
        currency = data.get("currency") or ""
        if price is not None and (len(currency) != 3 or not currency.isascii() or not currency.isalpha()):
            raise RuleError("Укажите валюту стоимости, например AED.")
        location = data.get("location")
        if location and (not location.startswith("https://") or len(location) > 250):
            raise RuleError("Нужна https-ссылка на карту или Telegram-локация.")
        if (data.get("close_at") or data["start"]) > data["start"]:
            raise RuleError("Запись должна закрываться не позже начала.")

    def save_event(self, actor, data, eid=None):
        with self.db() as c:
            if eid is None:
                eid = c.execute("INSERT INTO events(data) VALUES(?)", (json.dumps(data),)).lastrowid
            else:
                old = self._event(c, eid)
                if old["status"] in ("cancelled", "completed", "trash"):
                    raise RuleError("Завершённый или отменённый турнир не редактируется. Создайте копию.")
                occupied = c.execute("SELECT count(*) FROM entries WHERE event_id=? AND status IN ('confirmed','offered')", (eid,)).fetchone()[0]
                if old["status"] != "draft":
                    self.validate(data)
                if (data.get("capacity") or 0) < occupied:
                    raise RuleError("Новый лимит меньше числа занятых и зарезервированных мест.")
                if old["currency"] != (data.get("currency") or "") and c.execute(
                    "SELECT 1 FROM entries WHERE event_id=? AND paid=1 LIMIT 1", (eid,)
                ).fetchone():
                    raise RuleError("Нельзя менять валюту при сохранённых оплатах. Создайте новый турнир.")
                c.execute("UPDATE events SET data=?,revision=revision+1,dirty=1 WHERE id=?", (json.dumps(data), eid))
                c.execute("UPDATE outbox SET state='cancelled' WHERE event_id=? AND kind='reminder' AND state='pending'", (eid,))
                if old["status"] != "draft":
                    for r in c.execute("SELECT user_id FROM entries WHERE event_id=? AND status IN ('confirmed','ready','unsure','offered')", (eid,)).fetchall():
                        self.enqueue(c, f"change:{eid}:{old['revision']+1}:{r[0]}", r[0],
                                     self.change_text(eid, old, self._event(c, eid)),
                                     "change", eid)
                # An increased capacity may create offers for the existing ready queue.
                self._fill(c, eid)
            self.audit(c, actor, "save_event", eid)
            return eid

    @staticmethod
    def change_text(eid, old, new):
        labels = {"start": "Дата и время", "court": "Корт", "location": "Локация",
                  "duration": "Длительность", "capacity": "Мест", "price": "Стоимость",
                  "currency": "Валюта", "included": "Включено", "levels": "Уровни",
                  "format": "Формат", "close_at": "Закрытие записи"}
        lines = [f"Турнир №{eid}: изменения"]
        def display(key, value):
            if key in ("start", "close_at") and value:
                return datetime.fromtimestamp(value, ZoneInfo("Asia/Dubai")).strftime("%d.%m.%Y %H:%M")
            if key == "price":
                return f"{(value or 0)/100:.2f}"
            if isinstance(value, list):
                return ", ".join(value) or "Все уровни"
            return str(value) if value not in (None, "") else "Не указано"
        for key, label in labels.items():
            if old.get(key) != new.get(key):
                lines.append(f"{label}: {display(key, old.get(key))} → {display(key, new.get(key))}")
        lines.append("Актуальная карточка — в разделе «Турниры».")
        return "\n".join(lines)

    def trash_draft(self, actor, eid, restore=False, delete=False):
        with self.db() as c:
            e = self._event(c, eid)
            expected = "trash" if restore or delete else "draft"
            if e["status"] != expected:
                raise RuleError("Действие доступно только для черновика в соответствующем разделе.")
            if c.execute("SELECT 1 FROM entries WHERE event_id=?", (eid,)).fetchone():
                raise RuleError("Нельзя удалить турнир с записями игроков.")
            if delete:
                c.execute("DELETE FROM outbox WHERE event_id=?", (eid,))
                c.execute("DELETE FROM events WHERE id=?", (eid,))
            else:
                c.execute("UPDATE events SET status=? WHERE id=?", ("draft" if restore else "trash", eid))
            self.audit(c, actor, "delete" if delete else "restore" if restore else "trash", eid)

    def publish(self, actor, eid):
        with self.db() as c:
            e = self._event(c, eid)
            if e["status"] != "draft":
                raise RuleError("Турнир уже опубликован.")
            self.validate(json.loads(e["data"]))
            if e["start"] <= self.clock() or e["close_at"] <= self.clock():
                raise RuleError("Укажите будущие даты турнира и закрытия записи.")
            c.execute("UPDATE events SET status='published',dirty=1 WHERE id=?", (eid,))
            self.audit(c, actor, "publish", eid)

    def results(self, eid):
        with self.db() as c:
            return {r["user_id"]: r["place"] for r in c.execute(
                "SELECT user_id,place FROM event_results WHERE event_id=? ORDER BY place,user_id", (eid,))}

    def save_results(self, actor, eid, places, participants, previous):
        """Save the full standings and completion atomically; reject concurrent edits."""
        with self.db() as c:
            e = self._event(c, eid)
            if e["status"] not in ("published", "closed", "completed"):
                raise RuleError("Результаты доступны только для проведённого турнира.")
            current = {r[0] for r in c.execute("SELECT user_id FROM entries WHERE event_id=? AND status='confirmed'", (eid,))}
            if current != set(participants):
                raise RuleError("Состав изменился. Откройте ввод результатов заново.")
            saved = {r[0]:r[1] for r in c.execute("SELECT user_id,place FROM event_results WHERE event_id=?", (eid,))}
            if saved != previous:
                raise RuleError("Другой администратор уже изменил результаты. Откройте их заново.")
            if set(places) != current or any(type(v) is not int or not 1 <= v <= len(current) for v in places.values()):
                raise RuleError("Укажите место каждого игрока: от 1 до числа участников.")
            c.execute("DELETE FROM event_results WHERE event_id=?", (eid,))
            c.executemany("INSERT INTO event_results(event_id,user_id,place) VALUES(?,?,?)",
                          [(eid, uid, place) for uid,place in places.items()])
            c.execute("UPDATE events SET status='completed',dirty=1 WHERE id=?", (eid,))
            c.execute("UPDATE entries SET status='ready',offer_until=NULL WHERE event_id=? AND status='offered'", (eid,))
            c.execute("UPDATE outbox SET state='cancelled' WHERE event_id=? AND kind IN ('offer','reminder','results_request') AND state='pending'", (eid,))
            self.audit(c, actor, "results", [eid, places])

    def request_results(self, c, eid):
        admins = c.execute("SELECT value FROM settings WHERE key='result_admins'").fetchone()
        for uid in json.loads(admins[0]) if admins else []:
            self.enqueue(c, f"results_request:{eid}:{uid}", uid,
                         f"Турнир №{eid} завершён. Введите итоговые места участников в админке этого турнира.",
                         "results_request", eid, keyboard={"inline_keyboard": [[
                             {"text": "Ввести результаты", "callback_data": f"complete:{eid}"},
                             {"text": "Открыть турнир", "callback_data": f"ae:{eid}"}]]})

    def change_status(self, actor, eid, status):
        if status not in ("closed", "cancelled", "completed"):
            raise RuleError("Неизвестный статус.")
        with self.db() as c:
            e = self._event(c, eid)
            if e["status"] in ("cancelled", "completed"):
                raise RuleError("Турнир уже завершён или отменён.")
            if e["status"] == "draft":
                raise RuleError("Сначала опубликуйте турнир.")
            c.execute("UPDATE events SET status=?,dirty=1 WHERE id=?", (status, eid))
            kinds = "'offer'" if status == "closed" else "'offer','reminder'"
            c.execute(f"UPDATE outbox SET state='cancelled' WHERE event_id=? AND kind IN ({kinds}) AND state='pending'", (eid,))
            c.execute("UPDATE entries SET status='ready',offer_until=NULL WHERE event_id=? AND status='offered'", (eid,))
            if status == "cancelled":
                for r in c.execute("SELECT user_id FROM entries WHERE event_id=? AND status!='declined'", (eid,)).fetchall():
                    self.enqueue(c, f"cancel:{eid}:{r[0]}", r[0], f"Турнир №{eid} «{e['court']}» отменён.", "cancel", eid)
            self.audit(c, actor, "status", [eid, status])

    def entries(self, eid):
        with self.db() as c:
            return [dict(r) for r in c.execute("""SELECT en.*,u.username,u.display,u.full_name,u.level,u.dm_error
              FROM entries en JOIN users u ON en.user_id=u.id WHERE event_id=?
              ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'offered' THEN 1 WHEN 'ready' THEN 2
              WHEN 'unsure' THEN 3 ELSE 4 END, COALESCE(ready_at,joined_at),user_id""", (eid,))]

    def entry(self, eid, uid):
        with self.db() as c:
            r = c.execute("SELECT * FROM entries WHERE event_id=? AND user_id=?", (eid, uid)).fetchone()
            return dict(r) if r else None

    def enqueue(self, c, key, uid, text, kind, eid=None, revision=None, keyboard=None, due=None, expires=None):
        c.execute("""INSERT OR IGNORE INTO outbox(dedupe,user_id,text,keyboard,event_id,revision,kind,due,expires)
          VALUES(?,?,?,?,?,?,?,?,?)""", (key, uid, text, json.dumps(keyboard) if keyboard else None,
                                        eid, revision, kind, self.clock() if due is None else due, expires))

    def _fill(self, c, eid):
        e = self._event(c, eid)
        now = self.clock()
        if e["status"] != "published" or now >= min(e["close_at"], e["start"]):
            return
        occupied = c.execute("SELECT count(*) FROM entries WHERE event_id=? AND status IN ('confirmed','offered')", (eid,)).fetchone()[0]
        setting = c.execute("SELECT value FROM settings WHERE key='offer_minutes'").fetchone()
        minutes = json.loads(setting[0]) if setting else 30
        while occupied < e["capacity"]:
            r = c.execute("SELECT * FROM entries WHERE event_id=? AND status='ready' ORDER BY ready_at,user_id LIMIT 1", (eid,)).fetchone()
            if not r:
                break
            deadline = min(now + minutes * 60, e["close_at"], e["start"])
            version = r["offer_version"] + 1
            c.execute("UPDATE entries SET status='offered',offer_until=?,offer_version=? WHERE event_id=? AND user_id=?",
                      (deadline, version, eid, r["user_id"]))
            keyboard = {"inline_keyboard": [[
                {"text": "Подтвердить место", "callback_data": f"accept:{eid}:{version}"},
                {"text": "Отказаться", "callback_data": f"reject:{eid}:{version}"}]]}
            self.enqueue(c, f"offer:{eid}:{r['user_id']}:{version}", r["user_id"],
                         f"Для вас есть место в турнире №{eid} «{e['court']}»! Подтвердите до истечения предложения (не более {minutes} мин).",
                         "offer", eid, keyboard=keyboard, expires=deadline)
            occupied += 1
            c.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))

    def _expire(self, c, eid):
        rows = c.execute("SELECT user_id FROM entries WHERE event_id=? AND status='offered' AND offer_until<=?", (eid, self.clock())).fetchall()
        for r in rows:
            c.execute("UPDATE entries SET status='unsure',offer_until=NULL,ready_at=NULL WHERE event_id=? AND user_id=?", (eid, r[0]))
            self.enqueue(c, f"expired:{eid}:{r[0]}:{self.clock()}", r[0],
                         f"Предложение места в турнире №{eid} истекло. Вы отмечены как «Пока не уверен». Для новой заявки нажмите «Иду».", "info", eid)
        if rows:
            c.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))

    def join(self, eid, uid, choice, force=False, actor=None):
        if choice not in ("go", "queue", "no"):
            raise RuleError("Неизвестный вариант.")
        now = self.clock()
        with self.db() as c:
            e = self._event(c, eid)
            u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not u or not u["level"] or not u["private_started"]:
                raise RuleError("Сначала пройдите регистрацию в личном чате.")
            if e["status"] in ("draft", "trash", "cancelled", "completed"):
                raise RuleError("Запись на этот турнир недоступна.")
            if choice != "no":
                if (e["status"] != "published" or now >= min(e["start"], e["close_at"])) and not force:
                    raise RuleError("Запись закрыта.")
                if e.get("levels") and u["level"] not in e["levels"] and not force:
                    raise RuleError("Ваш уровень не подходит для этого турнира. Обратитесь к администратору.")
            self._expire(c, eid)
            self._fill(c, eid)
            old = c.execute("SELECT * FROM entries WHERE event_id=? AND user_id=?", (eid, uid)).fetchone()
            occupied = c.execute("SELECT count(*) FROM entries WHERE event_id=? AND status IN ('confirmed','offered') AND user_id!=?", (eid, uid)).fetchone()[0]
            if choice == "no":
                status = "declined"
            elif choice == "queue":
                status = "unsure"
            elif old and old["status"] == "confirmed":
                return "confirmed"
            elif old and old["status"] == "offered" and force:
                status = "confirmed"
            elif old and old["status"] == "offered":
                # An active offer is itself consent-required. The UI routes this to accept.
                raise RuleError("У вас есть предложение места. Подтвердите его кнопкой в личном сообщении.")
            else:
                status = "confirmed" if occupied < e["capacity"] else "ready"
            ready_at = old["ready_at"] if old and old["status"] == "ready" and status == "ready" else (now if status == "ready" else None)
            joined = old["joined_at"] if old and old["status"] == "confirmed" and status == "confirmed" else now
            c.execute("""INSERT INTO entries(event_id,user_id,status,ready_at,joined_at) VALUES(?,?,?,?,?)
              ON CONFLICT(event_id,user_id) DO UPDATE SET status=excluded.status,ready_at=excluded.ready_at,
              joined_at=excluded.joined_at,offer_until=NULL""", (eid, uid, status, ready_at, joined))
            if old and old["status"] == "offered":
                c.execute("UPDATE outbox SET state='cancelled' WHERE event_id=? AND user_id=? AND kind='offer' AND state='pending'", (eid, uid))
            c.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
            self._fill(c, eid)
            if actor:
                self.audit(c, actor, "move", [eid, uid, status])
            return status

    def answer_offer(self, eid, uid, version, accept):
        with self.db() as c:
            e = self._event(c, eid)
            r = c.execute("SELECT * FROM entries WHERE event_id=? AND user_id=?", (eid, uid)).fetchone()
            if not r or r["status"] != "offered" or r["offer_version"] != version or r["offer_until"] <= self.clock():
                raise RuleError("Это предложение уже недействительно.")
            if e["status"] != "published" or self.clock() >= e["close_at"]:
                raise RuleError("Запись закрыта.")
            c.execute("UPDATE entries SET status=?,joined_at=?,ready_at=NULL,offer_until=NULL WHERE event_id=? AND user_id=?",
                      ("confirmed" if accept else "declined", self.clock(), eid, uid))
            c.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
            c.execute("UPDATE outbox SET state='cancelled' WHERE event_id=? AND user_id=? AND kind='offer' AND state='pending'", (eid, uid))
            self._fill(c, eid)

    def paid(self, actor, eid, uid, value):
        with self.db() as c:
            e = self._event(c, eid)
            if not c.execute("SELECT 1 FROM entries WHERE event_id=? AND user_id=?", (eid, uid)).fetchone():
                raise RuleError("Нет записи игрока.")
            c.execute("UPDATE entries SET paid=?,paid_amount=? WHERE event_id=? AND user_id=?",
                      (int(value), e["price"] if value else 0, eid, uid))
            self.audit(c, actor, "payment", [eid, uid, value])

    def tick(self):
        with self.db() as c:
            now = self.clock()
            for er in c.execute("SELECT id FROM events WHERE status IN ('published','closed')").fetchall():
                eid = er[0]
                self._expire(c, eid)
                e = self._event(c, eid)
                if e["status"] == "published" and now >= e["close_at"]:
                    c.execute("UPDATE events SET status='closed',dirty=1 WHERE id=?", (eid,))
                    c.execute("UPDATE entries SET status='ready',offer_until=NULL WHERE event_id=? AND status='offered'", (eid,))
                    c.execute("UPDATE outbox SET state='cancelled' WHERE event_id=? AND kind='offer' AND state='pending'", (eid,))
                self._fill(c, eid)
                if now >= e["start"] + e["duration"] * 60:
                    c.execute("UPDATE events SET status='completed',dirty=1 WHERE id=?", (eid,))
                    self.request_results(c, eid)
                    continue
                for en in c.execute("SELECT * FROM entries WHERE event_id=? AND status='confirmed'", (eid,)).fetchall():
                    for hours in (24, 12, 2, 1):
                        due = e["start"] - hours * 3600
                        if due <= en["joined_at"]:
                            continue
                        self.enqueue(c, f"reminder:{eid}:{e['revision']}:{en['user_id']}:{hours}:{en['joined_at']}",
                                     en["user_id"], f"Напоминание: турнир №{eid} «{e['court']}» через {hours} ч. Проверьте карточку и локацию в разделе «Турниры».",
                                     "reminder", eid, e["revision"], due=due, expires=min(due + 900, e["start"]))

    def due_notifications(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM outbox WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 20", (self.clock(),))]

    def notification_valid(self, n):
        if n["expires"] and n["expires"] <= self.clock():
            return False
        if n["event_id"] and n["kind"] in ("offer", "reminder"):
            e = self.event(n["event_id"])
            en = self.entry(n["event_id"], n["user_id"])
            if e["status"] in ("draft", "trash", "cancelled", "completed") or not en:
                return False
            if n["kind"] == "offer":
                return en["status"] == "offered" and en["offer_until"] > self.clock()
            return (en["status"] == "confirmed" and n["revision"] == e["revision"]
                    and float(n["dedupe"].rsplit(":", 1)[1]) == en["joined_at"])
        return True

    def notification_result(self, n, state, error="", retry_after=None):
        with self.db() as c:
            c.execute("UPDATE outbox SET state=?,error=?,attempts=attempts+1,due=? WHERE id=?",
                      (state, error, self.clock() + (retry_after or min(300, 2 ** min(n["attempts"]+1, 8))), n["id"]))
            if error:
                c.execute("UPDATE users SET dm_error=? WHERE id=?", (error, n["user_id"]))

    def queue_broadcast(self, actor, target, text):
        with self.db() as c:
            if target == "all":
                users = c.execute("SELECT id FROM users WHERE private_started=1 AND level IS NOT NULL").fetchall()
            else:
                self._event(c, int(target))
                users = c.execute("SELECT user_id FROM entries WHERE event_id=? AND status!='declined'", (int(target),)).fetchall()
            batch = f"broadcast:{actor}:{self.clock()}"
            for r in users:
                self.enqueue(c, f"{batch}:{r[0]}", r[0], text, "broadcast")
            self.audit(c, actor, "broadcast", [target, len(users)])
            return len(users)

    def backup(self, target):
        with sqlite3.connect(self.path) as src, sqlite3.connect(target) as dst:
            src.backup(dst)



