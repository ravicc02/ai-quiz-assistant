import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.config import Settings
from app.db import Database, utc_now
from app.services.attendance.availability import timestamp, window
from app.services.attendance.class_scan import (
    BACKOFF_SECONDS,
    activity_cache_seconds,
    anchor_sort_key,
    choose_anchor,
    class_scan_summary,
    note_scan_failure,
    note_scan_success,
    register_membership,
    reset_class_scans,
    should_scan,
    switch_anchor_for_account,
)
from app.services.attendance.client import AttendanceClient
from app.services.attendance.codebook import find_sign_code
from app.services.attendance.store import AttendanceStore
from app.services.attendance.contracts import AttendanceError

NOW = datetime(2026,11,13,12,tzinfo=timezone(timedelta(hours=8))).timestamp()


class AttendanceWindowTests(unittest.TestCase):
    def test_seconds_milliseconds_date_objects_and_china_strings_share_one_clock(self):
        for value in (NOW, NOW*1000, str(int(NOW*1000)), {'time':NOW*1000},'2026-11-13 12:00:00','2026-11-13T04:00:00Z'):
            self.assertEqual(timestamp(value),NOW)

    def test_start_is_inclusive_and_end_is_exclusive(self):
        self.assertEqual(window({'startTime':NOW,'endTime':NOW+1},now=NOW)['status'],'open')
        self.assertEqual(window({'startTime':NOW-1,'endTime':NOW},now=NOW)['status'],'ended')
        self.assertEqual(window({'startTime':NOW+1,'endTime':NOW+2},now=NOW)['status'],'not_started')

    def test_ended_detail_overrides_list_status_and_missing_deadline(self):
        old={'status':1,'startTime':1758868045000,'endTime':''}
        self.assertEqual(window(old,{'status':1,'isOver':1,'timeLong':-1},now=NOW)['status'],'ended')
        self.assertEqual(window({'status':1,'startTime':NOW-20,'endTime':NOW+60},{'isOver':True},now=NOW)['status'],'ended')

    def test_missing_or_invalid_times_are_not_guessed_as_current(self):
        for value in ({},{'startTime':NOW-1},{'startTime':NOW-1,'endTime':'bad'}, {'startTime':NOW,'endTime':NOW}, {'startTime':False,'endTime':NOW+10}):
            self.assertEqual(window(value,now=NOW)['status'],'unknown')

    def test_indefinite_requires_explicit_nonexpired_platform_proof(self):
        start={'startTime':NOW-60,'endTime':None,'timeLong':-1}
        self.assertEqual(window(start,now=NOW)['status'],'unknown')
        self.assertEqual(window(start,{'setEndTimeNull':1,'isOver':0},now=NOW)['status'],'open')
        self.assertEqual(window(start,{'setEndTimeNull':1,'isOver':1},now=NOW)['status'],'ended')

    def test_conflicting_windows_use_the_conservative_intersection(self):
        self.assertEqual(window({'startTime':NOW-50,'endTime':NOW+100},{'starttime':NOW-30,'endtime':NOW-1},now=NOW)['status'],'ended')
        self.assertEqual(window({'startTime':NOW-50,'endTime':NOW+100},{'starttime':NOW+10,'endtime':NOW+90},now=NOW)['status'],'not_started')


class AttendanceAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_class_scan_state_uses_stable_anchor_idle_growth_and_capped_backoff(self):
        self.addCleanup(reset_class_scans)
        reset_class_scans()
        course={'course_id':'course-one','class_id':'class-one','course_name':'Course'}
        persisted=[]
        first=register_membership('account-a',course,persist=persisted.append)
        same=register_membership('account-b',course,persist=persisted.append)
        self.assertIs(first,same)
        self.assertEqual(first.members,{'account-a','account-b'})
        self.assertEqual(first.anchor,choose_anchor({'account-a','account-b'}))
        self.assertEqual(persisted[-1]['backoff_until'],0)
        self.assertEqual(persisted[-1]['next_due_at'],0)
        ordered=sorted(['account-a','account-b','account-c'],key=anchor_sort_key)
        self.assertEqual(ordered[0],choose_anchor(set(ordered)))

        note_scan_success(first,'account-a',[],30,now=1000)
        self.assertEqual(first.idle_streak,1)
        self.assertEqual(first.next_due_at,1060)
        active=datetime(2026,11,13,12,tzinfo=timezone(timedelta(hours=8)))
        self.assertFalse(should_scan(first,30,now=1030,wall_now=active))
        self.assertTrue(should_scan(first,30,now=1060,wall_now=active))

        note_scan_success(first,'account-a',[{'upstream_id':'1'}],30,now=1100)
        self.assertEqual(first.idle_streak,0)
        self.assertEqual(first.next_due_at,1130)

        delays=[]
        for index in range(7):
            note_scan_failure(first,30,'平台请求频繁，请稍后重试',now=2000+index*4000)
            delays.append(first.backoff_seconds)
        self.assertEqual(delays,list(BACKOFF_SECONDS)+[BACKOFF_SECONDS[-1]])
        self.assertFalse(should_scan(first,30,now=first.backoff_until-1))
        summary=class_scan_summary(['account-b'],now=first.backoff_until-1)
        self.assertEqual(summary['classes'],1)
        self.assertEqual(summary['cooling_classes'],1)

    async def test_class_scan_state_restores_backoff_and_cold_schedule_from_storage(self):
        self.addCleanup(reset_class_scans)
        reset_class_scans()
        wall_now=time.time()
        stored={
            'course_name':'Persisted course','class_name':'C806',
            'members':['persisted-member'],'anchor':'persisted-member',
            'idle_streak':3,'failure_streak':2,'backoff_seconds':240,
            'backoff_until':wall_now+120,'next_due_at':wall_now+300,
            'last_scan_at':wall_now-60,'last_error':'平台请求频繁',
            'scan_count':4,'anchor_scans':2,'fallback_scans':1,'activity_count':0,
        }
        persisted=[]
        state=register_membership(
            'new-member',
            {'course_id':'persisted-course','class_id':'persisted-class','course_name':'Persisted course'},
            load=lambda _: stored,
            persist=persisted.append,
        )
        self.assertEqual(state.members,{'persisted-member','new-member'})
        self.assertEqual(state.idle_streak,3)
        self.assertEqual(state.failure_streak,2)
        self.assertEqual(state.activities,[])
        self.assertGreater(state.backoff_until,time.monotonic()+100)
        self.assertFalse(should_scan(state,60))
        self.assertTrue(persisted)
        self.assertEqual(persisted[-1]['members'],sorted(state.members))
        self.assertGreater(persisted[-1]['backoff_until'],wall_now+90)

    async def test_authentication_failures_switch_the_class_anchor(self):
        self.addCleanup(reset_class_scans)
        reset_class_scans()
        course = {'course_id':'anchor-course','class_id':'anchor-class','course_name':'计算机科学'}
        state = register_membership('member-a', course)
        register_membership('member-b', course)
        active = state.anchor
        standby = next(member for member in state.members if member != active)

        note_scan_failure(state, 30, '学习通登录态已失效，请重新登录', account_id=active, now=1000)
        self.assertEqual(state.anchor, active)
        self.assertEqual(state.failure_streak, 1)

        note_scan_failure(state, 30, '学习通登录态已失效，请重新登录', account_id=active, now=1030)
        self.assertEqual(state.anchor, standby)
        self.assertEqual(state.failure_streak, 0)
        self.assertEqual(state.next_due_at, 0)
        self.assertEqual(state.anchor_switches, 1)
        self.assertIn('切换备用账号', state.last_error)

    async def test_service_level_auth_failure_switches_all_registered_anchors(self):
        self.addCleanup(reset_class_scans)
        reset_class_scans()
        states = [
            register_membership('anchor-a', {'course_id': 'course-a', 'class_id': 'class-a'}),
            register_membership('anchor-a', {'course_id': 'course-b', 'class_id': 'class-b'}),
        ]
        for state in states:
            register_membership('standby-b', {
                'course_id': state.course_id,
                'class_id': state.class_id,
            })
        active = states[0].anchor
        standby = next(member for member in states[0].members if member != active)
        switched = switch_anchor_for_account(active, '登录态已失效')
        self.assertEqual(switched, 2)
        self.assertTrue(all(state.anchor == standby for state in states))
        self.assertTrue(all(state.anchor_switches == 1 for state in states))

    async def test_prewarm_and_burst_modes_use_short_caches_and_ignore_cold_idle_delay(self):
        self.addCleanup(reset_class_scans)
        reset_class_scans()
        state=register_membership(
            'account-a',
            {'course_id':'fast-course','class_id':'fast-class','course_name':'Fast'},
        )
        state.idle_streak=3
        note_scan_success(state,'account-a',[],30,now=1000,mode='prewarm')
        self.assertEqual(state.next_due_at,1020)
        self.assertFalse(should_scan(state,30,now=1002,mode='prewarm'))
        self.assertFalse(should_scan(state,30,now=1008,mode='prewarm'))
        self.assertTrue(should_scan(state,30,now=1020,mode='prewarm'))
        note_scan_success(state,'account-a',[],5,now=1100,mode='burst')
        self.assertEqual(state.next_due_at,1105)
        self.assertFalse(should_scan(state,5,now=1102,mode='burst'))
        self.assertFalse(should_scan(state,5,now=1103,mode='burst'))
        self.assertTrue(should_scan(state,5,now=1105,mode='burst'))
        self.assertEqual(
            activity_cache_seconds(datetime(2026,11,13,12,tzinfo=timezone(timedelta(hours=8))),'burst'),
            3,
        )
        self.assertEqual(
            activity_cache_seconds(datetime(2026,11,13,12,tzinfo=timezone(timedelta(hours=8))),'prewarm'),
            8,
        )

    async def test_normal_scan_uses_wall_clock_for_quiet_hour_cache(self):
        self.addCleanup(reset_class_scans)
        reset_class_scans()
        state=register_membership(
            'account-a',
            {'course_id':'quiet-course','class_id':'quiet-class','course_name':'Quiet'},
        )
        note_scan_success(state,'account-a',[],30,now=1000)
        state.next_due_at=1000
        quiet=datetime(2026,11,13,23,tzinfo=timezone(timedelta(hours=8)))
        self.assertFalse(
            should_scan(state,30,now=1200,wall_now=quiet),
        )
        self.assertTrue(
            should_scan(state,30,now=1300,wall_now=quiet),
        )
        state.activities_at=1300
        self.assertFalse(
            should_scan(
                state,
                30,
                now=1301,
                wall_now=quiet,
                explicit_schedule=True,
            ),
        )
        self.assertTrue(
            should_scan(
                state,
                30,
                now=1320,
                wall_now=quiet,
                explicit_schedule=True,
            ),
        )

    async def test_request_guard_classifies_scan_sign_and_submit_requests(self):
        charges=[]
        response=SimpleNamespace(
            status=200,
            ok=True,
            json=AsyncMock(return_value={'result':1}),
            text=AsyncMock(return_value='success'),
        )
        client=AttendanceClient({},request_guard=charges.append)
        client.context=SimpleNamespace(get=AsyncMock(return_value=response),post=AsyncMock(return_value=response))
        await client.json('/mycourse/backclazzdata')
        await client.json('/widget/sign/pcStuSignController/checkSignCode')
        await client.json('/widget/sign/pcTeaSignController/getSignCode')
        with patch('app.services.attendance.availability.time.time',return_value=NOW):
            await client.submit({'params':{'activeId':'1'},'availability':{'start_time':NOW-30,'end_time':NOW+30}})
        self.assertEqual(charges,['scan','sign','sign','sign'])

    async def test_creator_sign_code_endpoint_is_the_fallback_for_activity_payload(self):
        self.assertEqual(find_sign_code({'result':1,'msg':'1258637'}),'1258637')
        self.assertEqual(find_sign_code({'result':0,'errorMsg':'没有权限'}),'')
        client=AttendanceClient({'id':'creator-code-account'})
        client.json=AsyncMock(side_effect=[
            {'result':1,'data':{}},
            {'result':1,'msg':'9999'},
        ])
        client._check_code=AsyncMock(return_value=(True,''))
        client.codebook_put=Mock()
        result=await client.resolve_sign_code({'upstream_id':'creator-activity','kind':'code'})
        self.assertEqual(result,{'code':'9999','source':'activity'})
        self.assertEqual(
            client.json.await_args_list[1].args[0],
            '/widget/sign/pcTeaSignController/getSignCode',
        )
        client.codebook_put.assert_called_once_with(
            'creator-activity',
            '9999',
            'activity',
            'code',
        )

    async def test_three_wrong_codes_stop_local_verification_before_a_fourth_request(self):
        client=AttendanceClient({'id':'code-attempt-account'})
        client.json=AsyncMock(return_value={'result':0,'errorMsg':'签到码不正确'})
        messages=[]
        for _ in range(3):
            valid,message=await client._check_code('activity-one','wrong-code')
            self.assertFalse(valid)
            messages.append(message)
        self.assertIn('3/3',messages[-1])
        valid,message=await client._check_code('activity-one','wrong-code')
        self.assertFalse(valid)
        self.assertIn('停止校验',message)
        self.assertEqual(client.json.await_count,3)

    async def test_verified_shared_code_is_reused_without_another_platform_check(self):
        client = AttendanceClient(
            {'id': 'shared-code-account'},
            codebook_get=lambda _: {'code': '9999', 'source': 'activity'},
        )
        client._check_code = AsyncMock()
        code = await client.resolve_sign_code({'upstream_id': 'activity-one', 'kind': 'code'})
        self.assertEqual(code, {'code': '9999', 'source': 'activity'})
        client._check_code.assert_not_awaited()

    async def test_wrong_code_receipt_removes_the_shared_code(self):
        client = AttendanceClient({'id': 'wrong-code-account'}, codebook_delete=Mock())
        response = SimpleNamespace(
            ok=True,
            status=200,
            text=AsyncMock(return_value='签到码不正确[90002]'),
        )
        client.context = SimpleNamespace(post=AsyncMock(return_value=response))
        now = time.time()
        result = await client.submit({
            'status': 'ready',
            'kind': 'code',
            'params': {'activeId': 'activity-wrong'},
            'availability': {
                'start_time': now - 60,
                'end_time': now + 60,
                'availability_status': 'open',
                'verified_at': utc_now(),
            },
        })
        self.assertEqual(result['status'], 'awaiting_input')
        client.codebook_delete.assert_called_once_with('activity-wrong')

    async def test_course_and_class_scan_results_are_shared_without_skipping_per_account_attendance(self):
        account={'id':'course-cache-account','platform_user_id':'1000'}
        payload={'result':1,'channelList':[{'content':{
            'id':'9002','name':'C806','state':0,
            'course':{'data':[{'id':'9001','name':'计算机科学'}]},
        }}]}
        first=AttendanceClient(account)
        first.json=AsyncMock(return_value=payload)
        courses=await first.courses()
        second=AttendanceClient(account)
        second.json=AsyncMock(side_effect=AssertionError('course cache should avoid another request'))
        self.assertEqual(await second.courses(),courses)

        course_row=courses[0]
        candidate={'activeType':2,'status':1,'id':991,'otherId':0,'startTime':NOW-60,'endTime':NOW+60}
        anchor=AttendanceClient({'id':'anchor-account','platform_user_id':'1001'})
        anchor.json=AsyncMock(side_effect=[
            {'result':1,'data':{'activeList':[candidate]}},
            {'status':1,'startTime':NOW-60,'endTime':NOW+60},
        ])
        with patch('app.services.attendance.availability.time.time',return_value=NOW):
            scanned=await anchor.class_activities(course_row)
        self.assertEqual([item['upstream_id'] for item in scanned],['991'])
        self.assertEqual(anchor.json.await_count,2)

        member=AttendanceClient({'id':'member-account','platform_user_id':'1002'})
        member.json=AsyncMock(return_value={'result':1,'data':{'status':0}})
        shared=await member.class_activities(course_row)
        records=await member.account_activities(course_row,shared)
        self.assertEqual([item['upstream_id'] for item in records],['991'])
        self.assertEqual(member.json.await_count,1)

    async def test_manual_force_only_refreshes_the_class_anchor_and_members_reuse_it(self):
        self.addCleanup(reset_class_scans)
        reset_class_scans()
        course = {'course_id': 'forced-course', 'class_id': 'forced-class', 'course_name': '计算机科学'}
        accounts = {'forced-anchor', 'forced-member'}
        anchor_id = choose_anchor(accounts)
        member_id = next(account for account in accounts if account != anchor_id)
        payload = {'result': 1, 'data': {'activeList': [{
            'activeType': 2, 'status': 1, 'id': 991,
            'startTime': NOW - 60, 'endTime': NOW + 60,
        }]}}
        detail = {'status': 1, 'startTime': NOW - 60, 'endTime': NOW + 60}
        anchor = AttendanceClient({'id': anchor_id, 'platform_user_id': '1001'})
        anchor.json = AsyncMock(side_effect=[payload, detail])
        with patch('app.services.attendance.availability.time.time', return_value=NOW):
            scanned = await anchor.class_activities(course, force=True, force_scan_token='manual-1')
        self.assertEqual([item['upstream_id'] for item in scanned], ['991'])
        self.assertEqual(anchor.json.await_count, 2)

        member = AttendanceClient({'id': member_id, 'platform_user_id': '1002'})
        member.json = AsyncMock(side_effect=AssertionError('member must reuse the anchor result'))
        with patch('app.services.attendance.availability.time.time', return_value=NOW):
            reused = await member.class_activities(course, force=True, force_scan_token='manual-1')
        self.assertEqual([item['upstream_id'] for item in reused], ['991'])
        self.assertEqual(member.json.await_count, 0)

        member.json = AsyncMock(side_effect=[payload, detail])
        with patch('app.services.attendance.availability.time.time', return_value=NOW):
            refreshed = await member.class_activities(course, force=True, force_scan_token='manual-2')
        self.assertEqual([item['upstream_id'] for item in refreshed], ['991'])
        self.assertEqual(member.json.await_count, 2)

    async def test_collection_rejects_ended_future_signed_and_keeps_only_current_unsigned(self):
        course={'course_id':'1','class_id':'2','course_name':'Course'}
        client=AttendanceClient({})
        candidates=[{'activeType':2,'status':1,'id':index,'otherId':2,'startTime':NOW-60,'endTime':NOW+60} for index in range(1,6)]
        client.json=AsyncMock(side_effect=[{'result':1,'data':{'activeList':candidates}},
            {'status':1,'isOver':1,'startTime':NOW-60,'endTime':NOW+60},
            {'status':1,'startTime':NOW+10,'endTime':NOW+60},
            {'status':1,'startTime':NOW-60,'endTime':NOW-1},
            {'status':1,'startTime':{'time':NOW*1000-60000},'endTime':{'time':NOW*1000+60000}},
            {'status':1,'startTime':NOW-60,'endTime':NOW+60},
            {'result':1,'data':{'status':1}}, {'result':1,'data':{'status':0}}])
        with patch('app.services.attendance.availability.time.time',return_value=NOW): records=await client.activities(course)
        self.assertEqual([item['upstream_id'] for item in records],['5'])
        self.assertEqual(records[0]['start_time'],NOW-60); self.assertEqual(records[0]['end_time'],NOW+60)
        self.assertEqual(records[0]['availability_status'],'open')

    async def test_unknown_deadline_is_a_reported_course_error(self):
        client=AttendanceClient({})
        client.json=AsyncMock(side_effect=[{'result':1,'data':{'activeList':[{'activeType':2,'status':1,'id':1,'startTime':NOW-60}]}},{'status':1,'startTime':NOW-60}])
        with patch('app.services.attendance.availability.time.time',return_value=NOW):
            with self.assertRaises(AttendanceError): await client.activities({'course_id':'1','class_id':'2','course_name':'Course'})

    async def test_crossing_the_deadline_before_submit_never_sends_the_request(self):
        client=AttendanceClient({}); client.context=SimpleNamespace(post=AsyncMock())
        with patch('app.services.attendance.availability.time.time',return_value=NOW):
            result=await client.submit({'params':{'activeId':'1'},'availability':{'start_time':NOW-30,'end_time':NOW}})
        self.assertEqual(result['status'],'expired'); client.context.post.assert_not_awaited()

    async def test_rate_limit_wait_cannot_submit_after_the_deadline(self):
        current=[NOW]
        async def pace(_):
            current[0]=NOW+2
        client=AttendanceClient({},request_guard=pace)
        client.context=SimpleNamespace(post=AsyncMock())
        with patch('app.services.attendance.availability.time.time',side_effect=lambda:current[0]):
            result=await client.submit({
                'params':{'activeId':'1'},
                'availability':{'start_time':NOW-30,'end_time':NOW+1},
            })
        self.assertEqual(result['status'],'expired')
        client.context.post.assert_not_awaited()

    async def test_cache_expiration_blocks_old_selections_without_deleting_batch_history(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(data_root=Path(folder)); settings.ensure_directories(); db=Database(settings); db.initialize()
            account=db.create_account('Student','chaoxing',settings); db.update_account(account['id'],status='ready')
            store=AttendanceStore(db); store.initialize()
            records=store.save_activities(account['id'],[{'upstream_id':'1','course_id':'2','class_id':'3','course_name':'Course','title':'Sign','kind':'normal','start_time':time.time()-10,'end_time':time.time()+60,'availability_status':'open','verified_at':utc_now()}],[('2','3')])
            identifier=records[0]['id']; batch=store.create_batch([identifier])
            with db.connect() as connection: connection.execute('UPDATE attendance_activities SET end_time=? WHERE id=?',(time.time()-1,identifier))
            self.assertEqual(store.activities(),[])
            with self.assertRaises(AttendanceError): store.create_batch([identifier])
            self.assertEqual(store.batch(batch['id'])['total'],1)

    async def test_legacy_cache_requires_reverification_and_is_preserved_as_history(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(data_root=Path(folder)); settings.ensure_directories(); db=Database(settings); db.initialize()
            account=db.create_account('Student','chaoxing',settings)
            with db.connect() as connection:
                connection.execute('CREATE TABLE attendance_activities(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,upstream_id TEXT NOT NULL,course_id TEXT NOT NULL,class_id TEXT NOT NULL,course_name TEXT NOT NULL,title TEXT NOT NULL,kind TEXT NOT NULL,start_time TEXT,active INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,UNIQUE(account_id,upstream_id,course_id,class_id))')
                connection.execute('INSERT INTO attendance_activities VALUES(?,?,?,?,?,?,?,?,?,?,?)',('old',account['id'],'1','2','3','Old course','Old sign','qr','1758868045000',1,utc_now()))
            store=AttendanceStore(db); store.initialize()
            self.assertEqual(store.activities(),[])
            with db.connect() as connection: row=connection.execute('SELECT active,availability_status FROM attendance_activities WHERE id=?',('old',)).fetchone()
            self.assertEqual(row['active'],0); self.assertEqual(row['availability_status'],'unverified')
