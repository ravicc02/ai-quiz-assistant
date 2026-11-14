import { test } from "node:test";
import assert from "node:assert/strict";
import { filterLearningAccounts } from "../src/features/learning/lib/accounts.js";

const accounts = [
  { id: "1", name: "刘典睿", platform: "chaoxing", status: "ready" },
  { id: "2", name: "测试账号", platform: "icourse", status: "ready" },
  { id: "3", name: "未授权账号", platform: "chaoxing", status: "waiting" },
];

test("learning account picker searches name, platform and authorization state", () => {
  assert.equal(filterLearningAccounts(accounts, "").length, 3);
  assert.deepEqual(filterLearningAccounts(accounts, "典睿").map(item => item.id), ["1"]);
  assert.deepEqual(filterLearningAccounts(accounts, "mooc", name => name === "icourse" ? "中国大学 MOOC" : "学习通").map(item => item.id), ["2"]);
  assert.deepEqual(filterLearningAccounts(accounts, "待授权").map(item => item.id), ["3"]);
});
