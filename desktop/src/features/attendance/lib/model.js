export const attendanceKinds = { normal: '普通', photo: '拍照', qr: '二维码', code: '签到码', gesture: '手势码', location: '位置', unknown: '待核验' };
export const attendanceMaterialLabels = {
  normal: '无需素材',
  photo: '使用账号照片素材',
  gesture: '自动读取手势码',
  code: '自动读取签到码',
  location: '使用位置签到素材',
};
export const attendanceStates = { pending: '待执行', queued: '排队中', running: '核验中', submitting: '提交中', succeeded: '已签到', already_signed: '已签到', awaiting_input: '待补充', failed: '失败', auth_required: '待授权', expired: '已结束', manual_required: '需人工处理', unknown: '待核对', cancelled: '已取消' };
export const attendanceRunning = batch => Boolean(batch?.items.some(item => ['queued', 'running', 'submitting'].includes(item.status)));
export function attendanceBatchDisplayStatus(batch) {
  if (!batch) return '';
  if (attendanceRunning(batch)) return 'running';
  const items = batch.items || [];
  for (const status of ['awaiting_input', 'manual_required', 'auth_required', 'unknown', 'failed', 'expired']) {
    if (items.some(item => item.status === status)) return status;
  }
  if (items.length && items.every(item => ['succeeded', 'already_signed'].includes(item.status))) {
    return 'succeeded';
  }
  return batch.status || 'unknown';
}
export function attendanceScanFeedback(batch, error = '') {
  if (error) return { kind: 'error', message: `检测完成：${error}` };
  if (!batch) return { kind: 'success', message: '检测完成，未发现可签活动' };
  const items = batch.items || [];
  const count = statuses => items.filter(item => statuses.includes(item.status)).length;
  const pending = count(['pending', 'awaiting_input']);
  const unknown = count(['unknown']);
  const manual = count(['manual_required']);
  const auth = count(['auth_required']);
  const failed = count(['failed']);
  const completed = count(['succeeded', 'already_signed']);
  if (unknown) return { kind: 'error', message: `检测完成：${unknown} 个账号需要先核对结果，未重复提交` };
  if (pending) return { kind: 'error', message: `检测完成：${pending} 个账号待补充素材或输入` };
  if (manual) return { kind: 'error', message: `检测完成：${manual} 个账号需人工处理` };
  if (auth) return { kind: 'error', message: `检测完成：${auth} 个账号需要重新授权` };
  if (failed) return { kind: 'error', message: `检测完成：${failed} 个账号签到失败` };
  if (completed) return { kind: 'success', message: `检测完成：${completed} 个账号已签到` };
  if (attendanceRunning(batch)) return { kind: 'success', message: '检测完成，已加入签到队列' };
  return { kind: 'success', message: '检测完成，未发现可签活动' };
}
export const attendanceRunnable = batch => Boolean(batch && !attendanceRunning(batch) && !batch.items.some(item => item.status === 'unknown') && batch.items.some(item => ['pending', 'awaiting_input', 'failed', 'auth_required', 'cancelled'].includes(item.status)));
const attendanceActivePollMs = 750;
const attendanceIdlePollMs = 8000;
const attendanceConfigurationPollMs = 60000;
export const attendancePollDelay = active => active ? attendanceActivePollMs : attendanceIdlePollMs;
export const attendanceConfigurationDue = (loadedAt, now = Date.now()) => (
  !loadedAt || now - loadedAt >= attendanceConfigurationPollMs
);
const attendanceAutoPrewarmSeconds = 60;
const attendanceAutoBurstSeconds = 3 * 60;
const attendanceAutoPrewarmInterval = 20;
const attendanceAutoBurstInterval = 5;
const attendanceWeekSeconds = 7 * 24 * 3600;
const attendanceClock = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Shanghai',
  weekday: 'short',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});
