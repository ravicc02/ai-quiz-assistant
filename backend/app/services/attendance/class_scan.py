from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger('mumu.attendance')

ACTIVE_CACHE_SECONDS = 20
QUIET_CACHE_SECONDS = 5 * 60
PREWARM_CACHE_SECONDS = 8
BURST_CACHE_SECONDS = 3
PREWARM_SCAN_SECONDS = 20
BURST_SCAN_SECONDS = 5
MAX_IDLE_FACTOR = 8
BACKOFF_SECONDS = (60, 120, 240, 480, 960, 1800)
THROTTLE_MARKERS = ('频繁', '稍后', '限流', 'too many', '429')
SCAN_MODES = {'normal', 'prewarm', 'burst'}
AUTH_MARKERS = ('重新登录', '登录态', '未授权', '身份未通过', 'auth', 'session')


def class_key(course):
    return f"{course.get('course_id') or ''}:{course.get('class_id') or ''}"


def activity_cache_seconds(now=None, mode='normal', *, quiet_hours=True):
    if mode == 'burst':
        return BURST_CACHE_SECONDS
    if mode == 'prewarm':
        return PREWARM_CACHE_SECONDS
    if not quiet_hours:
        return ACTIVE_CACHE_SECONDS
    current = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    return ACTIVE_CACHE_SECONDS if 7 <= current.hour < 22 else QUIET_CACHE_SECONDS


def is_throttle_error(error):
    message = str(error or '').casefold()
    return any(marker.casefold() in message for marker in THROTTLE_MARKERS)


def is_auth_error(error):
    message = str(error or '').casefold()
    return any(marker.casefold() in message for marker in AUTH_MARKERS)


def anchor_sort_key(account_id):
    return hashlib.sha1(f'anchor:{account_id}'.encode('utf-8')).hexdigest()


def choose_anchor(members):
    if not members:
        return ''
    return min((str(member) for member in members), key=anchor_sort_key)


def choose_alternate_anchor(members, current):
    candidates = {
        str(member)
        for member in members or []
        if member and str(member) != str(current or '')
    }
    return choose_anchor(candidates)


def switch_anchor_for_account(account_id, reason='账号认证失败'):
    switched = 0
    for state in list(_STATES.values()):
        if state.anchor != str(account_id or ''):
            continue
        if _switch_state_anchor(state, reason):
            switched += 1
    return switched


def _switch_state_anchor(state, reason='账号认证失败'):
    replacement = choose_alternate_anchor(state.members, state.anchor)
    if not replacement:
        return False
    state.anchor = replacement
    state.anchor_switches += 1
    state.failure_streak = 0
    state.backoff_until = 0
    state.backoff_seconds = 0
    state.next_due_at = 0
    state.activities_at = 0
    state.last_error = '班级扫描锚点登录失效，已切换备用账号'
    _persist_state(state)
    logger.warning(
        'Attendance class anchor switched after authentication failure',
        extra={
            'diagnostic_code':'attendance.class_scan_anchor_switch',
            'diagnostic_data':{
                'class_id':state.class_id,
                'course_id':state.course_id,
                'reason':str(reason or '')[:120],
            },
        },
    )
    return True


@dataclass
class ClassScanState:
    key: str
    course_id: str
    class_id: str
    course_name: str = ''
    class_name: str = ''
    members: set[str] = field(default_factory=set)
    anchor: str = ''
    activities: list | None = None
    activities_at: float = 0
    last_scan_at: float = 0
    last_scan_wall_at: float = 0
    last_scan_by: str = ''
    last_force_token: str = ''
    activity_count: int = 0
    next_due_at: float = 0
    idle_streak: int = 0
    failure_streak: int = 0
    backoff_until: float = 0
    backoff_seconds: int = 0
    last_error: str = ''
    scan_count: int = 0
    anchor_switches: int = 0
    reuse_count: int = 0
    anchor_scans: int = 0
    fallback_scans: int = 0
    persist: object | None = None


_STATES: dict[tuple[int, str], ClassScanState] = {}
_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _loop_scope(course):
    return id(asyncio.get_running_loop()), class_key(course)


