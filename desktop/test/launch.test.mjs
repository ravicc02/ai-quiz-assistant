import { test } from "node:test";
import assert from "node:assert/strict";
import { launchMessageForState, launchProgressForState, launchRequiresSetup } from "../src/app/lib/launch.js";

test("launch progress follows dependency, service and workspace phases", () => {
  assert.equal(launchProgressForState(null), 12);
  assert.equal(launchProgressForState({ status: "checking", items: [] }), 22);
  assert.equal(launchProgressForState({ status: "checking", items: [{ status: "ready" }, { status: "checking" }] }), 39);
  assert.equal(launchProgressForState({ status: "starting", items: [] }), 68);
  assert.equal(launchProgressForState({ status: "ready", backendReady: true, items: [] }), 92);
  assert.equal(launchProgressForState({ status: "error", error: "failed", items: [] }), 100);
});

test("blocked environment opens setup and errors stay on the launch screen", () => {
  assert.equal(launchRequiresSetup({ status: "blocked", items: [] }), true);
  assert.equal(launchRequiresSetup({ status: "ready", backendReady: true, items: [] }), false);
  assert.equal(launchMessageForState({ status: "installing", items: [] }), "正在准备运行库");
  assert.equal(launchMessageForState({ status: "error", error: "failed", items: [] }), "本地服务启动失败");
});