const attendanceWeekdays = { Sun: 7, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

export function attendanceAutoPhase(group, now = new Date()) {
  const current = now instanceof Date ? now : new Date(now);
  if (Number.isNaN(current.getTime())) return 'outside';
  const values = Object.fromEntries(
    attendanceClock.formatToParts(current).map(part => [part.type, part.value]),
  );
  const weekday = attendanceWeekdays[values.weekday] || 7;
  const hour = Number(values.hour) || 0;
  const minute = Number(values.minute) || 0;
  const second = Number(values.second) || 0;
  const windows = group?.windows || [];
  if (!windows.length) return hour >= 7 && hour < 22 ? 'normal' : 'outside';
  const currentWeek = (weekday - 1) * 86400 + hour * 3600 + minute * 60 + second;
  let prewarm = null;
  let activeNormal = false;
  for (const window of windows) {
    const [startHour, startMinute] = String(window.start || '').split(':').map(Number);
    const [endHour, endMinute] = String(window.end || '').split(':').map(Number);
    if (![startHour, startMinute, endHour, endMinute].every(Number.isFinite)) continue;
    const startMinuteOfDay = startHour * 60 + startMinute;
    const endMinuteOfDay = endHour * 60 + endMinute;
    const duration = ((endMinuteOfDay - startMinuteOfDay + 1440) % 1440) * 60;
    for (const day of window.days || []) {
      const start = ((Number(day) - 1) * 86400 + startMinuteOfDay * 60) % attendanceWeekSeconds;
      const elapsed = (currentWeek - start + attendanceWeekSeconds) % attendanceWeekSeconds;
      if (elapsed < duration) {
        if (elapsed < attendanceAutoBurstSeconds) return 'burst';
        activeNormal = true;
        continue;
      }
      const toStart = (start - currentWeek + attendanceWeekSeconds) % attendanceWeekSeconds;
      if (toStart > 0 && toStart <= attendanceAutoPrewarmSeconds) {
        prewarm = prewarm == null ? toStart : Math.min(prewarm, toStart);
      }
    }
  }
  return prewarm == null ? (activeNormal ? 'normal' : 'outside') : 'prewarm';
}

export function attendanceAutoEffectiveInterval(group, now = new Date()) {
  const base = Math.max(3, Number(group?.interval_seconds) || 30);
  const phase = attendanceAutoPhase(group, now);
  if (phase === 'burst') return Math.min(base, attendanceAutoBurstInterval);
  if (phase === 'prewarm') return Math.min(base, attendanceAutoPrewarmInterval);
  return base;
}

export function attendanceWindowOpen(item, now = Date.now()) {
  if (item.availability_status !== 'open' || !item.verified_at) return false;
  const start = Number(item.start_time);
  if (!Number.isFinite(start) || start <= 0 || start * 1000 > now) return false;
  if (item.end_time == null) return item.open_ended === true || item.open_ended === 1;
  const end = Number(item.end_time);
  return Number.isFinite(end) && end > start && end * 1000 > now;
}

export function attendanceWindowLabel(item) {
  const formatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const start = Number(item.start_time), end = Number(item.end_time);
  if (!Number.isFinite(start) || start <= 0) return '';
  return formatter.format(new Date(start * 1000)) + ' 至 ' + (item.end_time == null ? '平台未设截止' : Number.isFinite(end) ? formatter.format(new Date(end * 1000)) : '时间待核实');
}

export function attendanceLocation(latitude, longitude, address, source = 'manual') {
  if (latitude === '' && longitude === '' && !address.trim()) return null;
  const lat = Number(latitude), lon = Number(longitude);
  if (latitude === '' || longitude === '' || !Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
    throw new Error('请填写有效的纬度（-90 至 90）和经度（-180 至 180）');
  }
  if (!address.trim() || address.trim().length > 200) throw new Error('请填写不超过 200 字的实际位置名称');
  return { latitude: lat, longitude: lon, address: address.trim(), source };
}

export function attendanceCourseFilterSelection(filters, courses, preserveMissing = false) {
  const selected = Array.isArray(filters) ? filters : [];
  if (preserveMissing) return [...selected];
  const available = new Set(
    (courses || []).map(course => typeof course === 'string' ? course : course?.name).filter(Boolean),
  );
  return selected.filter(name => available.has(name));
}

export async function readAttendanceQr(file) {
  if (!file || file.size > 4 * 1024 * 1024) throw new Error('请选择不超过 4 MB 的二维码图片');
  const bitmap = await createImageBitmap(file);
  try {
    const scale = Math.min(1, 1800 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas'); canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext('2d', { willReadFrequently: true });
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const image = context.getImageData(0, 0, canvas.width, canvas.height);
    const { default: decodeQrCode } = await import('./qr-decoder.js');
    const code = decodeQrCode(image.data, image.width, image.height);
    if (!code?.data) throw new Error('图片中未识别到二维码');
    return code.data;
  } finally { bitmap.close(); }
}
