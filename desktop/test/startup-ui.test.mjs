import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const read = path => readFile(new URL(path, import.meta.url), "utf8");

test("startup uses a Mumu-only loading screen before the workspace", async () => {
  const [app, launch] = await Promise.all([
    read("../src/app/App.vue"),
    read("../src/features/brand/components/LaunchExperience.vue"),
  ]);
  assert.match(app, /<LaunchExperience\s+v-if="!launchComplete"/);
  assert.match(app, /<div v-show="launchComplete" class="app-shell"/);
  assert.match(app, /<h2>总控中心/);
  assert.match(launch, /<WaterMascot mood="working"/);
  assert.match(launch, /role="progressbar"/);
  assert.doesNotMatch(launch, /BrandOrbit/);
});

test("first-run setup is full screen and describes browser automation setup", async () => {
  const [startup, model] = await Promise.all([
    read("../src/features/brand/components/StartupExperience.vue"),
    read("../src/features/settings/components/ModelConfiguration.vue"),
  ]);
  assert.match(startup, /准备浏览器环境/);
  assert.match(startup, /Playwright 与 Browser Use/);
  assert.doesNotMatch(startup, /BrandOrbit/);
  assert.doesNotMatch(startup, /startup-wordmark|startup-mini-mascot/);
  assert.match(startup, /width: 100%; height: 100dvh/);
  assert.match(model, /placeholder="your-provider"/);
  assert.match(model, /placeholder="https:\/\/your-provider\.example\/v1"/);
  assert.match(model, /placeholder="your-model-name"/);
  assert.match(model, /class="reasoning-options"/);
});

test("learning workspace uses a searchable account picker", async () => {
  const source = await read("../src/features/learning/components/LearningWorkspace.vue");
  assert.match(source, /<LearningAccountPicker/);
  assert.doesNotMatch(source, /<select :value="accountId" aria-label="网课账号"/);
});
