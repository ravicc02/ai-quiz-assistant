import { test } from "node:test";
import assert from "node:assert/strict";

import {
  normalizeLocationReading,
  readCurrentAttendanceLocation,
} from "../src/features/attendance/lib/location.js";

function browserGeolocation(responses) {
  let call = 0;
  return {
    calls: [],
    getCurrentPosition(success, failure, options) {
      this.calls.push(options);
      const response = responses[call++];
      if (response.error) failure(response.error);
      else success(response);
    },
  };
}

test("native Windows location is preferred without asking the browser provider", async () => {
  const geolocation = browserGeolocation([]);
  const result = await readCurrentAttendanceLocation({
    desktopApi: {
      getWindowsLocation: async () => ({ latitude: 30.5, longitude: 114.3, accuracy: 25 }),
    },
    geolocation,
  });
  assert.deepEqual(result, {
    latitude: 30.5,
    longitude: 114.3,
    accuracy: 25,
    source: "browser",
  });
  assert.equal(geolocation.calls.length, 0);
});

test("browser location falls back to network accuracy when precise location fails", async () => {
  const geolocation = browserGeolocation([
    { error: { code: 2 } },
    { coords: { latitude: 30.6, longitude: 114.4, accuracy: 800 } },
  ]);
  const result = await readCurrentAttendanceLocation({
    desktopApi: { getWindowsLocation: async () => { throw new Error("native unavailable"); } },
    geolocation,
  });
  assert.equal(result.latitude, 30.6);
  assert.equal(result.accuracy, 800);
  assert.equal(geolocation.calls.length, 2);
});

test("location readings reject invalid coordinates and preserve permission errors", async () => {
  assert.throws(() => normalizeLocationReading({ latitude: 91, longitude: 0 }));
  const geolocation = browserGeolocation([{ error: { code: 1 } }]);
  await assert.rejects(
    readCurrentAttendanceLocation({ desktopApi: null, geolocation }),
    error => error.code === 1 && /权限/.test(error.message),
  );
  assert.equal(geolocation.calls.length, 1);
});
