import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  parseWindowsLocation,
  readWindowsLocation,
} = require("../electron/windows-location.cjs");

test("Windows location payload is parsed and bounds checked", () => {
  assert.deepEqual(parseWindowsLocation('{"latitude":30.5,"longitude":114.3,"accuracy":20}'), {
    latitude: 30.5,
    longitude: 114.3,
    accuracy: 20,
    source: "windows",
  });
  assert.throws(() => parseWindowsLocation('{"latitude":91,"longitude":114.3,"accuracy":20}'));
  assert.throws(() => parseWindowsLocation("not-json"));
});

test("Windows location adapter uses the fixed system command and returns coordinates", async () => {
  let invocation;
  const result = await readWindowsLocation({
    execute(executable, args, options, callback) {
      invocation = { executable, args, options };
      callback(null, JSON.stringify({ latitude: 21.1, longitude: 110.2, accuracy: 40 }));
    },
  });
  assert.match(invocation.executable, /WindowsPowerShell[\\/]v1\.0[\\/]powershell\.exe$/i);
  assert.ok(invocation.args.includes("-NoProfile"));
  assert.equal(invocation.options.windowsHide, true);
  assert.equal(result.latitude, 21.1);
  assert.equal(result.longitude, 110.2);
});