def _hydrate_state(state, stored):
    current_wall = time.time()
    current_mono = time.monotonic()
    state.course_name = str(stored.get('course_name') or '')[:200]
    state.class_name = str(stored.get('class_name') or '')[:200]
    state.members = {str(member) for member in stored.get('members') or [] if member}
    state.anchor = str(stored.get('anchor') or choose_anchor(state.members))
    state.idle_streak = max(0, int(stored.get('idle_streak') or 0))
    state.failure_streak = max(0, int(stored.get('failure_streak') or 0))
    state.backoff_seconds = max(0, int(stored.get('backoff_seconds') or 0))
    state.backoff_until = current_mono + max(
        0,
        float(stored.get('backoff_until') or 0) - current_wall,
    )
    state.next_due_at = current_mono + max(
        0,
        float(stored.get('next_due_at') or 0) - current_wall,
    )
    state.last_scan_wall_at = float(stored.get('last_scan_at') or 0)
    state.last_error = str(stored.get('last_error') or '')[:240]
    state.scan_count = max(0, int(stored.get('scan_count') or 0))
    state.anchor_switches = max(0, int(stored.get('anchor_switches') or 0))
    state.anchor_scans = max(0, int(stored.get('anchor_scans') or 0))
    state.fallback_scans = max(0, int(stored.get('fallback_scans') or 0))
    state.activity_count = max(0, int(stored.get('activity_count') or 0))
    if state.scan_count and state.activity_count == 0:
        state.activities = []
        state.activities_at = current_mono - QUIET_CACHE_SECONDS - 1


def _persist_state(state):
    if not state.persist:
        return
    current_wall = time.time()
    current_mono = time.monotonic()
    snapshot = {
        'class_key': state.key,
        'course_id': state.course_id,
        'class_id': state.class_id,
        'course_name': state.course_name,
        'class_name': state.class_name,
        'members': sorted(state.members),
        'anchor': state.anchor,
        'idle_streak': state.idle_streak,
        'failure_streak': state.failure_streak,
        'backoff_seconds': state.backoff_seconds,
        'backoff_until': (
            current_wall + max(0, state.backoff_until - current_mono)
            if state.backoff_until
            else 0
        ),
        'next_due_at': (
            current_wall + max(0, state.next_due_at - current_mono)
            if state.next_due_at
            else 0
        ),
        'last_scan_at': state.last_scan_wall_at,
        'last_error': state.last_error,
        'scan_count': state.scan_count,
        'anchor_switches': state.anchor_switches,
        'anchor_scans': state.anchor_scans,
        'fallback_scans': state.fallback_scans,
        'activity_count': state.activity_count,
    }
    try:
        state.persist(snapshot)
    except Exception:
        logger.warning(
            'Attendance class scan persistence failed',
            exc_info=True,
            extra={
                'diagnostic_code':'attendance.class_scan_persist_error',
                'diagnostic_data':{'class_id':state.class_id,'course_id':state.course_id},
            },
        )


def register_membership(account_id, course, *, load=None, persist=None):
    scope = _loop_scope(course)
    state = _STATES.get(scope)
    changed = False
    if state is None:
        state = ClassScanState(
            key=scope[1],
            course_id=str(course.get('course_id') or ''),
            class_id=str(course.get('class_id') or ''),
        )
        stored = load(scope[1]) if load else None
        if stored:
            _hydrate_state(state, stored)
        _STATES[scope] = state
        changed = True
    if persist and state.persist is not persist:
        state.persist = persist
    account_id = str(account_id or '')
    if account_id and account_id not in state.members:
        state.members.add(account_id)
        state.anchor = choose_anchor(state.members)
        changed = True
    course_name = str(course.get('course_name') or state.course_name or '')[:200]
    class_name = str(course.get('class_name') or state.class_name or '')[:200]
    if course_name != state.course_name or class_name != state.class_name:
        state.course_name = course_name
        state.class_name = class_name
        changed = True
    if changed:
        _persist_state(state)
    return state


def class_scan_lock(course):
    scope = _loop_scope(course)
    return _LOCKS.setdefault(scope, asyncio.Lock())


