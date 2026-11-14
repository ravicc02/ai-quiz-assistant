from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
import logging
import time
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from ...db import utc_now

from .class_scan import (
    cached_activities,
    class_scan_lock,
    note_reuse,
    note_scan_failure,
    note_scan_success,
    register_membership,
    should_scan,
)
from .codebook import find_sign_code
from .coordinates import wgs84_to_bd09
from .contracts import AttendanceError, KINDS, classify_receipt, parse_courses, parse_activities, qr_credentials
from .availability import window

MOBILE = 'https://mobilelearn.chaoxing.com'
PAN = 'https://pan-yz.chaoxing.com'
logger = logging.getLogger('mumu.attendance')
_COURSE_CACHE = {}
COURSE_CACHE_SECONDS = 10 * 60


class AttendanceClient:
    def __init__(self, account, *, fast=False, codebook_get=None, codebook_put=None,
                 codebook_delete=None, request_guard=None, scan_interval_seconds=60,
                 scan_mode='normal', explicit_schedule=False,
                 class_scan_load=None, class_scan_persist=None,
                 code_attempt_get=None, code_attempt_note=None, code_attempt_clear=None):
        self.account = account
        self.fast = fast
        self.driver = self.context = None
        self.codebook_get = codebook_get
        self.codebook_put = codebook_put
        self.codebook_delete = codebook_delete
        self.request_guard = request_guard
        self.scan_interval_seconds = max(3, int(scan_interval_seconds or 60))
        self.scan_mode = scan_mode if scan_mode in {'normal','prewarm','burst'} else 'normal'
        self.explicit_schedule = bool(explicit_schedule)
        self.class_scan_load = class_scan_load
        self.class_scan_persist = class_scan_persist
        self.code_attempt_get = code_attempt_get
        self.code_attempt_note = code_attempt_note
        self.code_attempt_clear = code_attempt_clear
        self._local_code_attempts = {}

    async def charge_request(self, kind):
        if self.request_guard:
            pending = self.request_guard(kind)
            if inspect.isawaitable(pending):
                await pending

    async def __aenter__(self):
        if self.account['platform'] != 'chaoxing' or self.account['status'] != 'ready':
            raise AttendanceError('请先授权支持签到的学习通账号')
        state_path = Path(self.account['storage_state_path'])
        if not state_path.is_file(): raise AttendanceError('未找到已保存的登录态，请重新授权')
        try:
            saved_state = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError) as error:
            raise AttendanceError('学习通登录态格式无法确认，请重新授权') from error
        expected = str(self.account.get('platform_user_id') or '')
        ids = {
            cookie['value'] for cookie in saved_state.get('cookies', [])
            if cookie.get('name') == '_uid' and cookie.get('domain', '').lstrip('.').endswith('chaoxing.com')
        }
        if not expected or ids != {expected}:
            raise AttendanceError('学习通登录态或账号身份未通过验证，请重新授权')
        self.driver = await async_playwright().start()
        try:
            self.context = await self.driver.request.new_context(storage_state=self.account['storage_state_path'],
                user_agent='Mozilla/5.0', timeout=20000, max_redirects=0)
            if not self.fast:
                await self.charge_request('scan')
                response = await self.context.get('https://i.chaoxing.com/base?ws=1')
                state = await self.context.storage_state()
                ids = {cookie['value'] for cookie in state.get('cookies', []) if cookie['name'] == '_uid' and cookie.get('domain','').lstrip('.').endswith('chaoxing.com')}
                name = BeautifulSoup(await response.text(), 'html.parser').select_one('.user-name')
                if not response.ok or ids != {expected} or not name:
                    raise AttendanceError('学习通登录态或账号身份未通过验证，请重新授权')
            return self
        except BaseException:
            await self.__aexit__(None, None, None); raise

    async def __aexit__(self, *args):
        try:
            if self.context: await self.context.dispose()
        finally:
            if self.driver: await self.driver.stop()

    async def json(self, path, params=None, origin=MOBILE):
        sign_paths = (
            '/widget/sign/pcStuSignController/checkSignCode',
            '/widget/sign/pcTeaSignController/getSignCode',
            '/v2/apis/answer/saveAnswer',
        )
        await self.charge_request('sign' if any(path.startswith(item) for item in sign_paths) else 'scan')
        response = await self.context.get(origin + path, params=params)
        if response.status in {301,302,401,403}: raise AttendanceError('签到接口要求重新登录，请刷新授权')
        if not response.ok: raise AttendanceError(f'签到接口返回 HTTP {response.status}')
        try: return await response.json()
        except Exception as error: raise AttendanceError('签到接口返回格式无法确认') from error

    async def courses(self, force=False):
        loop_id = id(asyncio.get_running_loop())
        cache_key = (loop_id, str(self.account.get('id') or ''), str(self.account.get('platform_user_id') or ''))
        cached = _COURSE_CACHE.get(cache_key)
        if not force and cached and time.monotonic() - cached[0] < COURSE_CACHE_SECONDS:
            return [dict(course) for course in cached[1]]
        courses = parse_courses(await self.json('/mycourse/backclazzdata', {
            'view':'json','getTchClazzType':'1','mcode':'',
        }, 'https://mooc1-api.chaoxing.com'))
        _COURSE_CACHE[cache_key] = (time.monotonic(), courses)
        return [dict(course) for course in courses]

    async def class_activities(self, course, force=False, force_scan_token=None):
        register_membership(
            self.account.get('id'),
            course,
            load=self.class_scan_load,
            persist=self.class_scan_persist,
        )
        lock = class_scan_lock(course)
        async with lock:
            state = register_membership(self.account.get('id'), course)
            account_id = str(self.account.get('id') or '')
            if force and force_scan_token:
                force_scan = state.last_force_token != str(force_scan_token)
                if force_scan:
                    state.last_force_token = str(force_scan_token)
            else:
                force_scan = bool(
                    force
                    and (not state.anchor or state.anchor == account_id)
                )
            if not should_scan(
                state,
                self.scan_interval_seconds,
                force=force_scan,
                mode=self.scan_mode,
                explicit_schedule=self.explicit_schedule,
            ):
                note_reuse(state)
                logger.info('Attendance class scan reused', extra={
                    'diagnostic_code':'attendance.class_scan_reused',
                    'diagnostic_data':{
                        'class_id':course['class_id'],
                        'anchor':state.anchor,
                        'scanner':state.last_scan_by,
                        'idle_streak':state.idle_streak,
                        'cooling':state.backoff_until > 0,
                        'mode':self.scan_mode,
                    },
                })
                return cached_activities(state)
            try:
                payload = await self.json('/v2/apis/active/student/activelist', {
                    'courseId':course['course_id'],'classId':course['class_id'],'fid':'0',
                    'personId':self.account.get('platform_user_id') or '',
                    'uid':self.account.get('platform_user_id') or '',
                    'DB_STRATEGY':'RANDOM',
                })
                if isinstance(payload, dict) and payload.get('result') == 0:
                    message = str(payload.get('errorMsg') or payload.get('msg') or '活动列表读取失败')
                    if '频繁' in message or '稍后' in message:
                        raise AttendanceError('平台请求频繁，请稍后重试')
                candidates = parse_activities(payload, course)
                verified = []
                for candidate in candidates:
                    detail = await self.json('/newsign/signDetail',{
                        'activePrimaryId':candidate['upstream_id'],'type':'1',
                    })
                    detail = detail.get('data',detail) if isinstance(detail,dict) else None
                    if not isinstance(detail,dict): raise AttendanceError('未能核验签到详情，未更新该课程活动')
                    availability = window(candidate,detail)
                    if availability['status'] != 'open':
                        logger.info('Attendance activity excluded by window',extra={'diagnostic_code':'attendance.filtered','diagnostic_data':{
                            'class_id':course['class_id'],'upstream_id':candidate['upstream_id'],'reason':availability['status'],
                            'start_time':availability['start_time'],'end_time':availability['end_time'],
                            'is_over':detail.get('isOver'),'explicit_no_deadline':detail.get('setEndTimeNull')}})
                    if availability['status']=='unknown': raise AttendanceError('签到有效时段未能核实，未更新该课程活动')
                    if availability['status'] != 'open': continue
                    verified.append({
                        **candidate,
                        **{key:availability[key] for key in ('start_time','end_time','open_ended')},
                        'availability_status':'open','verified_at':utc_now(),
                        'kind':KINDS.get(str(detail.get('otherId')),candidate['kind']),
                    })
            except Exception as error:
                note_scan_failure(
                    state,
                    self.scan_interval_seconds,
                    error,
                    account_id=self.account.get('id'),
                )
                raise
            note_scan_success(
                state,
                self.account.get('id'),
                verified,
                self.scan_interval_seconds,
                mode=self.scan_mode,
            )
            return [dict(candidate) for candidate in verified]

    async def account_activities(self, course, candidates):
        result = []
        for verified in candidates:
            candidate = {**verified, 'kind':verified['kind']}
            attendance = await self.json('/v2/apis/sign/getAttendInfo',{'activeId':candidate['upstream_id'],'moreClassAttendEnc':''})
            if attendance.get('result') != 1 or not isinstance(attendance.get('data'),dict):
                raise AttendanceError('未能核验个人出勤，未更新该课程活动')
            if type(attendance['data'].get('status')) is not int or attendance['data']['status'] != 0:
                logger.info('Attendance activity excluded by personal state',extra={'diagnostic_code':'attendance.filtered','diagnostic_data':{
                    'account_id':self.account.get('id'),'upstream_id':candidate['upstream_id'],'reason':'personal_state','personal_status':attendance['data'].get('status')}})
                continue
            result.append(candidate)
        return result

    async def activities(self, course, force=False, force_scan_token=None):
        candidates = await self.class_activities(
            course,
            force=force,
            force_scan_token=force_scan_token,
        )
        return await self.account_activities(course, candidates)

    async def quick_activity(self, active_id):
        """Resolve a short-lived QR activity without scanning every course."""
        info = await self.json('/v2/apis/active/getPPTActiveInfo', {'activeId':active_id})
        if not isinstance(info,dict) or info.get('result') != 1:
            raise AttendanceError('未能读取二维码对应的活动详情')
        detail = info.get('data') if isinstance(info,dict) else None
        if not isinstance(detail,dict):
            raise AttendanceError('未能读取二维码对应的活动详情')
        kind = KINDS.get(str(detail.get('otherId')), 'unknown')
        if kind != 'qr':
            raise AttendanceError('二维码对应活动不是二维码签到')
        class_id = str(detail.get('clazzid') or detail.get('classId') or '')
        if not class_id:
            raise AttendanceError('二维码活动缺少班级标识')
        courses = await self.courses()
        course = next((item for item in courses if str(item['class_id']) == class_id), None)
        if not course:
            raise AttendanceError('当前账号课程列表中没有该二维码活动')
        availability = window(detail)
        if availability['status'] != 'open':
            raise AttendanceError('二维码活动不在有效签到时段')
        return {
            **course,
            'upstream_id':str(active_id),
            'title':str(detail.get('name') or '二维码签到')[:200],
            'kind':'qr',
            **{key:availability[key] for key in ('start_time','end_time','open_ended')},
            'availability_status':'open',
            'verified_at':utc_now(),
        }

    async def _read_sign_code(self, activity_id):
        """The activity payload carries the current code; gesture values stay encrypted."""
        payload = await self.json('/v2/apis/answer/saveAnswer', {
            'courseId': '0', 'classId': '0', 'activeId': activity_id,
        })
        code = find_sign_code(payload)
        if code:
            return code
        payload = await self.json('/widget/sign/pcTeaSignController/getSignCode', {
            'activeId': activity_id,
        })
        return find_sign_code(payload)

    async def _check_code(self, activity_id, code):
        account_id = str(self.account.get('id') or '')
        now = time.time()
        attempt = (
            self.code_attempt_get(account_id, activity_id)
            if self.code_attempt_get
            else self._local_code_attempts.get(str(activity_id), {'wrong_count':0,'locked_until':0})
        )
        if float(attempt.get('locked_until') or 0) > now or int(attempt.get('wrong_count') or 0) >= 3:
            return False, '本活动已连续错码 3 次（3/3），为避免账号被锁定，已停止校验，请约 1 小时后重试'

        def note_attempt(*, locked=False):
            nonlocal attempt
            if self.code_attempt_note and account_id:
                attempt = self.code_attempt_note(account_id, activity_id, locked=locked)
            else:
                wrong_count = 3 if locked else min(3, int(attempt.get('wrong_count') or 0) + 1)
                attempt = {
                    'wrong_count':wrong_count,
                    'locked_until':now + 3600 if locked or wrong_count >= 3 else 0,
                }
                self._local_code_attempts[str(activity_id)] = attempt
            return attempt

        checked = await self.json('/widget/sign/pcStuSignController/checkSignCode', {
            'activeId': activity_id, 'signCode': code,
        })
        result = checked.get('result') if isinstance(checked, dict) else None
        if result == 1:
            if self.code_attempt_clear and account_id:
                self.code_attempt_clear(account_id, activity_id)
            else:
                self._local_code_attempts.pop(str(activity_id), None)
            return True, ''
        message = str((checked or {}).get('errorMsg') or (checked or {}).get('msg') or '').strip() if isinstance(checked, dict) else ''
        if '频繁' in message:
            return False, '平台提示操作频繁，请稍后再试'
        if result == -1 or '次数' in message or '锁定' in message:
            note_attempt(locked=True)
            return False, '签到码校验次数过多，平台可能已临时锁定，请稍后再试'
        attempt = note_attempt()
        wrong_count = int(attempt.get('wrong_count') or 0)
        if wrong_count >= 3:
            return False, '本活动已连续错码 3 次（3/3），为避免账号被锁定，已停止校验，请约 1 小时后重试'
        return False, f'签到码或手势未通过平台校验（{wrong_count}/3），请核对后重试'

    async def resolve_sign_code(self, activity):
        activity_id = activity['upstream_id']
        cached = self.codebook_get(activity_id) if self.codebook_get else None
        if cached and cached.get('code'):
            # Codes enter the shared book only after platform verification.
            # Reuse them without a second check for every account.
            return {'code': cached['code'], 'source': cached.get('source') or 'cache'}
        code = await self._read_sign_code(activity_id)
        if not code:
            raise AttendanceError('当前活动没有返回签到码，请等待平台更新或手动填写')
        valid, message = await self._check_code(activity_id, code)
        if not valid:
            raise AttendanceError(message)
        if self.codebook_put:
            self.codebook_put(activity_id, code, 'activity', activity.get('kind'))
        return {'code': code, 'source': 'activity'}

    async def _prepare_qr_fast(self, activity, inputs):
        if not inputs.qr:
            return {'status':'awaiting_input','message':'请提供当前有效的签到二维码内容'}
        availability = window({
            key: activity.get(key)
            for key in ('start_time', 'end_time', 'open_ended', 'availability_status', 'verified_at')
        })
        if availability['status'] != 'open':
            return {'status':'expired' if availability['status']=='ended' else 'failed',
                    'message':'当前不在已核实的有效签到时段，未提交'}
        params = {
            'activeId':activity['upstream_id'],'courseId':activity['course_id'],
            'uid':str(self.account['platform_user_id']),'name':self.account['name'],
            'fid':'0','appType':'15','clientip':'','latitude':'-1','longitude':'-1',
            'vpProbability':'-1','vpStrategy':'',
        }
        try: params.update(qr_credentials(inputs.qr, activity['upstream_id']))
        except AttendanceError as error:
            return {'status':'awaiting_input','message':str(error)}
        return {
            'status':'ready','params':params,'kind':'qr',
            'availability':{
                **{key:availability[key] for key in ('start_time','end_time','open_ended')},
                'availability_status':'open','verified_at':utc_now(),
            },
        }

    async def upload_photo(self, path):
        try:
            payload = Path(path).read_bytes()
        except OSError as error:
            raise AttendanceError('照片素材无法读取，请重新配置') from error
        if not payload or len(payload) > 8 * 1024 * 1024:
            raise AttendanceError('照片素材不得超过 8 MB')
        mime = 'image/png' if payload.startswith(b'\x89PNG\r\n\x1a\n') else (
            'image/jpeg' if payload.startswith(b'\xff\xd8\xff') else
            'image/webp' if payload.startswith(b'RIFF') and payload[8:12] == b'WEBP' else ''
        )
        if not mime:
            raise AttendanceError('照片素材仅支持 PNG、JPEG 或 WebP')
        await self.charge_request('sign')
        token_response = await self.context.get(
            PAN + '/api/token/uservalid',
            params={'_from':'mobilelearn'},
        )
        if token_response.status in {301,302,401,403}:
            raise AttendanceError('照片上传凭证要求重新登录，请刷新授权')
        if not token_response.ok:
            raise AttendanceError('未能取得照片上传凭证')
        try:
            token_payload = await token_response.json()
        except Exception as error:
            raise AttendanceError('照片上传凭证格式无法确认') from error
        token = token_payload.get('_token') or token_payload.get('token') or ''
        if not token:
            raise AttendanceError('未能取得照片上传凭证')
        await self.charge_request('sign')
        extension = {'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}[mime]
        response = await self.context.post(
            PAN + '/upload',
            params={'_from':'mobilelearn','_token':token},
            multipart={
                'puid':str(self.account.get('platform_user_id') or ''),
                'fid':'0',
                'file':{'name':'sign'+extension,'mimeType':mime,'buffer':payload},
            },
        )
        if not response.ok:
            raise AttendanceError(f'照片上传返回 HTTP {response.status}')
        try:
            result = await response.json()
        except Exception as error:
            raise AttendanceError('照片上传返回格式无法确认') from error
        object_id = str(result.get('objectId') or '')
        if not object_id:
            raise AttendanceError(str(result.get('msg') or '照片上传失败')[:200])
        return object_id

    async def prepare(self, activity, inputs, *, fast=False):
        if fast and activity.get('kind') == 'qr':
            return await self._prepare_qr_fast(activity, inputs)
        current = await self.activities(activity)
        matched = next((item for item in current if item['upstream_id']==activity['upstream_id']),None)
        if matched is None:
            receipt = await self.json('/v2/apis/sign/getAttendInfo', {'activeId':activity['upstream_id'],'moreClassAttendEnc':''})
            if receipt.get('result') != 1 or not isinstance(receipt.get('data'),dict):
                raise AttendanceError('活动已结束，但个人出勤记录尚未核实，请在平台核对')
            if type(receipt['data'].get('status')) is int and receipt['data']['status']==1:
                return {'status':'already_signed','message':'活动已结束，平台确认该账号已签到'}
            return {'status':'expired','message':'该活动已不在当前账号的进行中签到列表'}
        info = await self.json('/v2/apis/active/getPPTActiveInfo', {'activeId':activity['upstream_id']})
        attendance = await self.json('/v2/apis/sign/getAttendInfo', {'activeId':activity['upstream_id'],'moreClassAttendEnc':''})
        if info.get('result') != 1 or not isinstance(info.get('data'),dict) or attendance.get('result') != 1 or not isinstance(attendance.get('data'),dict):
            raise AttendanceError('活动详情或个人签到状态未通过核验，未提交')
        status = attendance['data'].get('status')
        if type(status) is int and status == 1: return {'status':'already_signed','message':'平台显示该账号已签到'}
        if status == 11: return {'status':'expired','message':'平台显示签到已过期'}
        if type(status) is not int or status != 0: return {'status':'manual_required','message':'该账号出勤状态需人工核对，未覆盖原记录'}
        detail = info['data']
        availability = window(matched,detail)
        if availability['status'] != 'open':
            return {'status':'expired' if availability['status']=='ended' else 'failed','message':'当前不在已核实的有效签到时段，未提交'}
        kind = KINDS.get(str(detail.get('otherId')), 'unknown')
        photo_required = kind == 'photo' or any(
            detail.get(key) in (1, '1', True)
            for key in ('ifphoto', 'ifPhoto')
        )
        face_required = detail.get('openCheckFaceFlag') in (1, '1', True)
        live_photo_required = detail.get('openPreventCheatFlag') in (1, '1', True)
        location_flag = detail.get('openLocationFlag') in (1, '1', True)
        located = bool(detail.get('locationText')) and str(detail.get('locationRange') or 0) not in {'0','0.0','-1'}
        address_required = any(detail.get(key) in (1, '1', True) for key in ('ifopenAddress','ifOpenAddress'))
        if kind == 'unknown':
            return {'status':'manual_required','message':'平台签到类型无法识别，请在学习通中核对后处理'}
        if face_required:
            return {'status':'manual_required','message':'该签到要求人脸校验，请在学习通中完成'}
        if photo_required and live_photo_required:
            return {
                'status':'manual_required',
                'message':'该签到要求学习通 App 现场拍摄凭证（不是人脸校验），普通照片或相册图片无法通过，请在学习通 App 内完成',
            }
        if kind != 'location' and (location_flag or located or address_required):
            return {
                'status':'manual_required',
                'message':'该签到要求位置校验，请在自动签到分组中勾选“位置”并配置位置素材',
            }
        if photo_required:
            photo = self.account.get('attendance_photo_path')
            if not photo:
                return {'status':'awaiting_input','message':'该签到要求拍照校验，请先为该账号配置拍照签到素材'}
        params = {'activeId':activity['upstream_id'],'courseId':activity['course_id'],'uid':str(self.account['platform_user_id']),
                  'name':self.account['name'],'fid':'0','appType':'15','clientip':'','latitude':'-1','longitude':'-1',
                  'vpProbability':'-1','vpStrategy':''}
        if kind == 'location':
            if inputs.location is None:
                return {'status':'awaiting_input','message':'请提供并确认本次签到的实际位置，再继续执行'}
            longitude, latitude = wgs84_to_bd09(
                inputs.location.longitude,
                inputs.location.latitude,
            )
            latitude = round(latitude, 6)
            longitude = round(longitude, 6)
            address = inputs.location.address
            location_payload = {
                'result':1,
                'latitude':latitude,
                'longitude':longitude,
                'address':address,
            }
            location_result = {
                **location_payload,
                'mockData':json.dumps(
                    {'strategy':0,'probability':-1},
                    separators=(',', ':'),
                ),
            }
            params.update(
                latitude=str(latitude),
                longitude=str(longitude),
                address=address,
                ifTiJiao='1',
                location=json.dumps(location_payload, ensure_ascii=False, separators=(',', ':')),
                locationResult=json.dumps(location_result, ensure_ascii=False, separators=(',', ':')),
            )
        if kind in {'code','gesture'}:
            code = inputs.code
            code_source = 'manual'
            if not code:
                try:
                    resolved = await self.resolve_sign_code(activity)
                except AttendanceError as error:
                    return {
                        'status':'failed' if '额度' in str(error) else 'awaiting_input',
                        'message':str(error),
                    }
                code = resolved['code']
                code_source = resolved['source']
            else:
                valid, message = await self._check_code(activity['upstream_id'], code)
                if not valid:
                    return {'status':'awaiting_input','message':message}
                if self.codebook_put:
                    self.codebook_put(activity['upstream_id'], code, 'manual', kind)
            params['signCode'] = code
        if kind == 'qr':
            if not inputs.qr: return {'status':'awaiting_input','message':'请提供当前有效的签到二维码内容'}
            try: params.update(qr_credentials(inputs.qr, activity['upstream_id']))
            except AttendanceError as error: return {'status':'awaiting_input','message':str(error)}
        if photo_required:
            # A normal activity can still require a photo gate. Face material is
            # kept for human identity checks and is never submitted as a photo.
            photo = self.account.get('attendance_photo_path')
            try:
                params['objectId'] = await self.upload_photo(photo)
            except AttendanceError as error:
                return {'status':'failed','message':str(error)}
            params['ifPhoto'] = '1'; params['useragent'] = ''
        prepared = {'status':'ready','params':params,'kind':kind,'availability':{**{key:availability[key] for key in ('start_time','end_time','open_ended')},'availability_status':'open','verified_at':utc_now()}}
        if kind in {'code','gesture'}:
            prepared['code_source'] = code_source
        return prepared

    async def submit(self, prepared):
        available = window(prepared.get('availability'))
        if available['status'] != 'open':
            return {'status':'expired' if available['status']=='ended' else 'failed','message':'签到时段已结束或尚未核实，未发出请求'}
        await self.charge_request('sign')
        if window(prepared.get('availability'))['status'] != 'open':
            return {'status':'expired','message':'签到时段已结束，未发出请求'}
        response = await self.context.post(MOBILE + '/pptSign/stuSignajax', form=prepared['params'], max_retries=0)
        if not response.ok: return {'status':'unknown','message':f'提交后收到 HTTP {response.status}，请先核对平台记录'}
        text = await response.text()
        status, message = classify_receipt(text)
        if (
            status == 'awaiting_input'
            and prepared.get('kind') in {'code','gesture'}
            and '签到码' in message
            and self.codebook_delete
        ):
            self.codebook_delete(prepared.get('params', {}).get('activeId', ''))
        return {'status':status,'message':message,'receipt_kind':'platform_response'}
