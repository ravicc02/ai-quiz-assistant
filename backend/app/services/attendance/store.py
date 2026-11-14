from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from ...db import utc_now
from .contracts import AttendanceError
from .availability import window

SUCCESS = {'succeeded', 'already_signed'}
ACTIVE = {'queued', 'running', 'submitting'}


def normalize_course_filters(course_filters=None, course_filter=None):
    values = course_filters if course_filters is not None else course_filter
    if isinstance(values, str):
        try:
            decoded = json.loads(values)
        except (TypeError, ValueError):
            decoded = values
        values = decoded if isinstance(decoded, list) else [decoded]
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values else []
    return list(dict.fromkeys(
        str(value).strip()[:200] for value in values if str(value or '').strip()
    ))[:20]


def normalize_scan_windows(windows=None):
    result = []
    for window in windows or []:
        if hasattr(window, 'model_dump'):
            window = window.model_dump()
        if not isinstance(window, dict):
            raise AttendanceError('检测时段格式无效')
        days = sorted({int(day) for day in window.get('days') or []})
        start = str(window.get('start') or '')
        end = str(window.get('end') or '')
        if not days or any(day < 1 or day > 7 for day in days):
            raise AttendanceError('检测时段必须选择 1 至 7 中的星期')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', start) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', end):
            raise AttendanceError('检测时段必须使用 HH:MM 格式')
        if start == end:
            raise AttendanceError('检测时段的开始和结束时间不能相同')
        result.append({'days':days,'start':start,'end':end})
    return result[:20]


