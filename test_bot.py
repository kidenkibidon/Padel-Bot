import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core import RuleError, Store, level_for_rating
from bot import App, APIError


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
        click("level_v2:C")
        self.assertEqual(self.s.user(uid)["level"], "C")
        self.assertEqual(self.s.entry(self.eid, uid)["status"], "confirmed")
        self.assertIsNone(self.s.session(uid))

    def test_group_signup_uses_private_popup_without_chat_messages(self):
        self.app.handle({"callback_query": {"id": "q", "from": {"id": 1, "first_name": "P"}, "data": f"go:{self.eid}",
            "message": {"message_id": 10, "chat": {"id": -123, "type": "supergroup"}}}})
        call = self.api.calls[-1]
        self.assertEqual(call[0], "answerCallbackQuery")
        self.assertIn("Вы записаны", call[1]["text"])
        self.assertEqual(self.s.entry(self.eid, 1)["status"], "confirmed")
        self.assertFalse(any(m == "sendMessage" for m,p in self.api.calls))

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
            {"text": "D+, C, C+"}, {"text": "-"},
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
        self.assertEqual(self.app.levels, ["D", "D+", "C", "C+", "B-", "B", "B+", "A"])

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
        self.assertEqual(self.s.session(999)["data"]["levels"], ["D", "C"])
        self.wiz("level", 0)
        self.wiz("done")
        self.assertEqual(self.s.session(999)["index"], 11)
        with self.assertRaises(RuleError):
            self.app.wizard_callback(999, ["wiz", old, "level", "1"])
        self.assertEqual(self.s.session(999)["data"]["levels"], ["C"])
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
        posts = [p for m,p in self.api.calls if m == "sendMessage" and "Выберите свой уровень игры" in p["text"]]
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

    def test_rating_strength_bonus_and_favorite_loss_are_bounded(self):
        places = {1:1, 2:2, 3:3, 4:4, 5:5}
        equal = Store.rating_changes({uid:2.0 for uid in places}, places)
        upset = Store.rating_changes({1:2.0, 2:3.5, 3:3.5, 4:3.5, 5:3.5}, places)
        self.assertAlmostEqual(equal[1], 0.1)
        self.assertGreater(upset[1], 0.19)
        self.assertLess(upset[1], 0.20)
        favorite_loss = Store.rating_changes({1:2.0,2:2.0,3:2.0,4:2.0,5:3.5}, places)
        self.assertLess(favorite_loss[5], equal[5])
        self.assertGreaterEqual(favorite_loss[5], -0.20)
        self.assertAlmostEqual(sum(equal.values()), 0.0)
        self.assertAlmostEqual(sum(upset.values()), 0.0)
        for count in (2,4,8,12,24):
            values = Store.rating_changes({uid:2.0 for uid in range(count)}, {uid:uid+1 for uid in range(count)})
            self.assertAlmostEqual(values[0], 0.1)
            self.assertTrue(all(abs(delta) <= 0.2 for delta in values.values()))

    def test_rating_ties_single_player_and_zero_floor(self):
        tied = Store.rating_changes({1:2.0,2:2.0}, {1:1,2:1})
        self.assertEqual(tied, {1:0.0,2:0.0})
        self.assertEqual(Store.rating_changes({1:2.0}, {1:1}), {1:0.0})
        low = Store.rating_changes({1:0.01,2:0.01}, {1:2,2:1})
        self.assertEqual(low[1], -0.01)

    def test_rating_uses_only_confirmed_results_never_attendance(self):
        self.s.join(self.eid, 1, "go")
        self.s.join(self.eid, 2, "go")
        self.s.change_status(999, self.eid, "completed")
        self.assertEqual(self.s.rating_profile(1)["rating"], 2.75)
        self.assertEqual(self.s.rating_profile(1)["rated_events"], 0)
        self.assertEqual(self.s.leaderboard(), [])
        self.s.save_results(999, self.eid, {1:1,2:2}, [1,2], {})
        self.assertAlmostEqual(self.s.rating_profile(1)["rating"], 2.85)
        self.assertAlmostEqual(self.s.rating_profile(2)["rating"], 2.65)
        self.assertEqual(self.s.rating_profile(1)["wins"], 1)
        self.assertEqual(self.s.rating_profile(1)["rank"], 1)
        self.assertEqual(self.s.rating_profile(2)["rank"], 2)
        self.assertEqual(self.s.rating_profile(3)["rating"], 2.75)
        self.assertIsNone(self.s.rating_profile(3)["rank"])
        self.assertEqual(self.s.rating_profile(3)["rated_events"], 0)

    def test_historical_correction_replays_later_events_without_double_credit(self):
        for uid in (1,2):
            self.s.join(self.eid, uid, "go")
        self.s.save_results(999, self.eid, {1:1,2:2}, [1,2], {})
        second = self.s.save_event(999, dict(self.data, start=self.data["start"]+86400, close_at=self.data["close_at"]+86400))
        self.s.publish(999, second)
        for uid in (1,2):
            self.s.join(second, uid, "go")
        self.s.save_results(999, second, {1:1,2:2}, [1,2], {})
        self.assertAlmostEqual(self.s.event_rating_changes(second)[1]["before_rating"], 2.85)
        self.s.save_results(999, self.eid, {1:2,2:1}, [1,2], {1:1,2:2})
        history = self.s.event_rating_changes(second)
        self.assertAlmostEqual(history[1]["before_rating"], 2.65)
        expected = 2.65 + Store.rating_changes({1:2.65,2:2.85}, {1:1,2:2})[1]
        self.assertAlmostEqual(self.s.rating_profile(1)["rating"], expected)
        self.assertEqual(self.s.rating_profile(1)["rated_events"], 2)
        self.assertEqual(self.s.rating_profile(1)["wins"], 1)
        self.s.save_results(999, self.eid, {1:2,2:1}, [1,2], {1:2,2:1})
        self.assertAlmostEqual(self.s.rating_profile(1)["rating"], expected)
        self.now += 365*86400*5
        restored = Store(self.path, lambda: self.now)
        self.assertAlmostEqual(restored.rating_profile(1)["rating"], expected)
        self.assertEqual(restored.rating_profile(1)["rated_events"], 2)

    def test_ratings_rebuild_in_playing_order_even_if_results_entered_late(self):
        for uid in (1,2):
            self.s.join(self.eid, uid, "go")
        second = self.s.save_event(999, dict(self.data, start=self.data["start"]+86400, close_at=self.data["close_at"]+86400))
        self.s.publish(999, second)
        for uid in (1,2):
            self.s.join(second, uid, "go")
        self.s.save_results(999, second, {1:1,2:2}, [1,2], {})
        self.s.save_results(999, self.eid, {1:1,2:2}, [1,2], {})
        self.assertAlmostEqual(self.s.event_rating_changes(self.eid)[1]["before_rating"], 2.75)
        self.assertAlmostEqual(self.s.event_rating_changes(second)[1]["before_rating"], 2.85)
        self.assertEqual(self.s.rating_profile(1)["rated_events"], 2)

    def test_rating_ui_profiles_group_access_and_level_filter(self):
        for uid in (1,2):
            self.s.join(self.eid, uid, "go")
        self.s.save_results(999, self.eid, {1:1,2:2}, [1,2], {})
        self.s.set_level(999, 2, "B")
        self.assertEqual([r["user_id"] for r in self.s.leaderboard("C")], [1,2])
        self.app.person(999, 1)
        self.assertIn("Мой Рейтинг 🏆: 2.85", self.api.calls[-1][1]["text"])
        self.app.callback({"id":"q", "from":{"id":1,"first_name":"Player"}, "data":"ranking:all:0",
                           "message":{"chat":{"id":-123,"type":"supergroup"}}})
        self.assertEqual(self.api.calls[-1][0], "answerCallbackQuery")
        self.assertTrue(self.api.calls[-1][1]["show_alert"])
        self.assertIn("2.85", self.api.calls[-1][1]["text"])
        with self.assertRaises(RuleError):
            self.app.callback({"id":"q", "from":{"id":1}, "data":"ranking:all:0",
                               "message":{"chat":{"id":-999,"type":"supergroup"}}})
        saved = self.app.results_text(self.eid, self.s.results(self.eid))
        preview = self.app.results_text(self.eid, self.s.results(self.eid), include_rating=False)
        self.assertIn("+0.10", saved)
        self.assertNotIn("+0.10", preview)

    def test_rating_migration_populates_existing_result_history(self):
        for uid in (1,2):
            self.s.join(self.eid, uid, "go")
        self.s.save_results(999, self.eid, {1:1,2:2}, [1,2], {})
        with self.s.db() as c:
            c.execute("DELETE FROM player_ratings")
            c.execute("DELETE FROM rating_history")
            c.execute("DELETE FROM settings WHERE key='rating_algorithm'")
        restored = Store(self.path, lambda: self.now)
        self.assertAlmostEqual(restored.rating_profile(1)["rating"], 2.85)
        self.assertEqual(len(restored.event_rating_changes(self.eid)), 2)

    def test_level_start_midpoints_and_registration_cannot_reset_seed(self):
        mapping = {"D":1.5,"D+":2.25,"C":2.75,"C+":3.25,"B-":4.50,"B":4.00,"B+":5.25,"A":6.5}
        for uid,(level,value) in enumerate(mapping.items(), start=101):
            self.s.touch_user({"id":uid,"first_name":level}, True)
            self.s.register(uid, "", level)
            self.assertEqual(self.s.rating_profile(uid)["rating"], value)
            self.assertEqual(self.s.user(uid)["rating_seed"], value)
            self.s.register(uid, "", "A")
            self.assertEqual(self.s.user(uid)["level"], level)
            self.assertEqual(self.s.rating_profile(uid)["rating"], value)
        with self.assertRaises(RuleError):
            self.s.register(101, "", "C-")

    def test_level_seeds_used_for_opponent_adjusted_first_tournament(self):
        self.s.set_level(999, 1, "D")
        self.s.set_level(999, 2, "B")
        unrestricted = dict(self.data, levels=[])
        eid = self.s.save_event(999, unrestricted)
        self.s.publish(999, eid)
        for uid in (1,2):
            self.s.join(eid, uid, "go")
        self.s.save_results(999, eid, {1:1,2:2}, [1,2], {})
        history = self.s.event_rating_changes(eid)
        self.assertEqual(history[1]["before_rating"], 1.5)
        self.assertEqual(history[2]["before_rating"], 4.00)
        self.assertGreater(history[1]["delta"], 0.19)
        # A later letter correction does not reset established competitive history.
        before = self.s.rating_profile(1)["rating"]
        self.s.set_level(999, 1, "C")
        self.assertEqual(self.s.rating_profile(1)["rating"], before)
        self.assertEqual(self.s.user(1)["rating_seed"], 1.5)

    def test_legacy_level_migration_keeps_c_minus_pending_until_admin_clarifies(self):
        with self.s.db() as c:
            c.execute("UPDATE users SET level='C-',rating_seed=NULL WHERE id=1")
            c.execute("UPDATE users SET rating_seed=NULL WHERE id=2")
            c.execute("INSERT OR REPLACE INTO settings VALUES('rating_algorithm',?)", (json.dumps("community-v1"),))
        migrated = Store(self.path, lambda: self.now)
        self.assertEqual(migrated.rating_profile(2)["rating"], 2.75)
        self.assertTrue(migrated.rating_profile(1)["seed_pending"])
        self.assertEqual(migrated.user(1)["level"], "C-")
        migrated.set_level(999, 1, "C")
        self.assertFalse(migrated.rating_profile(1)["seed_pending"])
        self.assertEqual(migrated.rating_profile(1)["rating"], 2.75)
        self.assertEqual(Store(self.path, lambda: self.now).user(1)["rating_seed"], 2.75)

    def test_old_registration_level_buttons_rejected_instead_of_reinterpreted(self):
        uid = 101
        self.s.touch_user({"id":uid,"first_name":"Player"}, True)
        self.app.start_registration(uid)
        q = {"id":"q", "from":{"id":uid,"first_name":"Player"},
             "data":"level:2", "message":{"chat":{"id":uid,"type":"private"}}}
        with self.assertRaises(RuleError):
            self.app.callback(q)
        self.assertIsNone(self.s.user(uid)["level"])
        q["data"]="level_v2:C"
        self.app.callback(q)
        self.assertEqual(self.s.user(uid)["level"], "C")
        self.assertEqual(self.s.rating_profile(uid)["rating"], 2.75)
        with self.assertRaises(RuleError):
            self.app.admin_callback(999, ["setlevel","101","2"])

    def test_old_database_schema_migrates_once_with_level_seed(self):
        path = Path(self.tmp.name)/"old.sqlite3"
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,display TEXT NOT NULL,full_name TEXT DEFAULT '',level TEXT,private_started INTEGER DEFAULT 0,dm_error TEXT DEFAULT '')")
            c.execute("INSERT INTO users(id,display,level) VALUES(101,'Old player','B+')")
        migrated = Store(path, lambda: self.now)
        self.assertEqual(migrated.user(101)["rating_seed"], 5.25)
        self.assertEqual(migrated.rating_profile(101)["rating"], 5.25)
        self.assertEqual(Store(path, lambda: self.now).rating_profile(101)["rating"], 5.25)

    def test_previous_b_baseline_migrates_and_replays_results_once(self):
        self.s.set_level(999, 1, "B")
        self.s.set_level(999, 2, "B-")
        for uid in (1,2):
            self.s.join(self.eid, uid, "go", force=True, actor=999)
        self.s.save_results(999, self.eid, {1:1,2:2}, [1,2], {})
        with self.s.db() as c:
            c.execute("UPDATE users SET rating_seed=4.25 WHERE id IN (1,2)")
            c.execute("INSERT OR REPLACE INTO settings VALUES('rating_algorithm',?)", (json.dumps("community-v2-level-seeds"),))
        migrated = Store(self.path, lambda: self.now)
        history = migrated.event_rating_changes(self.eid)
        self.assertEqual(history[1]["before_rating"], 4.0)
        self.assertEqual(history[2]["before_rating"], 4.5)
        first = migrated.rating_profile(1)["rating"]
        self.assertGreater(first, 4.1)
        restarted = Store(self.path, lambda: self.now)
        self.assertEqual(restarted.rating_profile(1)["rating"], first)
        self.assertEqual(restarted.rating_profile(1)["rated_events"], 1)

    def test_letter_thresholds_gaps_and_display_rounding(self):
        cases = [(0,"D"),(1.99,"D"),(2.0,"D+"),(2.49,"D+"),(2.5,"C"),
                 (2.99,"C"),(3.0,"C+"),(3.5,"C+"),(3.99,"C+"),(4.0,"B"),
                 (4.49,"B"),(4.5,"B-"),(4.99,"B-"),(5.0,"B+"),(5.5,"B+"),
                 (5.99,"B+"),(6.0,"A"),(8.0,"A"),(2.499,"C")]
        for rating, expected in cases:
            self.assertEqual(level_for_rating(rating), expected)

    def test_auto_promotion_demotion_history_and_tournament_eligibility(self):
        event_ids = []
        for index in range(4):
            eid = self.s.save_event(999, dict(self.data, levels=[], start=self.data["start"]+index*86400,
                                            close_at=self.data["close_at"]+index*86400))
            self.s.publish(999, eid)
            for uid in (1,2):
                self.s.join(eid, uid, "go")
            self.s.save_results(999, eid, {1:1,2:2}, [1,2], {})
            event_ids.append(eid)
        self.assertEqual(self.s.user(1)["level"], "C+")
        self.assertEqual(self.s.user(1)["declared_level"], "C")
        self.assertEqual(self.s.rating_profile(1)["level"], "C+")
        self.assertIn("Уровень игры: C+", self.app.rating_profile_text(1))
        self.assertEqual(next(r for r in self.s.entries(event_ids[-1]) if r["user_id"]==1)["level"], "C+")
        next_event = self.s.save_event(999, dict(self.data, levels=["C+"]))
        self.s.publish(999, next_event)
        self.assertEqual(self.s.join(next_event, 1, "go"), "confirmed")
        self.s.save_results(999, event_ids[-1], {1:2,2:1}, [1,2], {1:1,2:2})
        self.assertEqual(self.s.user(1)["level"], "C")
        self.assertEqual(self.s.rating_profile(1)["level"], "C")
        self.assertEqual(Store(self.path, lambda: self.now).user(1)["level"], "C")
        restricted = self.s.save_event(999, dict(self.data, levels=["C+"]))
        self.s.publish(999, restricted)
        with self.assertRaises(RuleError):
            self.s.join(restricted, 1, "go")

    def test_rating_rule_visibility_and_enforced_admin_access(self):
        def buttons():
            return [b for row in self.api.calls[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
        self.app.menu(1)
        self.assertIn("Мой Рейтинг 🏆", [b["text"] for b in buttons()])
        self.assertNotIn("rating_rules", [b["callback_data"] for b in buttons()])
        self.app.menu(999)
        self.assertIn("rating_rules", [b["callback_data"] for b in buttons()])
        self.app.ranking(-123)
        self.assertNotIn("rating_rules", [b["callback_data"] for b in buttons()])
        self.app.ranking(1)
        self.assertNotIn("rating_rules", [b["callback_data"] for b in buttons()])
        self.app.ranking(999)
        self.assertIn("rating_rules", [b["callback_data"] for b in buttons()])
        for chat, kind in ((1,"private"),(-123,"supergroup")):
            with self.assertRaises(RuleError):
                self.app.callback({"id":"q", "from":{"id":1}, "data":"rating_rules",
                                   "message":{"chat":{"id":chat,"type":kind}}})
        with self.assertRaises(RuleError):
            self.app.rating_rules(1)
        self.app.callback({"id":"q", "from":{"id":999}, "data":"rating_rules",
                           "message":{"chat":{"id":-123,"type":"supergroup"}}})
        self.assertEqual(self.api.calls[-1][1]["chat_id"], 999)
        self.assertIn("Как считается", self.api.calls[-1][1]["text"])

    def group_query(self, action, uid=1, chat=-123):
        return {"id":"group-query", "from":{"id":uid,"first_name":"Player"}, "data":action,
                "message":{"message_id":10,"chat":{"id":chat,"type":"supergroup"}}}

    def test_quiet_full_waitlist_duplicate_signup_and_unsure(self):
        self.fill()
        self.app.callback(self.group_query(f"go:{self.eid}", 5))
        self.assertEqual(self.s.entry(self.eid, 5)["status"], "ready")
        self.assertIn("листе ожидания", self.api.calls[-1][1]["text"])
        self.app.callback(self.group_query(f"go:{self.eid}", 5))
        self.assertEqual(len(self.s.entries(self.eid)), 5)
        self.app.callback(self.group_query(f"queue:{self.eid}", 6))
        self.assertEqual(self.s.entry(self.eid, 6)["status"], "unsure")
        self.assertIn("Не уверен", self.api.calls[-1][1]["text"])
        self.assertTrue(all(m == "answerCallbackQuery" for m,p in self.api.calls))

    def test_group_confirmed_cancellation_requires_private_consent_keeps_payment(self):
        self.s.join(self.eid, 1, "go")
        self.s.paid(999, self.eid, 1, True)
        self.app.callback(self.group_query(f"no:{self.eid}"))
        self.assertEqual(self.s.entry(self.eid, 1)["status"], "confirmed")
        sends = [p for m,p in self.api.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["chat_id"], 1)
        self.assertIn("Освободить", sends[0]["text"])
        self.assertIn("Место пока сохраняется", self.api.calls[-1][1]["text"])
        self.app.callback({"id":"q", "from":{"id":1}, "data":f"confirm_choice:{self.eid}:no",
                           "message":{"message_id":1,"chat":{"id":1,"type":"private"}}})
        self.assertEqual(self.s.entry(self.eid, 1)["status"], "declined")
        self.assertEqual(self.s.entry(self.eid, 1)["paid"], 1)
        self.assertFalse(any(m == "sendMessage" and p["chat_id"]==-123 for m,p in self.api.calls))

    def test_unregistered_group_click_opens_registration_without_changing_entry(self):
        self.app.callback(self.group_query(f"go:{self.eid}", 101))
        self.assertIsNone(self.s.entry(self.eid, 101))
        self.assertIn(f"ego_{self.eid}", self.api.calls[-1][1]["url"])
        self.assertFalse(any(m == "sendMessage" for m,p in self.api.calls))
        self.app.callback(self.group_query("ranking:all:0", 101))
        self.assertIn("start=register", self.api.calls[-1][1]["url"])

    def test_group_roster_only_sent_privately_and_popup_rating_has_no_chat_output(self):
        self.s.join(self.eid, 1, "go")
        self.s.paid(999, self.eid, 1, True)
        self.app.callback(self.group_query(f"list:{self.eid}:0"))
        sends = [p for m,p in self.api.calls if m=="sendMessage"]
        self.assertEqual([p["chat_id"] for p in sends], [1])
        self.assertNotIn("Оплачено", sends[0]["text"])
        self.assertNotIn("paid", sends[0]["text"])
        self.api.calls.clear()
        self.app.callback(self.group_query("ranking:all:0"))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.calls[0][0], "answerCallbackQuery")
        self.assertTrue(self.api.calls[0][1]["show_alert"])
        self.assertIn("Мой Рейтинг", self.api.calls[0][1]["text"])

    def test_blocked_private_chat_redirect_preserves_confirmed_place(self):
        self.s.join(self.eid, 1, "go")
        original = self.api.send
        def blocked(*args, **kwargs):
            raise APIError(403, "blocked")
        self.api.send = blocked
        try:
            self.app.callback(self.group_query(f"no:{self.eid}"))
        finally:
            self.api.send = original
        self.assertEqual(self.s.entry(self.eid, 1)["status"], "confirmed")
        self.assertIn(f"eno_{self.eid}", self.api.calls[-1][1]["url"])

    def test_group_offer_needs_consent_and_outside_group_cannot_mutate(self):
        self.fill()
        self.s.join(self.eid, 5, "go")
        self.s.join(self.eid, 1, "no")
        self.assertEqual(self.s.entry(self.eid, 5)["status"], "offered")
        self.app.callback(self.group_query(f"go:{self.eid}", 5))
        self.assertEqual(self.s.entry(self.eid, 5)["status"], "offered")
        self.assertTrue(any(m == "sendMessage" and p["chat_id"]==5 for m,p in self.api.calls))
        with self.assertRaises(RuleError):
            self.app.callback(self.group_query(f"go:{self.eid}", 6, -999))
        self.assertIsNone(self.s.entry(self.eid, 6))
        self.app.handle({"callback_query":self.group_query(f"paid:{self.eid}:5:1", 5)})
        self.assertEqual(self.s.entry(self.eid, 5)["paid"], 0)
        self.assertFalse(any(m=="sendMessage" and p["chat_id"]<0 for m,p in self.api.calls))

    def test_atomic_confirmed_place_guard_and_closed_event_popup(self):
        self.s.join(self.eid, 1, "go")
        with self.assertRaises(RuleError):
            self.s.join(self.eid, 1, "no", protect_confirmed=True)
        self.assertEqual(self.s.entry(self.eid, 1)["status"], "confirmed")
        self.s.change_status(999, self.eid, "cancelled")
        self.app.handle({"callback_query":self.group_query(f"go:{self.eid}", 2)})
        self.assertIsNone(self.s.entry(self.eid, 2))
        self.assertEqual(self.api.calls[-1][0], "answerCallbackQuery")
        self.assertIn("недоступна", self.api.calls[-1][1]["text"])
        self.assertTrue(self.api.calls[-1][1]["show_alert"])


if __name__ == "__main__":
    unittest.main()








