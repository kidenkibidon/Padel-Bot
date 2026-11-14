import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core import RuleError, Store
from bot import App


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.i = 0

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        self.i += 1
        return {"message_id": self.i, "status": "administrator"}

    def send(self, chat, text, keyboard=None):
        return self.call("sendMessage", chat_id=chat, text=text, reply_markup=keyboard)


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"test.sqlite3"
        self.now = 1800000000.0
        self.s = Store(self.path, lambda: self.now)
        self.api = FakeAPI()
        self.app = App(self.s, self.api, owner=999, group=-123)
        self.app.bot_id = 700
        for uid in range(1, 15):
            self.s.touch_user({"id": uid, "first_name": f"Player {uid}"}, True)
            self.s.register(uid, "", "C")
        self.data = dict(photo="file123", court="Court & Club", format="Mexicano",
                         start=self.now+30*3600, close_at=self.now+29*3600,
                         duration=90, capacity=4, location="https://maps.google.com/?q=1,2",
                         price=15000, currency="AED", included="Water & balls", levels=["C"])
        self.eid = self.s.save_event(999, self.data)
        self.s.publish(999, self.eid)

    def tearDown(self):
        self.tmp.cleanup()

    def fill(self):
        for uid in range(1, 5):
            self.s.join(self.eid, uid, "go")

    def test_profile_unique_after_username_change(self):
        self.s.touch_user({"id": 1, "first_name": "New", "username": "new_name"}, True)
        self.s.register(1, "Different", "A")
        u = self.s.user(1)
        self.assertEqual(u["level"], "C")
        self.assertEqual(u["username"], "new_name")
        self.assertEqual(self.s.join(self.eid, 1, "go"), "confirmed")

    def test_last_place_concurrency(self):
        for uid in range(1, 4):
            self.s.join(self.eid, uid, "go")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda uid: self.s.join(self.eid, uid, "go"), range(4, 12)))
        self.assertEqual(results.count("confirmed"), 1)
        self.assertEqual(results.count("ready"), 7)
        self.assertEqual(sum(r["status"] == "confirmed" for r in self.s.entries(self.eid)), 4)

    def test_duplicate_click_not_duplicate_entry(self):
        self.s.join(self.eid, 1, "go")
        self.s.join(self.eid, 1, "go")
        self.assertEqual(len(self.s.entries(self.eid)), 1)

    def test_ready_queue_confirmation_and_reservation(self):
        self.fill()
        self.s.join(self.eid, 5, "go")
        self.now += 1
        self.s.join(self.eid, 6, "go")
        self.s.join(self.eid, 1, "no")
        offer = self.s.entry(self.eid, 5)
        self.assertEqual(offer["status"], "offered")
        self.assertEqual(self.s.join(self.eid, 7, "go"), "ready")
        self.s.answer_offer(self.eid, 5, offer["offer_version"], True)
        self.assertEqual(self.s.entry(self.eid, 5)["status"], "confirmed")
        with self.assertRaises(RuleError):
            self.s.answer_offer(self.eid, 5, offer["offer_version"], True)

    def test_offer_expiry_goes_to_next(self):
        self.fill()
        self.s.join(self.eid, 5, "go")
        self.now += 1
        self.s.join(self.eid, 6, "go")
        self.s.join(self.eid, 1, "no")
        self.now += 1801
        self.s.tick()
        self.assertEqual(self.s.entry(self.eid, 5)["status"], "unsure")
        self.assertEqual(self.s.entry(self.eid, 6)["status"], "offered")

    def test_unsure_not_promoted(self):
        self.s.join(self.eid, 5, "queue")
        self.s.tick()
        self.assertEqual(self.s.entry(self.eid, 5)["status"], "unsure")
        self.assertEqual(self.s.join(self.eid, 5, "go"), "confirmed")

    def test_ready_time_not_unsure_time_sets_priority(self):
        self.fill()
        self.s.join(self.eid, 5, "queue")
        self.now += 10
        self.s.join(self.eid, 6, "go")
        self.now += 10
        self.s.join(self.eid, 5, "go")
        self.s.join(self.eid, 1, "no")
        self.assertEqual(self.s.entry(self.eid, 6)["status"], "offered")

    def test_payment_retained_after_cancellation_private(self):
        self.s.join(self.eid, 1, "go")
        self.s.paid(999, self.eid, 1, True)
        self.s.join(self.eid, 1, "no")
        en = self.s.entry(self.eid, 1)
        self.assertEqual(en["paid_amount"], 15000)
        self.assertEqual(en["paid"], 1)
        text = self.app.card_text(self.s.event(self.eid))
        roster, _ = self.app.roster_text(self.eid)
        self.assertNotIn("Оплачено", text + roster)
        self.assertNotIn("paid", text + roster)

    def test_price_edit_keeps_historical_payment_currency_guard(self):
        self.s.join(self.eid, 1, "go")
        self.s.paid(999, self.eid, 1, True)
        changed = dict(self.data, price=20000)
        self.s.save_event(999, changed, self.eid)
        self.assertEqual(self.s.entry(self.eid, 1)["paid_amount"], 15000)
        with self.assertRaises(RuleError):
            self.s.save_event(999, dict(changed, currency="USD"), self.eid)

    def test_unauthorized_admin_callbacks_no_mutation(self):
        self.s.join(self.eid, 1, "go")
        actions = [f"paid:{self.eid}:1:1", "setlevel:1:8", f"move:{self.eid}:1:no",
                   "users:0", f"ae:{self.eid}", f"participants:{self.eid}:0", "broadcast"]
        for action in actions:
            self.app.handle({"callback_query": {"id": "q", "from": {"id": 1, "first_name": "P"},
                "data": action, "message": {"message_id": 1, "chat": {"id": 1, "type": "private"}}}})
        self.assertEqual(self.s.entry(self.eid, 1)["paid"], 0)
        self.assertEqual(self.s.entry(self.eid, 1)["status"], "confirmed")
        self.assertEqual(self.s.user(1)["level"], "C")
        self.assertTrue(all("Доступ только" in p["text"] for m,p in self.api.calls if m == "sendMessage"))

    def test_unregistered_and_level_constraints(self):
        self.s.touch_user({"id": 20, "first_name": "N"}, True)
        with self.assertRaises(RuleError):
            self.s.join(self.eid, 20, "go")
        self.s.set_level(999, 1, "B")
        with self.assertRaises(RuleError):
            self.s.join(self.eid, 1, "go")
        self.assertEqual(self.s.join(self.eid, 1, "go", force=True, actor=999), "confirmed")

    def test_capacity_cannot_shrink_below_occupied(self):
        data = dict(self.data, capacity=8)
        self.s.save_event(999, data, self.eid)
        for uid in range(1, 6):
            self.s.join(self.eid, uid, "go")
        with self.assertRaises(RuleError):
            self.s.save_event(999, self.data, self.eid)

    def test_closed_cancelled_completed(self):
        self.fill()
        self.s.join(self.eid, 5, "go")
        self.s.join(self.eid, 1, "no")
        self.s.change_status(999, self.eid, "closed")
        self.assertEqual(self.s.entry(self.eid, 5)["status"], "ready")
        with self.assertRaises(RuleError):
            self.s.join(self.eid, 6, "go")
        self.s.join(self.eid, 2, "no")
        self.s.change_status(999, self.eid, "cancelled")
        self.s.tick()
        self.assertFalse(any(n["kind"] in ("offer", "reminder") for n in self.s.due_notifications()))

    def test_reminder_dedup_after_restart(self):
        self.s.join(self.eid, 1, "go")
        self.s.tick()
        self.now += 6*3600
        self.s.tick()
        due = [n for n in self.s.due_notifications() if n["kind"] == "reminder"]
        self.assertEqual(len(due), 1)
        self.assertTrue(self.s.notification_valid(due[0]))
        self.s.notification_result(due[0], "sent")
        self.s = Store(self.path, lambda: self.now)
        self.s.tick()
        self.assertFalse(self.s.due_notifications())

    def test_late_join_no_past_reminders(self):
        self.now += 29.2*3600
        self.s.join(self.eid, 1, "go", force=True, actor=999)
        self.s.tick()
        with self.s.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM outbox WHERE kind='reminder'").fetchone()[0], 0)

    def test_reschedule_invalidates_old_reminders(self):
        self.s.join(self.eid, 1, "go")
        self.s.tick()
        self.now += 6*3600
        old = self.s.due_notifications()[0]
        self.s.save_event(999, dict(self.data, start=self.data["start"]+86400, close_at=self.data["close_at"]+86400), self.eid)
        self.assertFalse(self.s.notification_valid(old))
        self.s.tick()
        self.assertFalse(any(n["kind"] == "reminder" for n in self.s.due_notifications()))

    def test_cancelled_then_rejoined_old_reminder_invalid(self):
        self.s.join(self.eid, 1, "go")
        self.s.tick()
        self.now += 6*3600
        old = self.s.due_notifications()[0]
        self.s.join(self.eid, 1, "no")
        self.now += 1
        self.s.join(self.eid, 1, "go")
        self.assertFalse(self.s.notification_valid(old))

    def test_publish_optional_photo_and_required_future(self):
        eid = self.s.save_event(999, dict(self.data, photo=None))
        self.s.publish(999, eid)
        self.assertEqual(self.s.event(eid)["status"], "published")
        past = self.s.save_event(999, dict(self.data, start=self.now-1, close_at=self.now-2))
        with self.assertRaises(RuleError):
            self.s.publish(999, past)
        with self.assertRaises(RuleError):
            self.s.publish(999, self.eid)

    def test_backup_restore_profiles_entries_payment(self):
        self.s.join(self.eid, 1, "go")
        self.s.paid(999, self.eid, 1, True)
        backup = Path(self.tmp.name)/"backup.sqlite3"
        self.s.backup(backup)
        restored = Store(backup, lambda: self.now)
        self.assertEqual(restored.user(1)["level"], "C")
        self.assertEqual(restored.entry(self.eid, 1)["paid_amount"], 15000)

    def test_html_escape_and_paginated_roster(self):
        self.s.touch_user({"id": 1, "first_name": "<script>&"}, True)
        self.s.join(self.eid, 1, "go")
        text, _ = self.app.roster_text(self.eid)
        self.assertIn("&lt;script&gt;&amp;", text)
        self.assertIn("Court &amp; Club", self.app.card_text(self.s.event(self.eid)))

    def test_registration_flow_with_return_to_event(self):
        uid = 100
        def msg(text):
            self.app.handle({"message": {"from": {"id": uid, "first_name": "Егор"}, "chat": {"id": uid, "type": "private"}, "text": text}})
        def click(data):
            self.app.handle({"callback_query": {"id": "q", "from": {"id": uid, "first_name": "Егор"},
                "data": data, "message": {"message_id": 1, "chat": {"id": uid, "type": "private"}}}})
        msg(f"/start ego_{self.eid}")
        self.assertEqual(self.s.session(uid)["step"], "level")
        click("level:3")
        self.assertEqual(self.s.user(uid)["level"], "C")
        self.assertEqual(self.s.entry(self.eid, uid)["status"], "confirmed")
        self.assertIsNone(self.s.session(uid))

    def test_group_action_redirects_privately_without_public_payment(self):
        self.app.handle({"callback_query": {"id": "q", "from": {"id": 1, "first_name": "P"}, "data": f"go:{self.eid}",
            "message": {"message_id": 10, "chat": {"id": -123, "type": "supergroup"}}}})
        call = self.api.calls[-1]
        self.assertEqual(call[0], "answerCallbackQuery")
        self.assertIn(f"ego_{self.eid}", call[1]["url"])
        self.assertIsNone(self.s.entry(self.eid, 1))

    def test_photo_publication_refresh_does_not_duplicate(self):
        self.app.refresh()
        e = self.s.event(self.eid)
        self.assertIsNotNone(e["photo_msg"])
        self.assertIsNotNone(e["roster_msg"])
        self.s.join(self.eid, 1, "go")
        self.app.refresh()
        self.assertEqual(sum(m == "sendPhoto" for m,p in self.api.calls), 1)
        self.assertEqual(sum(m == "sendMessage" for m,p in self.api.calls), 1)
        self.assertTrue(any(m == "editMessageText" for m,p in self.api.calls))

    def test_broadcast_is_private_and_not_sent_without_confirm(self):
        self.app.admin_callback(999, ["broadcast"])
        self.assertEqual(self.s.due_notifications(), [])
        self.s.session(999, {"kind": "broadcast_confirm", "target": str(self.eid), "text": "Hello"})
        self.s.join(self.eid, 1, "go")
        self.app.admin_callback(999, ["broadcast_confirm"])
        self.assertEqual(len(self.s.due_notifications()), 1)
        self.assertEqual(self.s.due_notifications()[0]["user_id"], 1)

    def test_wizard_persists_and_creates_draft(self):
        session = {"kind": "wizard", "index": 0, "data": dict(self.data)}
        self.s.session(999, session)
        restored = Store(self.path, lambda: self.now)
        self.assertEqual(restored.session(999)["data"]["court"], self.data["court"])
        self.app.save_wizard(999, session)
        self.assertEqual(len(self.s.events()), 2)
        self.assertEqual(self.s.events()[-1]["status"], "draft")

    def test_full_admin_wizard_to_publication(self):
        self.app.admin_callback(999, ["new"])
        inputs = [
            {"photo": [{"file_id": "new_photo"}]},
            {"text": "Padel Club"}, {"text": "Americano"}, {"text": "20.01.2030 19:00"},
            {"text": "90"}, {"text": "8"}, {"location": {"latitude": 25.1, "longitude": 55.2}},
            {"text": "150.50"}, {"text": "AED"}, {"text": "Вода и мячи"},
            {"text": "C-, C, C+"}, {"text": "-"},
        ]
        for payload in inputs:
            self.app.handle({"message": {"from": {"id": 999, "first_name": "Егор"},
                "chat": {"id": 999, "type": "private"}, **payload}})
        event = self.s.events()[-1]
        self.assertEqual(event["price"], 15050)
        self.assertEqual(event["capacity"], 8)
        self.assertEqual(event["status"], "draft")
        self.assertIsNone(self.s.session(999))
        self.app.admin_callback(999, ["publish", str(event["id"])])
        self.app.refresh()
        self.assertIsNotNone(self.s.event(event["id"])["roster_msg"])

    def test_long_public_lists_fit_and_private_names_remain_complete(self):
        for uid in range(30, 230):
            self.s.touch_user({"id": uid, "first_name": "Имя" * 20, "username": "u" * 25 + str(uid)}, True)
            self.s.register(uid, "Полное имя" * 10, "C")
            self.s.join(self.eid, uid, "no")
        text = self.app.card_text(self.s.event(self.eid))
        self.assertLess(len(text), 3900)
        self.assertIn("Полный список", text)
        roster, _ = self.app.roster_text(self.eid)
        self.assertIn("Имя" * 20, roster)
        self.assertNotIn("tg://", roster)
        self.assertNotIn("@", roster)

    def wiz(self, action, value=None, uid=999):
        session = self.s.session(uid)
        parts = ["wiz", session["token"], action]
        if value is not None:
            parts.append(str(value))
        self.app.wizard_callback(uid, parts)

    def test_exact_eight_levels(self):
        self.assertEqual(self.app.levels, ["D", "D+", "C-", "C", "C+", "B-", "B", "B+"])

    def test_all_steps_skip_to_incomplete_draft_and_publish_guard(self):
        self.app.admin_callback(999, ["new"])
        for _ in range(12):
            self.wiz("skip")
        self.assertIsNone(self.s.session(999))
        event = self.s.events()[0]  # Undated draft sorts first.
        self.assertEqual(event["status"], "draft")
        self.assertEqual(json.loads(event["data"]), {})
        text = self.app.card_text(event)
        for unwanted in ("💰", "Включено", "Открыть карту", "1970", "*", "None"):
            self.assertNotIn(unwanted, text)
        with self.assertRaisesRegex(RuleError, "Дата и время"):
            self.s.publish(999, event["id"])

    def test_multiselect_toggle_and_stale_buttons(self):
        self.app.wizard_prompt(999, {"kind": "wizard", "index": 10, "data": dict(self.data, levels=[])})
        old = self.s.session(999)["token"]
        self.wiz("level", 0)
        self.assertEqual(self.s.session(999)["token"], old)
        self.wiz("level", 2)
        self.assertEqual(self.s.session(999)["data"]["levels"], ["D", "C-"])
        self.wiz("level", 0)
        self.wiz("done")
        self.assertEqual(self.s.session(999)["index"], 11)
        with self.assertRaises(RuleError):
            self.app.wizard_callback(999, ["wiz", old, "level", "1"])
        self.assertEqual(self.s.session(999)["data"]["levels"], ["C-"])
        self.wiz("back")
        self.assertEqual(self.s.session(999)["index"], 10)

    def test_draft_autosave_restart_cancel_trash_restore_delete(self):
        self.app.admin_callback(999, ["new"])
        self.wiz("skip")
        session = self.s.session(999)
        session["data"]["court"] = "Saved court"
        session["index"] = 2
        self.app.wizard_prompt(999, session)
        eid = self.s.session(999)["eid"]
        restored = Store(self.path, lambda: self.now)
        self.assertEqual(restored.event(eid)["court"], "Saved court")
        self.assertEqual(restored.session(999)["index"], 2)
        self.wiz("cancel")
        self.assertIsNone(self.s.session(999))
        self.s.trash_draft(999, eid)
        self.assertEqual(self.s.event(eid)["status"], "trash")
        with self.assertRaises(RuleError):
            self.s.publish(999, eid)
        self.s.trash_draft(999, eid, restore=True)
        self.assertEqual(self.s.event(eid)["court"], "Saved court")
        with self.assertRaises(RuleError):
            self.s.trash_draft(999, eid, delete=True)
        self.s.trash_draft(999, eid)
        self.app.admin_callback(999, ["delete_draft", str(eid)])
        self.assertEqual(self.s.event(eid)["status"], "trash")
        self.app.admin_callback(999, ["delete_yes", str(eid)])
        with self.assertRaises(RuleError):
            self.s.event(eid)
        with self.assertRaises(RuleError):
            self.s.trash_draft(999, self.eid)

    def test_copy_is_clean_draft_without_date_entries_or_payments(self):
        self.s.join(self.eid, 1, "go")
        self.s.paid(999, self.eid, 1, True)
        self.app.admin_callback(999, ["copy", str(self.eid)])
        session = self.s.session(999)
        eid = session["eid"]
        self.assertNotEqual(eid, self.eid)
        self.assertEqual(self.s.event(eid)["status"], "draft")
        self.assertEqual(self.s.entries(eid), [])
        data = json.loads(self.s.event(eid)["data"])
        self.assertNotIn("start", data)
        self.assertNotIn("close_at", data)
        self.assertEqual(data["court"], self.data["court"])
        self.assertEqual(self.s.entry(self.eid, 1)["paid"], 1)

    def test_publish_without_optional_fields_and_photo_refresh(self):
        data = {k:v for k,v in self.data.items() if k not in ("photo", "included", "location", "price", "currency", "close_at", "levels")}
        eid = self.s.save_event(999, data)
        self.s.publish(999, eid)
        self.app.refresh()
        event = self.s.event(eid)
        self.assertIsNotNone(event["roster_msg"])
        self.assertIsNone(event["photo_msg"])
        self.assertFalse(event["dirty"])
        text = self.app.card_text(event)
        for value in ("💰", "Включено", "Открыть карту", "Запись до", "Уровни:"):
            self.assertNotIn(value, text)
        self.s.join(eid, 1, "go")
        self.s.tick()
        self.assertEqual(self.s.entry(eid, 1)["status"], "confirmed")

    def test_published_edit_autosave_does_not_change_live_until_saved(self):
        self.s.join(self.eid, 1, "go")
        self.app.admin_callback(999, ["edit", str(self.eid)])
        session = self.s.session(999)
        session["data"]["court"] = "New court"
        self.app.wizard_prompt(999, session)
        self.assertEqual(self.s.event(self.eid)["court"], self.data["court"])
        self.assertFalse(any(n["kind"] == "change" for n in self.s.due_notifications()))
        self.app.pause_wizard(999)
        draft = [e for e in self.s.events() if e["status"] == "draft"][0]
        self.assertEqual(draft["court"], "New court")
        self.assertEqual(self.s.event(self.eid)["court"], self.data["court"])
        self.assertEqual(self.s.entries(draft["id"]), [])

    def test_notification_describes_date_court_and_location_changes(self):
        self.s.join(self.eid, 1, "go")
        self.s.save_event(999, dict(self.data, court="New court", location="https://example.com/map", start=self.data["start"]+3600), self.eid)
        notices = [n for n in self.s.due_notifications() if n["kind"] == "change"]
        self.assertEqual(len(notices), 1)
        for part in ("Дата и время", "Корт", "Локация", "New court", "→"):
            self.assertIn(part, notices[0]["text"])

    def test_welcome_plain_name_dedupes_and_no_repeat_rating(self):
        user = {"id": 100, "first_name": "<Iegor>", "username": "secret_name"}
        self.app.welcome(-123, user, self.now)
        self.app.welcome(-123, user, self.now)
        self.assertEqual(len(self.api.calls), 1)
        text = self.api.calls[0][1]["text"]
        self.assertIn("&lt;Iegor&gt;, добро пожаловать", text)
        self.assertNotIn("secret_name", text)
        self.assertNotIn("tg://", text)
        self.assertTrue(self.api.calls[0][1]["reply_markup"])
        self.s.register(100, "", "C")
        self.app.welcome(-123, user, self.now+100)
        self.assertIsNone(self.api.calls[-1][1]["reply_markup"])
        self.assertEqual(self.s.user(100)["level"], "C")
        self.app.welcome(-999, user, self.now+200)
        self.assertEqual(len(self.api.calls), 2)

    def test_chat_member_and_service_join_welcome_only_once(self):
        user = {"id": 100, "first_name": "Player"}
        self.app.handle({"chat_member": {"chat": {"id": -123}, "date": self.now,
            "old_chat_member": {"status": "left", "user": user},
            "new_chat_member": {"status": "member", "user": user}}})
        self.app.handle({"message": {"from": {"id": 999}, "chat": {"id": -123, "type": "supergroup"},
            "new_chat_members": [user], "date": self.now}})
        self.assertEqual(sum(m == "sendMessage" for m,p in self.api.calls), 1)

    def test_bind_posts_registration_once_and_preserves_ratings(self):
        message = {"from": {"id": 999}, "chat": {"id": -123, "type": "supergroup"}, "text": "/bind"}
        self.app.handle_message(message)
        self.app.handle_message(message)
        posts = [p for m,p in self.api.calls if m == "sendMessage" and "Выберите свой рейтинг" in p["text"]]
        self.assertEqual(len(posts), 1)
        self.assertEqual(sum(m == "pinChatMessage" for m,p in self.api.calls), 2)
        self.assertEqual(self.s.user(1)["level"], "C")

    def test_included_buttons_and_presets(self):
        self.app.wizard_prompt(999, {"kind": "wizard", "index": 9, "data": {}})
        self.wiz("included", 0)
        self.wiz("included", 2)
        self.wiz("done")
        self.assertEqual(self.s.session(999)["data"]["included"], "Вода, Мячи")
        self.wiz("back")
        self.wiz("skip")
        self.assertNotIn("included", self.s.session(999)["data"])
        self.app.pause_wizard(999)
        self.app.wizard_prompt(999, {"kind": "wizard", "index": 5, "data": {}})
        self.wiz("value", 2)
        self.assertEqual(self.s.session(999)["data"]["capacity"], 12)

    def test_unauthorized_wizard_cannot_mutate(self):
        self.app.admin_callback(999, ["new"])
        token = self.s.session(999)["token"]
        with self.assertRaises(RuleError):
            self.app.wizard_callback(1, ["wiz", token, "skip"])
        self.assertEqual(self.s.session(999)["index"], 0)

    def test_start_cannot_reset_registered_level(self):
        self.app.handle_message({"from": {"id": 1, "first_name": "Player"}, "chat": {"id": 1, "type": "private"}, "text": "/start"})
        self.assertIsNone(self.s.session(1))
        self.assertEqual(self.s.user(1)["level"], "C")

    def test_played_count_only_completed_confirmed_and_survives_restart(self):
        self.s.join(self.eid, 1, "go")
        self.s.join(self.eid, 2, "go")
        self.s.join(self.eid, 2, "no")
        self.s.join(self.eid, 3, "queue")
        self.assertEqual(self.s.tournaments_played(1), 0)
        self.s.change_status(999, self.eid, "completed")
        self.assertEqual(self.s.tournaments_played(1), 1)
        self.assertEqual(self.s.tournaments_played(2), 0)
        self.assertEqual(self.s.tournaments_played(3), 0)
        self.assertEqual(self.s.tournaments_played(4), 0)
        self.s.tick()
        restored = Store(self.path, lambda: self.now)
        self.assertEqual(restored.tournaments_played(1), 1)
        with self.assertRaises(RuleError):
            self.s.change_status(999, self.eid, "completed")
        self.assertEqual(self.s.tournaments_played(1), 1)
        second = self.s.save_event(999, self.data)
        self.s.publish(999, second)
        self.s.join(second, 1, "go")
        self.s.change_status(999, second, "cancelled")
        self.assertEqual(self.s.tournaments_played(1), 1)
        third = self.s.save_event(999, self.data)
        self.s.publish(999, third)
        self.s.join(third, 1, "go")
        self.s.join(third, 1, "go")
        self.s.change_status(999, third, "completed")
        self.assertEqual(self.s.tournaments_played(1), 2)

    def test_played_count_on_automatic_completion_and_in_profiles(self):
        self.s.join(self.eid, 1, "go")
        self.now = self.data["start"] + self.data["duration"]*60 + 1
        self.s.tick()
        self.s.tick()
        self.assertEqual(self.s.tournaments_played(1), 1)
        self.app.person(999, 1)
        self.assertIn("Сыграно турниров: 1", self.api.calls[-1][1]["text"])
        self.app.callback({"id": "profile", "from": {"id": 1, "first_name": "Player"},
            "data": "profile", "message": {"chat": {"id": 1, "type": "private"}}})
        self.assertIn("Сыграно турниров: 1", self.api.calls[-1][1]["text"])

    def test_results_flow_ties_preview_confirm_and_profile_count(self):
        self.s.join(self.eid, 1, "go")
        self.s.join(self.eid, 2, "go")
        self.app.admin_callback(999, ["complete", str(self.eid)])
        self.assertEqual(self.s.event(self.eid)["status"], "published")
        for _ in range(2):
            session = self.s.session(999)
            self.app.admin_callback(999, ["placing", session["token"], "1"])
        session = self.s.session(999)
        self.assertEqual(self.s.results(self.eid), {})
        self.assertEqual(session["index"], 2)
        self.app.admin_callback(999, ["results_save", session["token"]])
        self.assertEqual(self.s.results(self.eid), {1:1, 2:1})
        self.assertEqual(self.s.event(self.eid)["status"], "completed")
        self.assertEqual(self.s.tournaments_played(1), 1)
        self.assertIn("1 место", self.api.calls[-1][1]["text"])
        restored = Store(self.path, lambda: self.now)
        self.assertEqual(restored.results(self.eid), {1:1, 2:1})
        with self.assertRaises(RuleError):
            self.app.admin_callback(999, ["results_save", session["token"]])

    def test_results_validate_full_roster_places_and_concurrent_edits(self):
        self.s.join(self.eid, 1, "go")
        self.s.join(self.eid, 2, "go")
        for places in ({1:1}, {1:0,2:1}, {1:3,2:1}, {1:True,2:1}):
            with self.assertRaises(RuleError):
                self.s.save_results(999, self.eid, places, [1,2], {})
        self.assertEqual(self.s.results(self.eid), {})
        with self.assertRaises(RuleError):
            self.s.save_results(999, self.eid, {1:1,2:2}, [1], {})
        self.s.save_results(999, self.eid, {1:1,2:2}, [1,2], {})
        with self.assertRaises(RuleError):
            self.s.save_results(999, self.eid, {1:2,2:1}, [1,2], {})
        self.s.save_results(999, self.eid, {1:2,2:1}, [1,2], {1:1,2:2})
        self.assertEqual(self.s.results(self.eid), {2:1,1:2})
        self.assertEqual(self.s.tournaments_played(1), 1)

    def test_automatic_completion_requests_results_once_per_admin(self):
        self.s.join(self.eid, 1, "go")
        self.now = self.data["start"] + self.data["duration"]*60 + 1
        self.s.tick()
        self.s.tick()
        requests = [n for n in self.s.due_notifications() if n["kind"] == "results_request"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["user_id"], 999)
        self.assertIn(f"complete:{self.eid}", requests[0]["keyboard"])
        self.app.start_results(999, self.eid)
        session = self.s.session(999)
        self.app.record_place(999, session, 1)
        self.app.admin_callback(999, ["results_save", self.s.session(999)["token"]])
        self.assertFalse(any(n["kind"] == "results_request" for n in self.s.due_notifications()))

    def test_results_cancel_back_restart_and_unauthorized_access(self):
        self.s.join(self.eid, 1, "go")
        self.app.start_results(999, self.eid)
        initial = self.s.session(999)
        with self.assertRaises(RuleError):
            self.app.start_results(1, self.eid)
        with self.assertRaises(RuleError):
            self.app.record_place(999, initial, 0)
        self.app.admin_callback(999, ["placing", initial["token"], "1"])
        with self.assertRaises(RuleError):
            self.app.admin_callback(999, ["placing", initial["token"], "1"])
        session = Store(self.path, lambda: self.now).session(999)
        self.assertEqual(session["places"], {"1":1})
        self.app.admin_callback(999, ["results_back", session["token"]])
        self.assertEqual(self.s.session(999)["index"], 0)
        self.app.admin_callback(999, ["results_cancel", self.s.session(999)["token"]])
        self.assertEqual(self.s.event(self.eid)["status"], "published")
        self.assertEqual(self.s.results(self.eid), {})

    def test_completed_archive_and_results_message_input(self):
        self.s.join(self.eid, 1, "go")
        self.app.start_results(999, self.eid)
        self.app.handle_message({"from": {"id":999,"first_name":"Admin"}, "chat":{"id":999,"type":"private"}, "text":"1"})
        self.app.admin_callback(999, ["results_save", self.s.session(999)["token"]])
        self.app.event_list(999, "completed_events")
        buttons = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["callback_data"], f"ae:{self.eid}")


if __name__ == "__main__":
    unittest.main()