class AttendanceStore:
    def __init__(self, database): self.database = database

    def initialize(self):
        with self.database.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attendance_activities(
                  id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                  upstream_id TEXT NOT NULL, course_id TEXT NOT NULL, class_id TEXT NOT NULL, course_name TEXT NOT NULL,
                  title TEXT NOT NULL, kind TEXT NOT NULL, start_time TEXT, active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
                  UNIQUE(account_id,upstream_id,course_id,class_id));
                CREATE TABLE IF NOT EXISTS attendance_batches(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attendance_items(
                  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES attendance_batches(id) ON DELETE CASCADE,
                  activity_id TEXT NOT NULL REFERENCES attendance_activities(id) ON DELETE CASCADE,
                  status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', attempt INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  UNIQUE(batch_id,activity_id));
                CREATE TABLE IF NOT EXISTS attendance_shares(
                  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES attendance_batches(id) ON DELETE CASCADE,
                  token_hash TEXT UNIQUE NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                  purpose TEXT NOT NULL DEFAULT 'qr', pin_hash TEXT, captured_at REAL, remote_session_id TEXT,
                  mode TEXT NOT NULL DEFAULT 'activity', account_ids_json TEXT);
                CREATE TABLE IF NOT EXISTS attendance_qr_groups(
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, remote_group_id TEXT UNIQUE NOT NULL,
                  account_ids_json TEXT NOT NULL, link_ciphertext TEXT NOT NULL,
                  requires_pin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                  revoked INTEGER NOT NULL DEFAULT 0, last_remote_run_id TEXT, last_batch_id TEXT,
                  last_status TEXT NOT NULL DEFAULT 'waiting', last_scan_at REAL, updated_at REAL NOT NULL,
                  FOREIGN KEY(last_batch_id) REFERENCES attendance_batches(id) ON DELETE SET NULL);
                CREATE TABLE IF NOT EXISTS attendance_account_assets(
                  account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                  photo_path TEXT, photo_mime TEXT, face_path TEXT, face_mime TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attendance_config(
                  id INTEGER PRIMARY KEY CHECK(id=1), latitude REAL, longitude REAL, address TEXT,
                  updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attendance_auto_groups(
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, account_ids_json TEXT NOT NULL,
                  kinds_json TEXT NOT NULL, course_filter TEXT NOT NULL DEFAULT '',
                  windows_json TEXT NOT NULL DEFAULT '[]',
                  interval_seconds INTEGER NOT NULL DEFAULT 30,
                  enabled INTEGER NOT NULL DEFAULT 1, location_json TEXT,
                  last_scan_at REAL, last_error TEXT NOT NULL DEFAULT '', last_batch_id TEXT,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  FOREIGN KEY(last_batch_id) REFERENCES attendance_batches(id) ON DELETE SET NULL);
                CREATE TABLE IF NOT EXISTS attendance_account_throttles(
                  account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                  failures INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attendance_request_budget(
                  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                  hour_key TEXT NOT NULL, scan_count INTEGER NOT NULL DEFAULT 0,
                  sign_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  PRIMARY KEY(account_id,hour_key));
                CREATE TABLE IF NOT EXISTS attendance_class_scan_state(
                  class_key TEXT PRIMARY KEY, course_id TEXT NOT NULL, class_id TEXT NOT NULL,
                  course_name TEXT NOT NULL DEFAULT '', class_name TEXT NOT NULL DEFAULT '',
                  members_json TEXT NOT NULL DEFAULT '[]', anchor TEXT NOT NULL DEFAULT '',
                  idle_streak INTEGER NOT NULL DEFAULT 0, failure_streak INTEGER NOT NULL DEFAULT 0,
                  backoff_seconds INTEGER NOT NULL DEFAULT 0, backoff_until REAL NOT NULL DEFAULT 0,
                  next_due_at REAL NOT NULL DEFAULT 0, last_scan_at REAL NOT NULL DEFAULT 0,
                  last_error TEXT NOT NULL DEFAULT '', scan_count INTEGER NOT NULL DEFAULT 0,
                  anchor_switches INTEGER NOT NULL DEFAULT 0, anchor_scans INTEGER NOT NULL DEFAULT 0, fallback_scans INTEGER NOT NULL DEFAULT 0,
                  activity_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attendance_codebook(
                  activity_id TEXT PRIMARY KEY, code TEXT NOT NULL, source TEXT NOT NULL, kind TEXT,
                  created_at REAL NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attendance_code_attempts(
                  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                  activity_id TEXT NOT NULL, wrong_count INTEGER NOT NULL DEFAULT 0,
                  locked_until REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  PRIMARY KEY(account_id,activity_id));
                CREATE TABLE IF NOT EXISTS attendance_auto_settings(
                  account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                  enabled INTEGER NOT NULL DEFAULT 0, kinds_json TEXT NOT NULL,
                  course_filter TEXT NOT NULL DEFAULT '', interval_seconds INTEGER NOT NULL DEFAULT 30,
                  location_json TEXT, last_scan_at REAL, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attendance_item_activity_status ON attendance_items(activity_id,status);
                CREATE INDEX IF NOT EXISTS attendance_class_scan_updated ON attendance_class_scan_state(updated_at);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(attendance_activities)')}
            additions = {'end_time':'REAL','availability_status':"TEXT NOT NULL DEFAULT 'unverified'",'verified_at':'TEXT','open_ended':'INTEGER NOT NULL DEFAULT 0'}
            migrated = False
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE attendance_activities ADD COLUMN {name} {definition}')
                    migrated = True
            class_scan_columns = {
                row['name'] for row in db.execute('PRAGMA table_info(attendance_class_scan_state)')
            }
            if 'anchor_switches' not in class_scan_columns:
                db.execute(
                    'ALTER TABLE attendance_class_scan_state '
                    'ADD COLUMN anchor_switches INTEGER NOT NULL DEFAULT 0'
                )
                migrated = True
            if migrated:
                db.execute("UPDATE attendance_activities SET active=0 WHERE availability_status='unverified'")
            auto_group_columns = {row['name'] for row in db.execute('PRAGMA table_info(attendance_auto_groups)')}
            if 'course_filter' not in auto_group_columns:
                db.execute("ALTER TABLE attendance_auto_groups ADD COLUMN course_filter TEXT NOT NULL DEFAULT ''")
            if 'windows_json' not in auto_group_columns:
                db.execute("ALTER TABLE attendance_auto_groups ADD COLUMN windows_json TEXT NOT NULL DEFAULT '[]'")
            db.execute('DELETE FROM attendance_request_budget WHERE hour_key<>?', (self.budget_hour(),))
            share_columns = {row['name'] for row in db.execute('PRAGMA table_info(attendance_shares)')}
            for name, definition in {
                'purpose': "TEXT NOT NULL DEFAULT 'qr'",
                'pin_hash': 'TEXT',
                'captured_at': 'REAL',
                'remote_session_id': 'TEXT',
                'mode': "TEXT NOT NULL DEFAULT 'activity'",
                'account_ids_json': 'TEXT',
            }.items():
                if name not in share_columns:
                    db.execute(f'ALTER TABLE attendance_shares ADD COLUMN {name} {definition}')

    def recover(self):
        with self.database.connect() as db:
            db.execute("UPDATE attendance_items SET status='unknown',message='应用重启，提交回执未确认，请先核对平台记录' WHERE status='submitting'")
            recovered = [
                row['batch_id']
                for row in db.execute(
                    "SELECT DISTINCT batch_id FROM attendance_items WHERE status IN ('queued','running')"
                ).fetchall()
            ]
            db.execute("""UPDATE attendance_items SET status='awaiting_input',message='应用重启，二维码需要重新扫码'
              WHERE status IN ('queued','running') AND activity_id IN (
                SELECT id FROM attendance_activities WHERE kind='qr'
              )""")
            db.execute("""UPDATE attendance_items SET status='pending',message='应用重启，等待恢复执行'
              WHERE status IN ('queued','running')""")
        return recovered

    def save_activities(self, account_id, rows, successful_courses, all_courses=None, failed_courses=None):
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for course_id, class_id in failed_courses or []:
                db.execute("UPDATE attendance_activities SET availability_status='unverified' WHERE account_id=? AND course_id=? AND class_id=?",(account_id,course_id,class_id))
            if all_courses is not None:
                known = set(all_courses)
                old = db.execute('SELECT DISTINCT course_id,class_id FROM attendance_activities WHERE account_id=?',(account_id,)).fetchall()
                for course in old:
                    if (course['course_id'],course['class_id']) not in known:
                        db.execute('UPDATE attendance_activities SET active=0 WHERE account_id=? AND course_id=? AND class_id=?',(account_id,course['course_id'],course['class_id']))
            for course_id, class_id in successful_courses:
                db.execute('UPDATE attendance_activities SET active=0 WHERE account_id=? AND course_id=? AND class_id=?', (account_id,course_id,class_id))
            for row in rows:
                availability = window(row)
                if row.get('availability_status') != 'open' or not row.get('verified_at') or availability['status'] != 'open': continue
                db.execute('''INSERT INTO attendance_activities(id,account_id,upstream_id,course_id,class_id,course_name,title,kind,start_time,active,updated_at,end_time,availability_status,verified_at,open_ended)
                    VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)
                    ON CONFLICT(account_id,upstream_id,course_id,class_id) DO UPDATE SET title=excluded.title,
                    course_name=excluded.course_name,kind=excluded.kind,start_time=excluded.start_time,active=1,updated_at=excluded.updated_at,
                    end_time=excluded.end_time,availability_status=excluded.availability_status,verified_at=excluded.verified_at,open_ended=excluded.open_ended''',
                    (uuid.uuid4().hex,account_id,row['upstream_id'],row['course_id'],row['class_id'],row['course_name'],row['title'],row['kind'],availability['start_time'],utc_now(),availability['end_time'],'open',row['verified_at'],int(availability['open_ended'])))
        return self.activities([account_id])

    def activities(self, account_ids=None, *, auto_only=False, retry_after=''):
        query = "SELECT t.*,a.name AS account_name,a.platform FROM attendance_activities t JOIN accounts a ON a.id=t.account_id WHERE t.active=1 AND t.availability_status='open' AND t.verified_at IS NOT NULL AND CAST(t.start_time AS REAL)<=? AND (t.end_time>? OR (t.end_time IS NULL AND t.open_ended=1)) AND NOT EXISTS (SELECT 1 FROM attendance_items i WHERE i.activity_id=t.id AND i.status IN ('succeeded','already_signed','unknown','queued','running','submitting'))"
        args = [time.time(), time.time()]
        if auto_only:
            query += " AND NOT EXISTS (SELECT 1 FROM attendance_items i WHERE i.activity_id=t.id AND i.status IN ('awaiting_input','manual_required')"
            if retry_after:
                query += ' AND i.updated_at>?'
                args.append(str(retry_after))
            query += ')'
        if account_ids:
            query += ' AND t.account_id IN (' + ','.join('?' for _ in account_ids) + ')'; args.extend(account_ids)
        with self.database.connect() as db:
            return [dict(row) for row in db.execute(query + ' ORDER BY t.course_name,a.name,t.title', args)]

    def activity_ids_for(self, upstream_id, account_ids):
        if not account_ids:
            raise AttendanceError('请选择需要签到的账号')
        placeholders = ','.join('?' for _ in account_ids)
        with self.database.connect() as db:
            rows = db.execute(f'''SELECT t.id FROM attendance_activities t
              WHERE t.upstream_id=? AND t.account_id IN ({placeholders})
              AND t.active=1 AND t.availability_status='open' AND t.verified_at IS NOT NULL
              AND CAST(t.start_time AS REAL)<=? AND (t.end_time>? OR (t.end_time IS NULL AND t.open_ended=1))''',
              [str(upstream_id), *account_ids, time.time(), time.time()]).fetchall()
        ids = [row['id'] for row in rows]
        if len(ids) != len(set(account_ids)):
            raise AttendanceError('所选账号没有全部出现在该签到活动中，请先重新采集')
        return ids

    def resolve_activity_ids(self, upstream_id, account_ids):
        if not account_ids:
            raise AttendanceError('请选择需要签到的账号')
        placeholders = ','.join('?' for _ in account_ids)
        now = time.time()
        with self.database.connect() as db:
            rows = db.execute(f'''SELECT t.id,t.account_id,t.course_name,t.title,t.kind
              FROM attendance_activities t JOIN accounts a ON a.id=t.account_id
              WHERE t.upstream_id=? AND t.account_id IN ({placeholders})
              AND t.active=1 AND a.platform='chaoxing' AND a.status='ready'
              AND t.availability_status='open' AND t.verified_at IS NOT NULL
              AND CAST(t.start_time AS REAL)<=? AND (t.end_time>? OR (t.end_time IS NULL AND t.open_ended=1))
              AND NOT EXISTS (SELECT 1 FROM attendance_items i WHERE i.activity_id=t.id
                AND i.status IN ('succeeded','already_signed','unknown','queued','running','submitting'))
              ORDER BY t.id''', [str(upstream_id), *account_ids, now, now]).fetchall()
        found = {}
        for row in rows:
            found.setdefault(row['account_id'], dict(row))
        return found

    def create_open_qr_batch(self, account_ids):
        unique = list(dict.fromkeys(str(account_id) for account_id in account_ids if account_id))
        if not unique or len(unique) > 100:
            raise AttendanceError('请选择 1 至 100 个签到账号')
        placeholders = ','.join('?' for _ in unique)
        with self.database.connect() as db:
            rows = db.execute(f'''SELECT id FROM accounts
              WHERE id IN ({placeholders}) AND platform='chaoxing' AND status='ready' ''', unique).fetchall()
            if len(rows) != len(unique):
                raise AttendanceError('部分账号不存在或尚未完成学习通授权')
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO attendance_batches VALUES(?,?)', (identifier, utc_now()))
        return self.batch(identifier)

    def populate_batch_items(self, batch_id, activity_ids):
        unique = list(dict.fromkeys(activity_ids))
        if not unique:
            raise AttendanceError('没有匹配到可执行的签到活动')
        now = time.time()
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = db.execute('SELECT id FROM attendance_batches WHERE id=?', (batch_id,)).fetchone()
            existing = db.execute('SELECT 1 FROM attendance_items WHERE batch_id=? LIMIT 1', (batch_id,)).fetchone()
            if not batch or existing:
                raise AttendanceError('扫码批次已处理或不存在')
            for activity_id in unique:
                row = db.execute('''SELECT t.id FROM attendance_activities t JOIN accounts a ON a.id=t.account_id
                  WHERE t.id=? AND t.active=1 AND a.platform='chaoxing' AND a.status='ready'
                  AND t.availability_status='open' AND t.verified_at IS NOT NULL
                  AND CAST(t.start_time AS REAL)<=? AND (t.end_time>? OR (t.end_time IS NULL AND t.open_ended=1))
                  AND NOT EXISTS (SELECT 1 FROM attendance_items i WHERE i.activity_id=t.id
                    AND i.status IN ('succeeded','already_signed','unknown','queued','running','submitting'))''',
                  (activity_id, now, now)).fetchone()
                if not row:
                    raise AttendanceError('匹配到的签到活动已失效，请重新采集')
                db.execute('INSERT INTO attendance_items VALUES(?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex, batch_id, activity_id, 'pending', '待执行', 0, utc_now()))
        return self.batch(batch_id)

    def create_batch(self, activity_ids):
        if not activity_ids or len(activity_ids) != len(set(activity_ids)): raise AttendanceError('请选择签到活动，且不能重复选择')
        identifier = uuid.uuid4().hex
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for activity_id in activity_ids:
                row = db.execute('''SELECT t.id FROM attendance_activities t JOIN accounts a ON a.id=t.account_id
                  WHERE t.id=? AND t.active=1 AND a.platform='chaoxing' AND a.status='ready' AND t.availability_status='open' AND t.verified_at IS NOT NULL
                  AND CAST(t.start_time AS REAL)<=? AND (t.end_time>? OR (t.end_time IS NULL AND t.open_ended=1))
                  AND NOT EXISTS (SELECT 1 FROM attendance_items i WHERE i.activity_id=t.id AND i.status IN ('succeeded','already_signed','unknown','queued','running','submitting'))''', (activity_id,time.time(),time.time())).fetchone()
                if not row: raise AttendanceError('所选活动已失效或账号尚未授权，请重新采集')
            db.execute('INSERT INTO attendance_batches VALUES(?,?)', (identifier,utc_now()))
            for activity_id in activity_ids:
                db.execute('INSERT INTO attendance_items VALUES(?,?,?,?,?,?,?)', (uuid.uuid4().hex,identifier,activity_id,'pending','待执行',0,utc_now()))
        return self.batch(identifier)

    def batch(self, identifier):
        with self.database.connect() as db:
            batch = db.execute('SELECT * FROM attendance_batches WHERE id=?',(identifier,)).fetchone()
            if not batch: raise KeyError('签到批次不存在')
            items = [dict(row) for row in db.execute('''SELECT i.*,t.account_id,t.upstream_id,t.course_id,t.class_id,t.course_name,t.title,t.kind,
                t.start_time,t.end_time,t.availability_status,t.verified_at,t.open_ended,a.name AS account_name FROM attendance_items i JOIN attendance_activities t ON t.id=i.activity_id
                JOIN accounts a ON a.id=t.account_id WHERE i.batch_id=? ORDER BY t.course_name,a.name,i.id''',(identifier,))]
            shares = [dict(row) for row in db.execute('''SELECT id,expires_at,revoked,purpose,mode,account_ids_json,captured_at
              FROM attendance_shares WHERE batch_id=? ORDER BY expires_at DESC''',(identifier,))]
            for share in shares:
                try:
                    share['account_ids'] = json.loads(share.pop('account_ids_json') or '[]')
                except (TypeError, ValueError):
                    share['account_ids'] = []
            participant_ids = list(dict.fromkeys(
                account_id
                for share in shares
                if not share['revoked'] and share['mode'] == 'open'
                for account_id in share.get('account_ids', [])
            ))
            participant_rows = []
            if participant_ids:
                placeholders = ','.join('?' for _ in participant_ids)
                participant_rows = [dict(row) for row in db.execute(
                    f'SELECT id,name,status FROM accounts WHERE id IN ({placeholders}) ORDER BY name',
                    participant_ids,
                )]
        states = [item['status'] for item in items]
        waiting_open = not items and any(not share['revoked'] and share['expires_at'] > time.time() and share['mode'] == 'open' for share in shares)
        state = 'waiting' if waiting_open else 'running' if any(value in ACTIVE for value in states) else 'completed' if states and all(value in SUCCESS for value in states) else 'needs_review' if 'unknown' in states else 'awaiting_input' if any(value in {'pending','awaiting_input'} for value in states) else 'finished'
        return {**dict(batch),'items':items,'shares':shares,'participants':participant_rows,'status':state,'completed':sum(value in SUCCESS for value in states),'total':len(items)}

    def batches(self):
        with self.database.connect() as db: ids = [row['id'] for row in db.execute('SELECT id FROM attendance_batches ORDER BY created_at DESC LIMIT 100')]
        return [self.batch(identifier) for identifier in ids]

    def update_item(self, identifier, status, message):
        with self.database.connect() as db:
            db.execute('UPDATE attendance_items SET status=?,message=?,updated_at=?,attempt=attempt+? WHERE id=?',
                (status,message[:240],utc_now(),int(status=='running'),identifier))

    def item_status(self, identifier):
        with self.database.connect() as db: return db.execute('SELECT status FROM attendance_items WHERE id=?',(identifier,)).fetchone()['status']

    def account_active(self, account_id):
        with self.database.connect() as db:
            return db.execute("SELECT 1 FROM attendance_items i JOIN attendance_activities a ON a.id=i.activity_id WHERE a.account_id=? AND i.status IN ('queued','running','submitting') LIMIT 1",(account_id,)).fetchone() is not None

    def prior_outcome(self, activity_id, item_id):
        with self.database.connect() as db:
            row = db.execute("SELECT status FROM attendance_items WHERE activity_id=? AND id!=? AND status IN ('succeeded','already_signed','unknown','submitting') ORDER BY CASE WHEN status IN ('unknown','submitting') THEN 0 ELSE 1 END,updated_at DESC LIMIT 1",(activity_id,item_id)).fetchone()
        return row['status'] if row else None

    def share(self, batch_id, minutes, *, purpose='input', pin='', remote_session_id='', mode='activity', account_ids=None):
        batch = self.batch(batch_id)
        account_ids = list(dict.fromkeys(account_ids or []))
        if mode not in {'activity', 'open'}:
            raise AttendanceError('未知的扫码链接模式')
        if purpose == 'qr' and mode == 'activity':
            kinds = {item['kind'] for item in batch['items']}
            activities = {item['upstream_id'] for item in batch['items']}
            if kinds != {'qr'} or len(activities) != 1:
                raise AttendanceError('扫码链接只能绑定一个二维码签到活动')
        if mode == 'open' and not account_ids:
            raise AttendanceError('开放扫码链接至少需要一个账号')
        token = secrets.token_urlsafe(32); identifier = uuid.uuid4().hex
        expires = time.time() + minutes * 60
        pin_hash = hashlib.sha256(pin.encode()).hexdigest() if pin else None
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_shares(
                id,batch_id,token_hash,expires_at,revoked,purpose,pin_hash,captured_at,remote_session_id,mode,account_ids_json
              ) VALUES(?,?,?,?,0,?,?,NULL,?,?,?)''',
              (identifier,batch_id,hashlib.sha256(token.encode()).hexdigest(),expires,purpose,pin_hash,
               remote_session_id or None,mode,json.dumps(account_ids) if mode == 'open' else None))
        return {'id':identifier,'token':token,'expires_at':expires,'requires_pin':bool(pin),'purpose':purpose,
                'mode':mode,'batch_id':batch_id,'account_ids':account_ids}

    def resolve_share(self, token):
        if not token or len(token) > 128: raise AttendanceError('分享链接无效或已失效')
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attendance_shares WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['revoked'] or row['expires_at'] <= time.time(): raise AttendanceError('分享链接无效、已过期或已撤销')
        return dict(row)

    def share_record(self, identifier):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attendance_shares WHERE id=?',(identifier,)).fetchone()
        if not row:
            raise KeyError('分享链接不存在')
        item = dict(row)
        try: item['account_ids'] = json.loads(item.pop('account_ids_json') or '[]')
        except (TypeError, ValueError): item['account_ids'] = []
        return item

    def pin_matches(self, record, pin):
        if not record.get('pin_hash'):
            return True
        return bool(pin) and secrets.compare_digest(record['pin_hash'], hashlib.sha256(pin.encode()).hexdigest())

    def capture_share(self, identifier, text):
        with self.database.connect() as db:
            changed = db.execute('''UPDATE attendance_shares SET captured_at=?
              WHERE id=? AND revoked=0 AND expires_at>? AND captured_at IS NULL''',
              (time.time(), identifier, time.time())).rowcount
        if changed != 1:
            raise AttendanceError('扫码链接已接收过二维码、已过期或已撤销')

    def reopen_share(self, identifier):
        with self.database.connect() as db:
            db.execute('''UPDATE attendance_shares SET captured_at=NULL
              WHERE id=? AND revoked=0 AND expires_at>?''',(identifier,time.time()))

    def revoke(self, identifier):
        with self.database.connect() as db: db.execute('UPDATE attendance_shares SET revoked=1 WHERE id=?',(identifier,))

    def revoke_batch(self, identifier):
        with self.database.connect() as db: db.execute('UPDATE attendance_shares SET revoked=1 WHERE batch_id=?',(identifier,))

    def has_live_shares(self):
        with self.database.connect() as db: return db.execute('SELECT 1 FROM attendance_shares WHERE revoked=0 AND expires_at>? LIMIT 1',(time.time(),)).fetchone() is not None

    def live_remote_shares(self):
        with self.database.connect() as db:
            rows = db.execute('''SELECT id,batch_id,expires_at,remote_session_id,mode,account_ids_json
              FROM attendance_shares
              WHERE revoked=0 AND expires_at>? AND remote_session_id IS NOT NULL''',(time.time(),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item['account_ids'] = json.loads(item.pop('account_ids_json') or '[]')
            except (TypeError, ValueError): item['account_ids'] = []
            result.append(item)
        return result

    def create_qr_group(self, name, account_ids, remote_group_id, link_ciphertext, requires_pin=False):
        unique = list(dict.fromkeys(str(account_id) for account_id in account_ids if account_id))
        if not unique or len(unique) > 100:
            raise AttendanceError('扫码链接需要选择 1 至 100 个签到账号')
        placeholders = ','.join('?' for _ in unique)
        with self.database.connect() as db:
            rows = db.execute(f'''SELECT id FROM accounts
              WHERE id IN ({placeholders}) AND platform='chaoxing' AND status='ready' ''', unique).fetchall()
            if len(rows) != len(unique):
                raise AttendanceError('部分账号不存在或尚未完成学习通授权')
            identifier = uuid.uuid4().hex
            now = time.time()
            db.execute('''INSERT INTO attendance_qr_groups(
                id,name,remote_group_id,account_ids_json,link_ciphertext,requires_pin,created_at,
                revoked,last_remote_run_id,last_batch_id,last_status,last_scan_at,updated_at)
              VALUES(?,?,?,?,?,?,?,0,NULL,NULL,'waiting',NULL,?)''',
              (identifier, str(name or '').strip()[:80], remote_group_id, json.dumps(unique),
               link_ciphertext, int(bool(requires_pin)), now, now))
        return self.qr_group(identifier)

    def qr_group(self, identifier):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attendance_qr_groups WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise KeyError('扫码链接不存在')
            item = dict(row)
            try: item['account_ids'] = json.loads(item.pop('account_ids_json') or '[]')
            except (TypeError, ValueError): item['account_ids'] = []
            item['requires_pin'] = bool(item['requires_pin'])
            item['revoked'] = bool(item['revoked'])
            participants = []
            if item['account_ids']:
                placeholders = ','.join('?' for _ in item['account_ids'])
                participants = [dict(account) for account in db.execute(
                    f'SELECT id,name,status FROM accounts WHERE id IN ({placeholders}) ORDER BY name',
                    item['account_ids'],
                )]
        item['participants'] = participants
        return item

    def qr_groups(self):
        with self.database.connect() as db:
            identifiers = [row['id'] for row in db.execute(
                'SELECT id FROM attendance_qr_groups ORDER BY revoked,created_at DESC LIMIT 100'
            )]
        return [self.qr_group(identifier) for identifier in identifiers]

    def live_qr_groups(self):
        with self.database.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM attendance_qr_groups WHERE revoked=0 ORDER BY created_at'
            )]

    def has_live_qr_groups(self):
        with self.database.connect() as db:
            return db.execute('SELECT 1 FROM attendance_qr_groups WHERE revoked=0 LIMIT 1').fetchone() is not None

    def set_qr_group_run(self, identifier, remote_run_id, batch_id, status, scanned=False):
        with self.database.connect() as db:
            db.execute('''UPDATE attendance_qr_groups SET last_remote_run_id=?,last_batch_id=?,
              last_status=?,last_scan_at=CASE WHEN ? THEN ? ELSE last_scan_at END,updated_at=?
              WHERE id=? AND revoked=0''',
              (remote_run_id, batch_id, status, int(bool(scanned)), time.time(), time.time(), identifier))

    def set_qr_group_status(self, identifier, status):
        with self.database.connect() as db:
            db.execute('UPDATE attendance_qr_groups SET last_status=?,updated_at=? WHERE id=?',
                       (status, time.time(), identifier))

    def revoke_qr_group(self, identifier):
        with self.database.connect() as db:
            changed = db.execute('UPDATE attendance_qr_groups SET revoked=1,last_status=\'revoked\',updated_at=? WHERE id=?',
                                 (time.time(), identifier)).rowcount
        if changed != 1:
            raise KeyError('扫码链接不存在')

    def rename_qr_group(self, identifier, name):
        clean = str(name or '').strip()[:80]
        if not clean:
            raise AttendanceError('链接名称不能为空')
        with self.database.connect() as db:
            changed = db.execute('UPDATE attendance_qr_groups SET name=?,updated_at=? WHERE id=? AND revoked=0',
                                 (clean,time.time(),identifier)).rowcount
        if changed != 1:
            raise KeyError('扫码链接不存在或已撤销')
        return self.qr_group(identifier)

    def account_assets(self, account_id):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attendance_account_assets WHERE account_id=?', (account_id,)).fetchone()
        return dict(row) if row else {
            'account_id':account_id,'photo_path':None,'photo_mime':None,
            'face_path':None,'face_mime':None,'updated_at':None,
        }

    def save_account_asset(self, account_id, kind, path, mime):
        if kind not in {'photo','face'}:
            raise AttendanceError('未知的账号素材类型')
        current = self.account_assets(account_id)
        current[f'{kind}_path'] = str(path)
        current[f'{kind}_mime'] = str(mime)
        now = utc_now()
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_account_assets(
                account_id,photo_path,photo_mime,face_path,face_mime,updated_at)
              VALUES(?,?,?,?,?,?)
              ON CONFLICT(account_id) DO UPDATE SET photo_path=excluded.photo_path,
                photo_mime=excluded.photo_mime,face_path=excluded.face_path,
                face_mime=excluded.face_mime,updated_at=excluded.updated_at''',
              (account_id,current.get('photo_path'),current.get('photo_mime'),
               current.get('face_path'),current.get('face_mime'),now))
        return self.account_assets(account_id)

    def delete_account_asset(self, account_id, kind):
        if kind not in {'photo','face'}:
            raise AttendanceError('未知的账号素材类型')
        with self.database.connect() as db:
            db.execute(f'UPDATE attendance_account_assets SET {kind}_path=NULL,{kind}_mime=NULL,updated_at=? WHERE account_id=?',
                       (utc_now(), account_id))
        return self.account_assets(account_id)

    def global_location(self):
        with self.database.connect() as db:
            row = db.execute('SELECT latitude,longitude,address,updated_at FROM attendance_config WHERE id=1').fetchone()
        if not row or row['latitude'] is None or row['longitude'] is None or not row['address']:
            return None
        return {'latitude':float(row['latitude']),'longitude':float(row['longitude']),
                'address':row['address'],'source':'manual','updated_at':row['updated_at']}

    def save_global_location(self, location):
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_config(id,latitude,longitude,address,updated_at)
              VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET latitude=excluded.latitude,
                longitude=excluded.longitude,address=excluded.address,updated_at=excluded.updated_at''',
              (float(location['latitude']),float(location['longitude']),str(location['address']),utc_now()))
        return self.global_location()

    def auto_group(self, identifier):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attendance_auto_groups WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise KeyError('自动签到分组不存在')
            item = dict(row)
            try: item['account_ids'] = json.loads(item.pop('account_ids_json') or '[]')
            except (TypeError, ValueError): item['account_ids'] = []
            try: item['kinds'] = json.loads(item.pop('kinds_json') or '[]')
            except (TypeError, ValueError): item['kinds'] = []
            item['course_filters'] = normalize_course_filters(course_filter=item.get('course_filter'))
            item['course_filter'] = '、'.join(item['course_filters'])
            try: item['windows'] = normalize_scan_windows(json.loads(item.pop('windows_json') or '[]'))
            except (TypeError, ValueError, AttendanceError): item['windows'] = []
            try: item['location'] = json.loads(item.pop('location_json')) if item.get('location_json') else None
            except (TypeError, ValueError): item['location'] = None
            item.pop('location_json', None)
            item['cooldown_until'] = 0
            item['enabled'] = bool(item['enabled'])
            participants = []
            cooldowns = {}
            if item['account_ids']:
                placeholders = ','.join('?' for _ in item['account_ids'])
                participants = [dict(account) for account in db.execute(
                    f'SELECT id,name,status FROM accounts WHERE id IN ({placeholders}) ORDER BY name',
                    item['account_ids'],
                )]
                cooldowns = {
                    row['account_id']: float(row['cooldown_until'] or 0)
                    for row in db.execute(
                        f'''SELECT account_id,cooldown_until FROM attendance_account_throttles
                            WHERE account_id IN ({placeholders})''',
                        item['account_ids'],
                    )
                }
            now = time.time()
            active_cooldowns = {
                account_id: until for account_id, until in cooldowns.items()
                if until > now
            }
            item['account_cooldowns'] = {account_id: cooldowns.get(account_id, 0) for account_id in item['account_ids']}
            item['all_cooling'] = bool(item['account_ids']) and all(
                cooldowns.get(account_id, 0) > now for account_id in item['account_ids']
            )
            if active_cooldowns:
                item['cooldown_until'] = max(active_cooldowns.values())
        item['participants'] = participants
        return item

    def auto_groups(self):
        with self.database.connect() as db:
            identifiers = [row['id'] for row in db.execute(
                'SELECT id FROM attendance_auto_groups ORDER BY enabled DESC,created_at DESC LIMIT 100'
            )]
        return [self.auto_group(identifier) for identifier in identifiers]

    def account_throttle(self, account_id):
        with self.database.connect() as db:
            row = db.execute(
                'SELECT failures,cooldown_until FROM attendance_account_throttles WHERE account_id=?',
                (account_id,),
            ).fetchone()
        return {
            'account_id':account_id,
            'failures':int(row['failures'] or 0) if row else 0,
            'cooldown_until':float(row['cooldown_until'] or 0) if row else 0,
        }

    def note_account_throttle(self, account_id, now=None):
        current = self.account_throttle(account_id)
        failures = min(6, current['failures'] + 1)
        delay = min(1800, 60 * (2 ** (failures - 1)))
        cooldown_until = float(now if now is not None else time.time()) + delay
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_account_throttles(account_id,failures,cooldown_until,updated_at)
              VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET failures=excluded.failures,
                cooldown_until=excluded.cooldown_until,updated_at=excluded.updated_at''',
              (account_id, failures, cooldown_until, utc_now()))
        return {
            'account_id':account_id,
            'failures':failures,
            'cooldown_until':cooldown_until,
            'delay_seconds':delay,
        }

    def clear_account_throttle(self, account_id):
        with self.database.connect() as db:
            db.execute('DELETE FROM attendance_account_throttles WHERE account_id=?', (account_id,))

    def budget_hour(self, now=None):
        current = now or datetime.now(ZoneInfo('Asia/Shanghai'))
        return current.strftime('%Y-%m-%dT%H')

    def request_budget(self, account_id, now=None):
        hour = self.budget_hour(now)
        with self.database.connect() as db:
            row = db.execute(
                '''SELECT scan_count,sign_count FROM attendance_request_budget
                   WHERE account_id=? AND hour_key=?''',
                (account_id, hour),
            ).fetchone()
        return {
            'account_id':account_id,
            'hour':hour,
            'scan_count':int(row['scan_count'] or 0) if row else 0,
            'sign_count':int(row['sign_count'] or 0) if row else 0,
        }

    def charge_request_budget(self, account_id, kind, *, scan_limit, sign_limit, now=None):
        if kind not in {'scan','sign'}:
            raise AttendanceError('未知的签到请求类型')
        hour = self.budget_hour(now)
        scan_limit = max(1, int(scan_limit))
        sign_limit = max(1, int(sign_limit))
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                '''SELECT scan_count,sign_count FROM attendance_request_budget
                   WHERE account_id=? AND hour_key=?''',
                (account_id, hour),
            ).fetchone()
            scan_count = int(row['scan_count'] or 0) if row else 0
            sign_count = int(row['sign_count'] or 0) if row else 0
            if kind == 'scan' and scan_count >= scan_limit:
                raise AttendanceError(f'该账号本小时扫描额度已用完（{scan_limit} 次），签到请求仍会保留')
            if kind == 'sign' and sign_count >= sign_limit:
                raise AttendanceError(f'该账号本小时签到请求额度已用完（{sign_limit} 次），请等待下个小时')
            column = 'scan_count' if kind == 'scan' else 'sign_count'
            db.execute(f'''INSERT INTO attendance_request_budget(
                account_id,hour_key,scan_count,sign_count,updated_at)
              VALUES(?,?,?,?,?)
              ON CONFLICT(account_id,hour_key) DO UPDATE SET
                {column}={column}+1,updated_at=excluded.updated_at''',
              (account_id, hour, int(kind == 'scan'), int(kind == 'sign'), utc_now()))
            updated = db.execute(
                '''SELECT scan_count,sign_count FROM attendance_request_budget
                   WHERE account_id=? AND hour_key=?''',
                (account_id, hour),
            ).fetchone()
        return {
            'account_id':account_id,
            'hour':hour,
            'scan_count':int(updated['scan_count'] or 0),
            'sign_count':int(updated['sign_count'] or 0),
        }

    def class_scan_state(self, class_key):
        with self.database.connect() as db:
            row = db.execute(
                'SELECT * FROM attendance_class_scan_state WHERE class_key=?',
                (str(class_key),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item['members'] = json.loads(item.pop('members_json') or '[]')
        except (TypeError, ValueError):
            item['members'] = []
        return item

    def save_class_scan_state(self, state):
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_class_scan_state(
                class_key,course_id,class_id,course_name,class_name,members_json,anchor,
                idle_streak,failure_streak,backoff_seconds,backoff_until,next_due_at,
                last_scan_at,last_error,scan_count,anchor_switches,anchor_scans,fallback_scans,activity_count,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(class_key) DO UPDATE SET
                course_id=excluded.course_id,class_id=excluded.class_id,
                course_name=excluded.course_name,class_name=excluded.class_name,
                members_json=excluded.members_json,anchor=excluded.anchor,
                idle_streak=excluded.idle_streak,failure_streak=excluded.failure_streak,
                backoff_seconds=excluded.backoff_seconds,backoff_until=excluded.backoff_until,
                next_due_at=excluded.next_due_at,last_scan_at=excluded.last_scan_at,
                last_error=excluded.last_error,scan_count=excluded.scan_count,
                anchor_switches=excluded.anchor_switches,anchor_scans=excluded.anchor_scans,fallback_scans=excluded.fallback_scans,
                activity_count=excluded.activity_count,updated_at=excluded.updated_at''',(
                str(state.get('class_key') or ''),
                str(state.get('course_id') or ''),
                str(state.get('class_id') or ''),
                str(state.get('course_name') or '')[:200],
                str(state.get('class_name') or '')[:200],
                json.dumps(sorted({str(member) for member in state.get('members') or [] if member})),
                str(state.get('anchor') or ''),
                max(0,int(state.get('idle_streak') or 0)),
                max(0,int(state.get('failure_streak') or 0)),
                max(0,int(state.get('backoff_seconds') or 0)),
                float(state.get('backoff_until') or 0),
                float(state.get('next_due_at') or 0),
                float(state.get('last_scan_at') or 0),
                str(state.get('last_error') or '')[:240],
                max(0,int(state.get('scan_count') or 0)),
                max(0,int(state.get('anchor_switches') or 0)),
                max(0,int(state.get('anchor_scans') or 0)),
                max(0,int(state.get('fallback_scans') or 0)),
                max(0,int(state.get('activity_count') or 0)),
                utc_now(),
            ))

    def class_scan_summary(self, account_ids=None):
        selected = {str(account_id) for account_id in account_ids or [] if account_id}
        with self.database.connect() as db:
            rows = db.execute(
                'SELECT * FROM attendance_class_scan_state ORDER BY updated_at DESC LIMIT 5000'
            ).fetchall()
        states = []
        for row in rows:
            try:
                members = set(json.loads(row['members_json'] or '[]'))
            except (TypeError, ValueError):
                members = set()
            if selected and not members.intersection(selected):
                continue
            states.append((dict(row), members))
        current = time.time()
        return {
            'classes':len(states),
            'anchored':sum(bool(state['anchor']) for state, _ in states),
            'cold_classes':sum(int(state['idle_streak'] or 0) > 0 for state, _ in states),
            'cooling_classes':sum(float(state['backoff_until'] or 0) > current for state, _ in states),
            'scans':sum(int(state['scan_count'] or 0) for state, _ in states),
            'anchor_switches':sum(int(state.get('anchor_switches') or 0) for state, _ in states),
            'reused':0,
            'anchor_scans':sum(int(state['anchor_scans'] or 0) for state, _ in states),
            'fallback_scans':sum(int(state['fallback_scans'] or 0) for state, _ in states),
        }

    def _validate_auto_group_materials(self, account_ids, kinds, location=None):
        if 'photo' in kinds:
            placeholders = ','.join('?' for _ in account_ids)
            with self.database.connect() as db:
                rows = db.execute(f'''SELECT a.name, assets.photo_path
                  FROM accounts a
                  LEFT JOIN attendance_account_assets assets ON assets.account_id=a.id
                  WHERE a.id IN ({placeholders})''', account_ids).fetchall()
            missing = [row['name'] for row in rows if not row['photo_path']]
            if missing:
                raise AttendanceError('拍照签到缺少账号照片素材：'+'、'.join(missing))
        if 'location' in kinds and not location and not self.global_location():
            raise AttendanceError('位置签到需要先设置位置签到素材')

    def create_auto_group(self, name, account_ids, kinds, interval_seconds=30, enabled=True,
                          location=None, course_filter='', course_filters=None, windows=None):
        unique = list(dict.fromkeys(str(account_id) for account_id in account_ids if account_id))
        if not unique or len(unique) > 100:
            raise AttendanceError('自动签到分组需要选择 1 至 100 个账号')
        kinds = [kind for kind in kinds if kind in {'normal','photo','gesture','code','location'}]
        if not kinds:
            raise AttendanceError('自动签到分组至少选择一种处理类型')
        filters = normalize_course_filters(course_filters, course_filter)
        windows = normalize_scan_windows(windows)
        placeholders = ','.join('?' for _ in unique)
        with self.database.connect() as db:
            accounts = db.execute(f'''SELECT id FROM accounts
              WHERE id IN ({placeholders}) AND platform='chaoxing' AND status='ready' ''', unique).fetchall()
            if len(accounts) != len(unique):
                raise AttendanceError('部分账号不存在或尚未完成学习通授权')
            self._validate_auto_group_materials(unique,kinds,location)
            identifier = uuid.uuid4().hex; now = time.time()
            db.execute('''INSERT INTO attendance_auto_groups(
                id,name,account_ids_json,kinds_json,course_filter,windows_json,interval_seconds,enabled,location_json,
                last_scan_at,last_error,last_batch_id,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?, '',NULL,?,?)''',
              (identifier,str(name or '').strip()[:80],json.dumps(unique),json.dumps(kinds),
               json.dumps(filters,ensure_ascii=False),json.dumps(windows,ensure_ascii=False),
               max(30,min(3600,int(interval_seconds or 30))),int(bool(enabled)),
               json.dumps(location,ensure_ascii=False) if location else None,None,now,now))
        return self.auto_group(identifier)

    def save_auto_group(self, identifier, *, name=None, account_ids=None, kinds=None,
                        interval_seconds=None, enabled=None, location=None, course_filter=None,
                        course_filters=None, windows=None):
        validate_materials = account_ids is not None or kinds is not None or location is not None
        current = self.auto_group(identifier)
        name = current['name'] if name is None else str(name).strip()[:80]
        account_ids = current['account_ids'] if account_ids is None else list(dict.fromkeys(account_ids))
        kinds = current['kinds'] if kinds is None else kinds
        kinds = [kind for kind in kinds if kind in {'normal','photo','gesture','code','location'}]
        filters = normalize_course_filters(
            course_filters if course_filters is not None else course_filter,
            None,
        ) if course_filters is not None or course_filter is not None else current['course_filters']
        windows = current['windows'] if windows is None else normalize_scan_windows(windows)
        interval = current['interval_seconds'] if interval_seconds is None else max(30,min(3600,int(interval_seconds)))
        if not name or not account_ids or not kinds:
            raise AttendanceError('自动签到分组名称、账号和处理类型不能为空')
        with self.database.connect() as db:
            placeholders = ','.join('?' for _ in account_ids)
            accounts = db.execute(f'''SELECT id FROM accounts
              WHERE id IN ({placeholders}) AND platform='chaoxing' AND status='ready' ''',account_ids).fetchall()
            if len(accounts) != len(account_ids):
                raise AttendanceError('部分账号不存在或尚未完成学习通授权')
            if validate_materials:
                self._validate_auto_group_materials(account_ids,kinds,location)
            db.execute('''UPDATE attendance_auto_groups SET name=?,account_ids_json=?,kinds_json=?,
              course_filter=?,windows_json=?,interval_seconds=?,enabled=?,location_json=?,updated_at=? WHERE id=?''',
              (name,json.dumps(account_ids),json.dumps(kinds),json.dumps(filters,ensure_ascii=False),
               json.dumps(windows,ensure_ascii=False),interval,
               int(current['enabled'] if enabled is None else bool(enabled)),
               json.dumps(location,ensure_ascii=False) if location is not None else
                 (json.dumps(current['location'],ensure_ascii=False) if current.get('location') else None),
               utc_now(),identifier))
        return self.auto_group(identifier)

    def delete_auto_group(self, identifier):
        with self.database.connect() as db:
            changed = db.execute('DELETE FROM attendance_auto_groups WHERE id=?', (identifier,)).rowcount
        if changed != 1:
            raise KeyError('自动签到分组不存在')

    def mark_auto_group_scan(self, identifier, *, error='', batch_id=None):
        with self.database.connect() as db:
            db.execute('''UPDATE attendance_auto_groups SET last_scan_at=?,last_error=?,last_batch_id=COALESCE(?,last_batch_id),
              updated_at=? WHERE id=?''', (time.time(),str(error or '')[:240],batch_id,utc_now(),identifier))
        return self.auto_group(identifier)

    def codebook_get(self, activity_id, max_age=7200):
        now = time.time()
        with self.database.connect() as db:
            row = db.execute('''SELECT * FROM attendance_codebook
              WHERE activity_id=? AND expires_at>? AND created_at>=?''',
              (str(activity_id), now, now - max_age)).fetchone()
        return dict(row) if row else None

    def codebook_put(self, activity_id, code, source, kind=''):
        now = time.time()
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_codebook(activity_id,code,source,kind,created_at,expires_at)
              VALUES(?,?,?,?,?,?)
              ON CONFLICT(activity_id) DO UPDATE SET code=excluded.code,source=excluded.source,
                kind=excluded.kind,created_at=excluded.created_at,expires_at=excluded.expires_at''',
              (str(activity_id), code, source, kind or '', now, now + 7 * 24 * 3600))

    def codebook_delete(self, activity_id):
        with self.database.connect() as db:
            db.execute('DELETE FROM attendance_codebook WHERE activity_id=?', (str(activity_id),))

    def code_attempt(self, account_id, activity_id, now=None):
        current = float(now if now is not None else time.time())
        with self.database.connect() as db:
            row = db.execute('''SELECT wrong_count,locked_until FROM attendance_code_attempts
              WHERE account_id=? AND activity_id=?''', (account_id, str(activity_id))).fetchone()
            if not row:
                return {'wrong_count':0,'locked_until':0}
            item = {
                'wrong_count':int(row['wrong_count'] or 0),
                'locked_until':float(row['locked_until'] or 0),
            }
            if item['locked_until'] and item['locked_until'] <= current:
                db.execute('''UPDATE attendance_code_attempts
                  SET wrong_count=0,locked_until=0,updated_at=?
                  WHERE account_id=? AND activity_id=?''',
                  (utc_now(), account_id, str(activity_id)))
                return {'wrong_count':0,'locked_until':0}
            return item

    def note_code_attempt(self, account_id, activity_id, *, locked=False, now=None):
        current = float(now if now is not None else time.time())
        state = self.code_attempt(account_id, activity_id, now=current)
        wrong_count = 3 if locked else min(3, state['wrong_count'] + 1)
        locked_until = current + 3600 if locked or wrong_count >= 3 else state['locked_until']
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_code_attempts(
                account_id,activity_id,wrong_count,locked_until,updated_at)
              VALUES(?,?,?,?,?)
              ON CONFLICT(account_id,activity_id) DO UPDATE SET
                wrong_count=excluded.wrong_count,locked_until=excluded.locked_until,
                updated_at=excluded.updated_at''',
              (account_id, str(activity_id), wrong_count, locked_until, utc_now()))
        return {'wrong_count':wrong_count,'locked_until':locked_until}

    def clear_code_attempt(self, account_id, activity_id):
        with self.database.connect() as db:
            db.execute('DELETE FROM attendance_code_attempts WHERE account_id=? AND activity_id=?',
                       (account_id, str(activity_id)))

    def auto_settings(self, account_id):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attendance_auto_settings WHERE account_id=?', (account_id,)).fetchone()
        if not row:
            return {'account_id':account_id,'enabled':False,'kinds':['normal','gesture','code','qr','location'],
                    'course_filter':'','interval_seconds':30,'location':None,'last_scan_at':0,'last_error':''}
        item = dict(row)
        try: item['kinds'] = json.loads(item.pop('kinds_json'))
        except (TypeError, ValueError): item['kinds'] = []
        try: item['location'] = json.loads(item.pop('location_json')) if item.get('location_json') else None
        except (TypeError, ValueError): item['location'] = None
        item.pop('location_json', None)
        item['enabled'] = bool(item['enabled'])
        return item

    def save_auto_settings(self, account_id, payload):
        current = self.auto_settings(account_id)
        kinds = [kind for kind in payload.get('kinds', current['kinds']) if kind in {'normal','gesture','code','qr','location','photo'}]
        course_filter = str(payload.get('course_filter', current['course_filter']) or '').strip()[:200]
        interval = max(30, min(3600, int(payload.get('interval_seconds', current['interval_seconds']) or 30)))
        location = payload.get('location', current['location'])
        now = utc_now()
        with self.database.connect() as db:
            db.execute('''INSERT INTO attendance_auto_settings(
                account_id,enabled,kinds_json,course_filter,interval_seconds,location_json,last_scan_at,last_error,updated_at
              ) VALUES(?,?,?,?,?,?,?,?,?)
              ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,kinds_json=excluded.kinds_json,
                course_filter=excluded.course_filter,interval_seconds=excluded.interval_seconds,
                location_json=excluded.location_json,updated_at=excluded.updated_at''',
              (account_id,int(bool(payload.get('enabled',current['enabled']))),json.dumps(kinds),
               course_filter,interval,json.dumps(location,ensure_ascii=False) if location else None,
               current.get('last_scan_at') or None,current.get('last_error') or '',now))
        return self.auto_settings(account_id)

    def auto_accounts(self):
        with self.database.connect() as db:
            rows = db.execute('''SELECT s.account_id FROM attendance_auto_settings s
              JOIN accounts a ON a.id=s.account_id
              WHERE s.enabled=1 AND a.platform='chaoxing' AND a.status='ready' ''').fetchall()
        return [self.auto_settings(row['account_id']) for row in rows]

    def mark_auto_scan(self, account_id, error=''):
        with self.database.connect() as db:
            db.execute('UPDATE attendance_auto_settings SET last_scan_at=?,last_error=?,updated_at=? WHERE account_id=?',
                (time.time(), str(error or '')[:240], utc_now(), account_id))
