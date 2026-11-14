import concurrent.futures
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import Store, RuleError, LEVEL_START_RATINGS, level_for_rating
from bot import App, configure_commands, APIError
from test_bot import FakeAPI
from scheduling import TZ, daytime, reminder_time


def at(value):
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ).timestamp()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'test.db'
        self.now = at('2030-01-01 12:00:00')
        self.s = Store(self.path, lambda:self.now)
        self.api = FakeAPI()
        self.app = App(self.s,self.api,999,group=-123)
        for uid in range(1,12):
            self.s.touch_user({'id':uid,'first_name':str(uid)},True)
            self.s.register(uid,'','C')
        self.data = dict(court='Club',format='Мексикано',photo='photo',location='https://maps.example/club',
                         start=self.now+3*86400,close_at=self.now+3*86400,duration=90,capacity=4,levels=[],price=15000,currency='AED')
        self.eid = self.s.save_event(999,self.data)
        self.s.publish(999,self.eid)

    def tearDown(self):
        self.tmp.cleanup()

    def vacancy(self, waiters=(5,6,7,8)):
        for uid in (1,2,3,4): self.s.join(self.eid,uid,'go')
        for uid in waiters:
            self.s.join(self.eid,uid,'go')
        self.s.join(self.eid,1,'no')

    def sent(self):
        return [p for m,p in self.api.calls if m=='sendMessage']

    def test_chain_0_15_16_17_minutes_and_restart(self):
        self.vacancy()
        self.app.deliver_vacancies()
        self.assertEqual([p['chat_id'] for p in self.sent()],[5])
        self.s = Store(self.path,lambda:self.now)
        self.app.s = self.s
        for dt,expected in [(899,[5]),(1,[5,6]),(59,[5,6]),(1,[5,6,7]),(60,[5,6,7,8])]:
            self.now += dt
            self.s.tick(); self.app.deliver_vacancies()
            self.assertEqual([p['chat_id'] for p in self.sent()],expected)
        self.assertEqual(self.s.entry(self.eid,5)['status'],'ready')

    def test_decline_skips_to_next_immediately(self):
        self.vacancy(); self.app.deliver_vacancies()
        self.s.join(self.eid,5,'queue');self.app.deliver_vacancies()
        self.assertEqual([p['chat_id'] for p in self.sent()],[5,6])
        self.s.join(self.eid,6,'no');self.app.deliver_vacancies()
        self.assertEqual([p['chat_id'] for p in self.sent()],[5,6,7])

    def test_multiple_vacancies_edit_one_message_and_new_cycle(self):
        self.vacancy();self.app.deliver_vacancies()
        self.s.join(self.eid,2,'no');self.app.deliver_vacancies()
        self.assertEqual(len(self.sent()),1)
        edits = [p for m,p in self.api.calls if m=='editMessageText']
        self.assertIn('мест: 2',edits[-1]['text'])
        self.s.join(self.eid,9,'go');self.s.join(self.eid,10,'go')
        self.app.deliver_vacancies()
        self.assertIn('Мест больше нет',[p for m,p in self.api.calls if m=='editMessageText'][-1]['text'])
        self.now += 1000;self.s.tick();self.app.deliver_vacancies()
        self.assertEqual(len(self.sent()),1)
        self.s.join(self.eid,10,'no');self.app.deliver_vacancies()
        self.assertEqual(len(self.sent()),2)
        self.assertEqual(self.sent()[-1]['chat_id'],5)

    def test_two_clicks_for_last_place_only_one_wins(self):
        self.vacancy()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            result=list(pool.map(lambda uid:self.s.join(self.eid,uid,'go'),[5,6]))
        self.assertEqual(sorted(result),['confirmed','ready'])
        self.assertEqual(sum(e['status']=='confirmed' for e in self.s.entries(self.eid)),4)

    def test_night_group_activity_5_minutes_and_bot_30_minutes(self):
        self.now = at('2030-01-01 23:00:00')
        self.s.activity(5,'group',self.now-301)
        self.s.activity(6,'group',self.now-300)
        self.s.activity(7,'bot',self.now-1800)
        self.vacancy();self.app.deliver_vacancies()
        self.assertEqual([p['chat_id'] for p in self.sent()],[6])
        self.s.join(self.eid,6,'no');self.app.deliver_vacancies()
        self.assertEqual([p['chat_id'] for p in self.sent()],[6,7])
        self.s.join(self.eid,7,'no');self.app.deliver_vacancies()
        self.assertEqual(len(self.sent()),2)
        self.s.activity(5,'bot');self.s.tick();self.app.deliver_vacancies()
        self.assertEqual(self.sent()[-1]['chat_id'],5)

    def test_expired_pending_activity_rechecked_and_can_reactivate(self):
        self.now=at('2030-01-01 23:00:00')
        self.s.activity(5,'group',self.now-299)
        self.vacancy();self.now+=2
        self.app.deliver_vacancies()
        self.assertEqual(self.sent(),[])
        self.s.activity(5,'bot');self.s.tick();self.app.deliver_vacancies()
        self.assertEqual(self.sent()[0]['chat_id'],5)

    def test_no_spam_when_clock_ticks_or_bot_messages(self):
        self.vacancy();self.app.deliver_vacancies()
        for _ in range(10):self.s.tick();self.app.deliver_vacancies()
        self.assertEqual(len(self.sent()),1)
        with self.s.db() as c:
            self.assertIsNone(c.execute('SELECT * FROM activity WHERE user_id=5').fetchone())

    def test_cancellation_and_deadline_stop_pending(self):
        self.vacancy();self.s.change_status(999,self.eid,'cancelled')
        self.app.deliver_vacancies()
        self.assertEqual(self.sent(),[])

    def test_blocked_first_waiter_does_not_hold_chain(self):
        self.vacancy()
        original=self.api.send
        def send(chat,*a,**kw):
            if chat==5:raise APIError(403,'blocked')
            return original(chat,*a,**kw)
        self.api.send=send
        self.app.deliver_vacancies();self.s.tick();self.app.deliver_vacancies()
        self.assertEqual([p['chat_id'] for p in self.sent()],[6])

    def test_quiet_time_mapping_boundaries_and_year(self):
        for clock,want in [('08:00:00','09:00:00'),('08:30:00','09:00:00'),('22:30:00','22:00:00'),
                           ('23:30:00','22:00:00'),('09:00:00','09:00:00'),('22:00:00','22:00:00')]:
            self.assertEqual(reminder_time(at('2030-12-31 '+clock)),at('2030-12-31 '+want))
        self.assertTrue(daytime(at('2030-01-01 22:00:30')))
        self.assertFalse(daytime(at('2030-01-01 22:01:00')))

    def test_colliding_morning_reminders_merge_and_survive_restart(self):
        self.now=at('2030-01-01 12:00:00')
        data=dict(self.data,start=at('2030-01-02 10:00:00'),close_at=at('2030-01-02 10:00:00'))
        eid=self.s.save_event(999,data);self.s.publish(999,eid);self.s.join(eid,1,'go')
        self.s.tick()
        with self.s.db() as c:
            rows=c.execute("SELECT * FROM outbox WHERE event_id=? AND kind='reminder'",(eid,)).fetchall()
        self.assertEqual(sum(r['due']==at('2030-01-02 09:00:00') for r in rows),1)
        self.now=at('2030-01-02 09:00:00')
        self.s=Store(self.path,lambda:self.now);self.app.s=self.s
        self.s.tick();self.app.deliver()
        self.assertEqual(len([p for p in self.sent() if 'Напоминание' in p['text']]),1)
        self.s.tick();self.app.deliver()
        self.assertEqual(len([p for p in self.sent() if 'Напоминание' in p['text']]),1)
        self.assertIn('10:00',self.sent()[-1]['text'])

    def test_early_game_and_retroactive_evening_reminders_are_skipped(self):
        self.now=at('2030-01-01 22:15:00')
        eid=self.s.save_event(999,dict(self.data,start=at('2030-01-02 00:30:00'),close_at=at('2030-01-02 00:30:00')))
        self.s.publish(999,eid);self.s.join(eid,1,'go');self.s.tick()
        with self.s.db() as c:
            rows=c.execute("SELECT * FROM outbox WHERE event_id=? AND kind='reminder'",(eid,)).fetchall()
        self.assertEqual(rows,[])

    def test_night_does_not_send_even_recent_player_game_reminder(self):
        self.s.join(self.eid,1,'go');self.s.tick()
        self.now=at('2030-01-01 23:00:00');self.s.activity(1,'bot')
        n=dict(kind='reminder',expires=self.now+100,event_id=self.eid,revision=1,dedupe='anything')
        self.assertFalse(self.s.notification_valid(n))

    def test_court_edit_archive_snapshot_and_photo_reuse(self):
        cid=self.s.save_court(999,self.data)
        snapshot=self.s.court_data(cid)
        eid=self.s.save_event(999,snapshot)
        self.s.save_court(999,dict(self.data,court='Changed',photo='other'),cid)
        self.assertEqual(self.s.event(eid)['court'],'Club')
        self.assertEqual(self.s.event(eid)['photo'],'photo')
        self.assertEqual(self.s.court_data(cid)['court'],'Changed')
        self.s.archive_court(999,cid)
        self.assertEqual(self.s.courts(),[])
        with self.assertRaises(RuleError):self.s.court_data(cid)
        self.s.archive_court(999,cid,True)
        self.assertEqual(len(self.s.courts()),1)

    def wizard(self,index=3,data=None):
        self.app.wizard_prompt(999,dict(kind='wizard',index=index,data=data or {}))
        return self.s.session(999)

    def click(self,action,*args):
        state=self.s.session(999)
        self.app.wizard_callback(999,['wiz',state['token'],action,*args])

    def input(self,text):
        self.app.handle({'message':{'from':{'id':999,'first_name':'Admin'},'chat':{'id':999,'type':'private'},'text':text}})

    def test_date_buttons_14_days_rollover_and_last_manual_button(self):
        self.now=at('2030-12-25 12:00:00');self.wizard()
        buttons=self.api.calls[-1][1]['reply_markup']['inline_keyboard']
        dates=[b for r in buttons for b in r if ':date:' in b['callback_data']]
        self.assertEqual(len(dates),14)
        self.assertTrue(dates[-1]['callback_data'].endswith('2031-01-07'))
        self.assertEqual(buttons[-1][0]['text'],'Другая дата')
        self.click('date','2031-01-02')
        # Persist partial date through session cancellation / draft resume.
        state=self.s.session(999);eid=state['eid']
        self.app.pause_wizard(999)
        self.app.admin_callback(999,['edit',str(eid)])
        self.input('19:30')
        self.assertEqual(self.s.event(eid)['start'],at('2031-01-02 19:30:00'))
        self.assertEqual(self.s.session(999)['index'],4)

    def test_manual_date_and_past_time_rejected(self):
        self.wizard();self.click('manual_date');self.input('31.12.2031');self.input('18:00')
        self.assertEqual(self.s.event(self.s.session(999)['eid'])['start'],at('2031-12-31 18:00:00'))
        self.wizard();self.click('date','2030-01-01')
        self.input('10:00')
        self.assertEqual(self.s.session(999)['index'],3)
        self.assertIn('будущем',self.sent()[-1]['text'])

    def test_stale_date_callback_rejected(self):
        self.wizard();self.now+=14*86400
        with self.assertRaises(RuleError):self.click('date','2030-01-01')

    def test_format_presets_and_custom_text(self):
        for index,name in enumerate(['Мексикано','Американо','Social']):
            self.wizard(2);self.click('value',str(index))
            self.assertEqual(self.s.event(self.s.session(999)['eid'])['format'],name)
        self.wizard(2);self.click('custom');self.input('Игра с тренером')
        self.assertEqual(self.s.event(self.s.session(999)['eid'])['format'],'Игра с тренером')

    def test_command_scopes_and_no_group_messages(self):
        configure_commands(self.api)
        scopes={p['scope']['type']:p['commands'] for m,p in self.api.calls if m=='setMyCommands'}
        self.assertEqual(scopes['all_group_chats'],[])
        self.assertTrue(scopes['all_private_chats'])
        self.api.calls.clear()
        for text in ['/my@padel_escape_bot','/id','/admin','/rating']:
            self.app.handle({'message':{'from':{'id':1},'chat':{'id':-123,'type':'supergroup'},'text':text}})
        self.assertFalse(any(p['chat_id']<0 for p in self.sent()))

    def test_only_connected_group_activity_and_no_bot_activity(self):
        for chat,uid in [(-123,1),(-999,2)]:
            self.app.handle({'message':{'from':{'id':uid},'chat':{'id':chat,'type':'supergroup'},'text':'hello','date':self.now}})
        with self.s.db() as c:
            self.assertEqual(c.execute('SELECT group_at FROM activity WHERE user_id=1').fetchone()[0],self.now)
            self.assertIsNone(c.execute('SELECT group_at FROM activity WHERE user_id=2').fetchone())

    def test_old_offers_migrate_without_reserving_and_b_order_replays_once(self):
        self.s.set_level(999,1,'B');self.s.set_level(999,2,'B-')
        with self.s.db() as c:
            c.execute("UPDATE users SET rating_seed=4.0 WHERE id=1")
            c.execute("UPDATE users SET rating_seed=4.5 WHERE id=2")
            c.execute("UPDATE users SET level='A' WHERE id=1")
            c.execute("INSERT OR REPLACE INTO settings VALUES('rating_algorithm',?)",(json.dumps('community-v3-b-seeds'),))
            c.execute("DELETE FROM settings WHERE key='queue_algorithm'")
            c.execute("INSERT INTO entries(event_id,user_id,status,joined_at,ready_at,offer_until) VALUES(?,5,'offered',?,?,?)",(self.eid,self.now,self.now,self.now+1800))
        restored=Store(self.path,lambda:self.now)
        self.assertEqual(restored.entry(self.eid,5)['status'],'ready')
        self.assertEqual(restored.user(1)['rating_seed'],4.5)
        self.assertEqual(restored.user(2)['rating_seed'],4.0)
        self.assertEqual(Store(self.path,lambda:self.now).user(1)['rating_seed'],4.5)
        self.assertEqual(level_for_rating(4.0),'B-')
        self.assertEqual(level_for_rating(4.5),'B')


    def test_court_editor_creation_edits_and_permissions(self):
        self.app.admin_callback(999,['court_add'])
        state=self.s.session(999)
        for field,value in [('court','New club'),('location','https://maps.example/new')]:
            self.app.admin_callback(999,['court_field',field,state['token']])
            self.input(value)
        self.app.admin_callback(999,['court_field','save',state['token']])
        self.assertEqual(self.s.courts()[0]['name'],'New club')
        cid=self.s.courts()[0]['id']
        self.app.admin_callback(999,['court_change',str(cid)])
        state=self.s.session(999)
        self.app.admin_callback(999,['court_field','court',state['token']]);self.input('Updated')
        self.app.admin_callback(999,['court_field','save',state['token']])
        self.assertEqual(self.s.courts()[0]['name'],'Updated')
        with self.assertRaises(RuleError):self.app.admin_callback(1,['court_archive',str(cid)])
        self.app.admin_callback(999,['court_archive',str(cid)])
        self.assertEqual(self.s.courts(),[])

    def test_stalled_worker_health_and_head(self):
        import os
        import urllib.request
        import urllib.error
        from unittest.mock import patch
        from bot import start_health_server
        live=[True]
        with patch.dict(os.environ,{'PORT':'0'}):server=start_health_server(lambda:live[0])
        try:
            url='http://127.0.0.1:'+str(server.server_port)
            with urllib.request.urlopen(url+'/health') as r:self.assertEqual(r.read(),b'ok\n')
            live[0]=False
            with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url+'/health')
            self.assertEqual(e.exception.code,503)
            with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(url+'/health',method='HEAD'))
            self.assertEqual(e.exception.code,503)
            with urllib.request.urlopen(url+'/') as r:self.assertEqual(r.status,200)
        finally:server.shutdown();server.server_close()

    def test_evening_adjusted_reminder_is_valid_during_dispatch_minute(self):
        self.now=at('2030-01-01 20:00:00')
        start=at('2030-01-02 00:30:00')
        eid=self.s.save_event(999,dict(self.data,start=start,close_at=start))
        self.s.publish(999,eid);self.s.join(eid,1,'go');self.s.tick()
        self.now=at('2030-01-01 22:00:05')
        notes=[n for n in self.s.due_notifications() if n['event_id']==eid]
        self.assertEqual(len(notes),1)
        self.assertTrue(self.s.notification_valid(notes[0]))

    def test_reminder_retry_after_quiet_boundary_never_sends_at_night(self):
        self.now=at('2030-01-01 20:00:00')
        start=at('2030-01-02 00:30:00')
        eid=self.s.save_event(999,dict(self.data,start=start,close_at=start))
        self.s.publish(999,eid);self.s.join(eid,1,'go');self.s.tick()
        self.now=at('2030-01-01 22:00:59')
        note=[n for n in self.s.due_notifications() if n['event_id']==eid][0]
        self.s.notification_result(note,'pending','Telegram 429',retry_after=10)
        self.now+=10
        self.app.deliver()
        self.assertFalse(any('Напоминание' in p['text'] for p in self.sent()))


if __name__=='__main__':unittest.main()