def should_scan(
    state,
    interval_seconds,
    *,
    force=False,
    now=None,
    mode='normal',
    wall_now=None,
    explicit_schedule=False,
):
    if force:
        return True
    mode = mode if mode in SCAN_MODES else 'normal'
    current = float(now if now is not None else time.monotonic())
    if state.backoff_until > current:
        return False
    if state.activities is None:
        return True
    cache_seconds = activity_cache_seconds(
        wall_now or datetime.now(ZoneInfo('Asia/Shanghai')),
        mode,
        quiet_hours=not explicit_schedule,
    )
    if current - state.activities_at < cache_seconds:
        return False
    if mode == 'prewarm':
        return current - state.activities_at >= max(
            cache_seconds,
            min(max(3, int(interval_seconds or 60)), PREWARM_SCAN_SECONDS),
        )
    if mode == 'burst':
        return current - state.activities_at >= max(
            cache_seconds,
            min(max(3, int(interval_seconds or 60)), BURST_SCAN_SECONDS),
        )
    return current >= state.next_due_at


def cached_activities(state):
    return [dict(item) for item in state.activities or []]


def note_reuse(state):
    state.reuse_count += 1


def note_scan_success(state, account_id, activities, interval_seconds, *, now=None, mode='normal'):
    mode = mode if mode in SCAN_MODES else 'normal'
    current = float(now if now is not None else time.monotonic())
    state.activities = [dict(item) for item in activities or []]
    state.activities_at = current
    state.last_scan_at = current
    state.last_scan_wall_at = time.time()
    state.last_scan_by = str(account_id or '')
    state.activity_count = len(state.activities)
    state.scan_count += 1
    if state.anchor == state.last_scan_by:
        state.anchor_scans += 1
    else:
        state.fallback_scans += 1
    state.idle_streak = 0 if state.activities else min(4, state.idle_streak + 1)
    state.failure_streak = 0
    state.backoff_until = 0
    state.backoff_seconds = 0
    state.last_error = ''
    idle_factor = 1 if mode in {'prewarm', 'burst'} else min(
        MAX_IDLE_FACTOR,
        2 ** min(3, state.idle_streak),
    )
    interval = max(3, int(interval_seconds or 60))
    if mode == 'burst':
        interval = min(interval, BURST_SCAN_SECONDS)
    elif mode == 'prewarm':
        interval = min(interval, PREWARM_SCAN_SECONDS)
    state.next_due_at = current + interval * idle_factor
    _persist_state(state)
    return state


def note_scan_failure(state, interval_seconds, error, *, now=None, account_id=''):
    current = float(now if now is not None else time.monotonic())
    state.last_scan_at = current
    state.last_scan_wall_at = time.time()
    state.scan_count += 1
    state.failure_streak += 1
    state.last_error = str(error or '')[:240]
    if is_auth_error(error) and str(account_id or '') == state.anchor and state.failure_streak >= 2:
        if _switch_state_anchor(state, error):
            return state
    if is_throttle_error(error):
        index = min(state.failure_streak - 1, len(BACKOFF_SECONDS) - 1)
        state.backoff_seconds = BACKOFF_SECONDS[index]
        state.backoff_until = current + state.backoff_seconds
        state.next_due_at = state.backoff_until
    else:
        state.next_due_at = current + max(3, int(interval_seconds or 60))
    _persist_state(state)
    return state


def class_scan_summary(account_ids=None, *, now=None):
    selected = {str(account_id) for account_id in account_ids or [] if account_id}
    states = [
        state for state in _STATES.values()
        if not selected or state.members.intersection(selected)
    ]
    current = float(now if now is not None else time.monotonic())
    return {
        'classes': len(states),
        'anchored': sum(bool(state.anchor) for state in states),
        'cold_classes': sum(state.idle_streak > 0 for state in states),
        'cooling_classes': sum(state.backoff_until > current for state in states),
        'scans': sum(state.scan_count for state in states),
        'anchor_switches': sum(state.anchor_switches for state in states),
        'reused': sum(state.reuse_count for state in states),
        'anchor_scans': sum(state.anchor_scans for state in states),
        'fallback_scans': sum(state.fallback_scans for state in states),
    }


def reset_class_scans():
    _STATES.clear()
    _LOCKS.clear()
