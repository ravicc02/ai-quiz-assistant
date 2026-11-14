from __future__ import annotations

import asyncio
import math
import time


class AsyncTokenBucket:
    def __init__(
        self,
        rate_per_second,
        burst,
        *,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.rate = max(0.1, float(rate_per_second))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.updated_at = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    def _refill(self, now):
        elapsed = max(0, now - self.updated_at)
        if elapsed:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated_at = now

    async def acquire(self):
        while True:
            async with self._lock:
                now = self._clock()
                self._refill(now)
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate
            await self._sleep(delay)


class AttendanceRequestPacer:
    def __init__(
        self,
        *,
        scan_qps,
        sign_qps,
        bucket_factory=AsyncTokenBucket,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        scan_qps = max(1.0, float(scan_qps))
        sign_qps = max(1.0, float(sign_qps))
        self._buckets = {
            'scan': bucket_factory(
                scan_qps,
                max(2, math.ceil(scan_qps / 2)),
                clock=clock,
                sleep=sleep,
            ),
            'sign': bucket_factory(
                sign_qps,
                max(2, math.ceil(sign_qps / 2)),
                clock=clock,
                sleep=sleep,
            ),
        }

    async def acquire(self, kind):
        bucket = self._buckets.get(str(kind or ''))
        if bucket is None:
            raise ValueError('未知的签到请求类型')
        await bucket.acquire()
