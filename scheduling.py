"""Durable vacancy notifications and Dubai quiet-hour scheduling."""
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Dubai')


def daytime(now):
    dt = datetime.fromtimestamp(now, TZ)
    # User-facing times have minute precision; allow the 22:00 dispatch minute.
    return 9*60 <= dt.hour*60+dt.minute <= 22*60


def reminder_time(timestamp):
    dt = datetime.fromtimestamp(timestamp, TZ)
    if dt.hour < 9:
        return dt.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    if (dt.hour, dt.minute, dt.second) > (22, 0, 0):
        return dt.replace(hour=22, minute=0, second=0, microsecond=0).timestamp()
    return timestamp


class NotificationRules:
    def activity(self, uid, source, stamp=None):
        if source not in ('bot', 'group'):
            raise ValueError('Invalid activity source')
        now = self.clock()
        stamp = min(now, now if stamp is None else stamp)
        with self.db() as c:
            c.execute('INSERT INTO activity(user_id,bot_at,group_at) VALUES(?,?,?) '
                      f'ON CONFLICT(user_id) DO UPDATE SET {source}_at=MAX(COALESCE(activity.{source}_at,0),excluded.{source}_at)',
                      (uid, stamp if source == 'bot' else None, stamp if source == 'group' else None))

    def _eligible(self, c, uid):
        now = self.clock()
        if daytime(now):
            return True
        r = c.execute('SELECT bot_at,group_at FROM activity WHERE user_id=?', (uid,)).fetchone()
        return bool(r and (r['bot_at'] is not None and 0 <= now-r['bot_at'] <= 1800
                           or r['group_at'] is not None and 0 <= now-r['group_at'] <= 300))

    def _free(self, c, e):
        if e['status'] != 'published' or self.clock() >= min(e['start'], e['close_at']):
            return 0
        occupied = c.execute("SELECT count(*) FROM entries WHERE event_id=? AND status='confirmed'", (e['id'],)).fetchone()[0]
        return max(0, e['capacity'] - occupied)

    def _fill(self, c, eid):
        """Notify, never reserve a place or promote an entry without consent."""
        e = self._event(c, eid)
        free = self._free(c, e)
        chain = c.execute('SELECT * FROM vacancy_chains WHERE event_id=?', (eid,)).fetchone()
        if not free:
            if chain and chain['active']:
                c.execute('UPDATE vacancy_chains SET active=0 WHERE event_id=?', (eid,))
                c.execute("UPDATE vacancy_notices SET state='cancelled' WHERE event_id=? AND state='pending'", (eid,))
            return
        if not chain or not chain['active']:
            generation = chain['generation'] + 1 if chain else 1
            c.execute('INSERT INTO vacancy_chains(event_id,generation,active,next_due,sent_count,last_uid) VALUES(?,?,1,?,0,NULL) '
                      'ON CONFLICT(event_id) DO UPDATE SET generation=excluded.generation,active=1,next_due=excluded.next_due,sent_count=0,last_uid=NULL',
                      (eid, generation, self.clock()))
            chain = c.execute('SELECT * FROM vacancy_chains WHERE event_id=?', (eid,)).fetchone()
        if self.clock() < chain['next_due']:
            return
        if c.execute("SELECT 1 FROM vacancy_notices WHERE event_id=? AND generation=? AND state='pending'", (eid, chain['generation'])).fetchone():
            return
        candidates = c.execute("SELECT en.user_id FROM entries en JOIN users u ON u.id=en.user_id "
                               "WHERE en.event_id=? AND en.status='ready' AND u.private_started=1 "
                               "AND NOT EXISTS(SELECT 1 FROM vacancy_notices n WHERE n.event_id=en.event_id AND n.user_id=en.user_id AND n.generation=?) "
                               'ORDER BY en.ready_at,en.user_id', (eid, chain['generation'])).fetchall()
        for row in candidates:
            user = c.execute('SELECT level FROM users WHERE id=?', (row[0],)).fetchone()
            if e.get('levels') and self._effective_level(c, row[0], user[0]) not in e['levels']:
                continue
            if self._eligible(c, row[0]):
                c.execute("INSERT INTO vacancy_notices(event_id,generation,user_id,state,retry_at) VALUES(?,?,?,'pending',?)",
                          (eid, chain['generation'], row[0], self.clock()))
                break

    def _queue_departure(self, c, eid, uid):
        chain = c.execute('SELECT * FROM vacancy_chains WHERE event_id=?', (eid,)).fetchone()
        if chain and chain['active']:
            pending = c.execute("SELECT 1 FROM vacancy_notices WHERE event_id=? AND generation=? AND user_id=? AND state='pending'",
                                (eid, chain['generation'], uid)).fetchone()
            c.execute("UPDATE vacancy_notices SET state='cancelled' WHERE event_id=? AND user_id=? AND state='pending'", (eid, uid))
            if pending or chain['last_uid'] == uid:
                c.execute('UPDATE vacancy_chains SET next_due=? WHERE event_id=?', (self.clock(), eid))

    def vacancy_tasks(self):
        """Revalidate at send time; edits reuse the original private message."""
        tasks = []
        with self.db() as c:
            rows = c.execute("SELECT * FROM vacancy_notices WHERE state='pending' OR (message_id IS NOT NULL AND state='sent') ORDER BY id").fetchall()
            for row in rows:
                n = dict(row)
                if n['retry_at'] > self.clock():
                    continue
                e = self._event(c, n['event_id'])
                chain = c.execute('SELECT * FROM vacancy_chains WHERE event_id=?', (n['event_id'],)).fetchone()
                en = c.execute('SELECT status FROM entries WHERE event_id=? AND user_id=?', (n['event_id'], n['user_id'])).fetchone()
                free = self._free(c, e)
                active = bool(chain and chain['active'] and chain['generation'] == n['generation'] and free and en and en[0] == 'ready')
                if n['state'] == 'pending' and (not active or not self._eligible(c, n['user_id'])):
                    c.execute("DELETE FROM vacancy_notices WHERE id=? AND message_id IS NULL", (n['id'],))
                    c.execute('UPDATE vacancy_chains SET next_due=? WHERE event_id=?', (self.clock(), n['event_id']))
                    continue
                if active:
                    when = datetime.fromtimestamp(e['start'], TZ).strftime('%d.%m.%Y %H:%M')
                    text = (f"Освободилось мест: {free}. Турнир №{e['id']} «{e['court']}» — {when} (Дубай).\n"
                            'Нажмите «Иду», чтобы записаться. Место получит тот, кто подтвердит первым.')
                    keyboard = {'inline_keyboard': [[{'text': 'Иду', 'callback_data': f"go:{e['id']}"},
                                                    {'text': 'Не иду', 'callback_data': f"no:{e['id']}"},
                                                    {'text': 'Не уверен', 'callback_data': f"queue:{e['id']}"}]]}
                else:
                    text = (f"Турнир №{e['id']} «{e['court']}»: " + ('Вы записаны.' if en and en[0] == 'confirmed'
                            else 'Вы больше не в очереди.' if en and en[0] != 'ready' else 'Мест больше нет или запись закрыта.'))
                    keyboard = {'inline_keyboard': []}
                import json
                signature = json.dumps([text, keyboard], ensure_ascii=False, sort_keys=True)
                if n['rendered'] != signature:
                    tasks.append({**n, 'text': text, 'keyboard': keyboard, 'signature': signature, 'active': active})
                if len(tasks) >= 20:
                    break
        return tasks

    def vacancy_result(self, n, message_id=None, error=None, permanent=False, retry_after=None):
        with self.db() as c:
            if error:
                c.execute('UPDATE vacancy_notices SET attempts=attempts+1,retry_at=? WHERE id=?',
                          (self.clock()+(retry_after or min(300, 2**min(n['attempts']+1, 8))), n['id']))
                if permanent:
                    c.execute("UPDATE vacancy_notices SET state=?,rendered=? WHERE id=?", ('uneditable' if n['message_id'] else 'failed', n['signature'], n['id']))
                    c.execute('UPDATE vacancy_chains SET next_due=? WHERE event_id=?', (self.clock(), n['event_id']))
                c.execute('UPDATE users SET dm_error=? WHERE id=?', (error, n['user_id']))
                return
            c.execute("UPDATE vacancy_notices SET message_id=?,rendered=?,state=?,attempts=0,retry_at=0 WHERE id=?",
                      (message_id or n['message_id'], n['signature'], 'sent' if n['active'] else 'closed', n['id']))
            if n['state'] == 'pending':
                chain = c.execute('SELECT * FROM vacancy_chains WHERE event_id=?', (n['event_id'],)).fetchone()
                delay = 900 if chain['sent_count'] == 0 else 60
                c.execute('UPDATE vacancy_chains SET next_due=?,sent_count=sent_count+1,last_uid=? WHERE event_id=? AND generation=?',
                          (self.clock()+delay, n['user_id'], n['event_id'], n['generation']))

    def _enqueue_reminders(self, c, e, en):
        revised = c.execute('SELECT value FROM settings WHERE key=?', (f"reminder_revision_at:{e['id']}",)).fetchone()
        import json
        threshold = max(en['joined_at'], json.loads(revised[0]) if revised else 0)
        for hours in (24, 12, 2, 1):
            raw = e['start'] - hours*3600
            due = reminder_time(raw)
            if raw <= en['joined_at'] or due <= threshold or due >= e['start']:
                continue
            when = datetime.fromtimestamp(e['start'], TZ).strftime('%d.%m.%Y %H:%M')
            text = f"Напоминание: турнир №{e['id']} «{e['court']}» — {when} (Дубай)."
            if e.get('location'):
                text += '\nЛокация: ' + e['location']
            self.enqueue(c, f"reminder:{e['id']}:{e['revision']}:{en['user_id']}:{int(due)}:{en['joined_at']}",
                         en['user_id'], text, 'reminder', e['id'], e['revision'], due=due, expires=min(due+900, e['start']))
