import unittest

from app.services.attendance.pacing import AsyncTokenBucket, AttendanceRequestPacer


class AttendancePacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_bucket_waits_only_after_burst_capacity_is_used(self):
        current = [0.0]
        waits = []

        async def sleep(delay):
            waits.append(delay)
            current[0] += delay

        bucket = AsyncTokenBucket(
            2,
            2,
            clock=lambda: current[0],
            sleep=sleep,
        )
        await bucket.acquire()
        await bucket.acquire()
        self.assertEqual(waits, [])
        await bucket.acquire()
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 0.5)
        self.assertAlmostEqual(current[0], 0.5)

    async def test_pacer_keeps_scan_and_sign_rates_independent(self):
        created = {}

        class RecordingBucket:
            def __init__(self, rate, burst, **kwargs):
                self.rate = rate
                self.burst = burst
                self.calls = 0

            async def acquire(self):
                self.calls += 1

        def factory(rate, burst, **kwargs):
            bucket = RecordingBucket(rate, burst, **kwargs)
            created[(rate, burst)] = bucket
            return bucket

        pacer = AttendanceRequestPacer(
            scan_qps=12,
            sign_qps=10,
            bucket_factory=factory,
        )
        await pacer.acquire('scan')
        await pacer.acquire('sign')
        self.assertEqual(created[(12.0, 6)].calls, 1)
        self.assertEqual(created[(10.0, 5)].calls, 1)
        with self.assertRaises(ValueError):
            await pacer.acquire('unknown')
