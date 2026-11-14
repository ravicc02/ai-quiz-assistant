<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { BookOpen, Check, ClipboardList, Cpu, FolderOpen, Globe2, HardDrive, LoaderCircle, RotateCcw, Save, Settings2 } from "@lucide/vue";
import { api } from "../../../app/api.js";
import { accountSchedule, restorePreferenceDraft, scheduleError } from "../lib/preferences.js";
import ModelConfiguration from "./ModelConfiguration.vue";
import AttendanceDomainSettings from "./AttendanceDomainSettings.vue";
import PlatformLogo from "../../platforms/components/PlatformLogo.vue";
import { accountsForBusiness } from "../../platforms/lib/catalog.js";

const props = defineProps({ accounts: { type: Array, default: () => [] }, info: Object, section: { type: String, default: "model" }, embedded: Boolean });
const desktopAvailable = Boolean(window.desktopApi);
const emit = defineEmits(["saved", "model-saved", "update:section", "open-data", "open-logs", "platforms"]);
const tabs = [{ id: "model", label: "模型配置", icon: Cpu }, { id: "assignments", label: "作业配置", icon: ClipboardList }, { id: "learning", label: "网课配置", icon: BookOpen }, { id: "attendance", label: "签到域名", icon: Globe2 }, { id: "system", label: "系统设置", icon: Settings2 }];
const current = computed(() => tabs.find(item => item.id === props.section) || tabs[0]);
const preferences = ref(null);
const schedules = ref([]);
const originals = ref({});
const loading = ref(true);
const loadError = ref("");
const pending = ref("");
const messages = ref({});
const errors = ref({});
const draftKey = "studio:settings-draft:v1";
const accountsById = computed(() => new Map(props.accounts.map(account => [account.id, account])));
const supportedAccounts = computed(() => accountsForBusiness(props.accounts, "assignments"));
function snapshot(section) { return JSON.stringify({ value: preferences.value?.[section], ...(section === "assignments" ? { schedules: schedules.value } : {}) }); }
function dirty(section) { return Boolean(preferences.value && Object.hasOwn(originals.value, section) && originals.value[section] !== snapshot(section)); }
const validation = computed(() => {
  if (!preferences.value) return "";
  if (current.value.id === "assignments") {
    for (const schedule of schedules.value) {
      const issue = scheduleError(schedule);
      if (issue) return accountsById.value.get(schedule.account_id)?.name + "：" + issue;
    }
  }
  return "";
});
function cache() {
  if (loading.value || !preferences.value) return;
  const draft = {};
  for (const section of ["assignments", "learning", "system"]) {
    if (dirty(section)) draft[section] = preferences.value[section];
  }
  if (draft.assignments) draft.schedules = schedules.value;
  try { Object.keys(draft).length ? localStorage.setItem(draftKey, JSON.stringify(draft)) : localStorage.removeItem(draftKey); } catch { /* Storage may be unavailable. */ }
}
watch([preferences, schedules], cache, { deep: true });
onMounted(load);
async function load() {
  loading.value = true; loadError.value = "";
  try {
    const result = await api.preferences();
    result.assignments.interactive = true;
    preferences.value = result;
    schedules.value = supportedAccounts.value.map(accountSchedule);
    for (const section of ["assignments", "learning", "system"]) originals.value[section] = snapshot(section);
    let cached;
    try { cached = JSON.parse(localStorage.getItem(draftKey) || "null"); } catch { cached = null; }
    const restored = restorePreferenceDraft(result, schedules.value, cached);
    preferences.value = restored.preferences; preferences.value.assignments.interactive = true; schedules.value = restored.schedules;
  } catch (error) { loadError.value = error.message; }
  finally { loading.value = false; }
}
async function save() {
  const section = current.value.id;
  if (pending.value || validation.value) return;
  pending.value = section; messages.value[section] = ""; errors.value[section] = "";
  try {
    const result = await api.savePreferences({ [section]: preferences.value[section], ...(section === "assignments" ? { schedules: schedules.value } : {}) });
    originals.value[section] = snapshot(section);
    messages.value[section] = "已保存";
    cache(); emit("saved", { preferences: result, section });
  } catch (error) { errors.value[section] = error.message; }
  finally { pending.value = ""; }
}
function reset() {
  const section = current.value.id;
  const original = JSON.parse(originals.value[section]);
  preferences.value[section] = original.value;
  if (section === "assignments") schedules.value = original.schedules;
  messages.value[section] = ""; errors.value[section] = ""; cache();
}
function disableSchedule(schedule) { if (!schedule.enabled) schedule.auto_execute = false; }
function scheduleState(account) {
  if (account.automation?.pending) return account.automation.pending + " 个待执行 · " + (account.automation.reason || "等待调度");
  if (account.automation?.failed) return account.automation.last_error || "自动任务创建失败";
  if (account.last_collection_status === "running") return "正在检测";
  if (account.status !== "ready") return "等待授权";
  if (account.auto_execute_enabled && !account.automation_baseline_ready) return "下次检测建立基线";
  if (!account.auto_collect_enabled) return "检测已关闭";
  const last = new Date(account.last_collection_at).getTime();
  if (!Number.isFinite(last)) return "等待首次检测";
  const next = new Date(last + account.auto_collect_interval_seconds * 1000);
  return account.last_collection_status === "error" ? "上次检测失败" : next.getTime() <= Date.now() ? "等待空闲后检测" : "下次 " + next.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
</script>

<template>
  <main class="settings-workspace" :class="{ 'settings-embedded': embedded }">
    <aside v-if="!embedded" class="preferences-nav">
      <h2>设置</h2>
      <nav aria-label="设置分类">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          type="button"
          :aria-label="tab.label"
          :aria-current="current.id === tab.id ? 'page' : undefined"
          :class="{ active: current.id === tab.id }"
          @click="emit('update:section', tab.id)"
        >
          <component :is="tab.icon" :size="16" />
          <span>{{ tab.label }}</span>
          <span v-if="dirty(tab.id)" class="unsaved-dot" title="未保存" aria-hidden="true" />
        </button>
      </nav>
    </aside>

    <div class="preferences-content">
      <header v-if="!embedded" class="preferences-title">
        <component :is="current.icon" :size="20" />
        <h2>{{ current.label }}</h2>
      </header>

      <div v-show="current.id === 'model'" class="preferences-body settings-section-stack">
        <ModelConfiguration @saved="emit('model-saved')" />
      </div>
      <div v-if="current.id === 'attendance'" class="preferences-body settings-section-stack">
        <AttendanceDomainSettings />
      </div>
      <Transition v-else name="preference-switch" mode="out-in">
        <div v-if="current.id !== 'model'" :key="current.id" class="preferences-body settings-section-stack">
          <div v-if="loading" class="settings-loading" role="status">
            <LoaderCircle class="spinning" :size="18" />正在读取配置
          </div>
          <div v-else-if="loadError" class="settings-load-error" role="alert">
            {{ loadError }}
            <button class="button light" type="button" @click="load"><RotateCcw :size="14" />重试</button>
          </div>
          <form v-else-if="preferences" @submit.prevent="save" :aria-busy="Boolean(pending)">
            <fieldset :disabled="Boolean(pending)">
              <template v-if="current.id === 'assignments'">
                <section class="settings-card">
                  <header class="settings-card-heading">
                    <span class="settings-card-index">01</span>
                    <div class="settings-card-title">
                      <h3>执行策略</h3>
                      <p>控制提交、评测和单次任务的执行边界</p>
                    </div>
                  </header>
                  <div class="settings-card-body">
                    <label class="preference-row">
                      <span>
                        <strong>学习通最终提交</strong>
                        <small>{{ preferences.assignments.auto_submit ? '填写、暂存后提交，以平台核验结果为准。' : '填写并暂存，转入待审核。' }}</small>
                      </span>
                      <input v-model="preferences.assignments.auto_submit" class="switch-input" type="checkbox" role="switch" aria-label="允许最终提交">
                    </label>
                    <label v-if="Object.hasOwn(preferences.assignments, 'educoder_auto_evaluate')" class="preference-row">
                      <span>
                        <strong>头歌自动评测</strong>
                        <small>保存后自动评测；明确失败时修正代码并再次评测，通过后进入下一关。</small>
                      </span>
                      <input v-model="preferences.assignments.educoder_auto_evaluate" class="switch-input" type="checkbox" role="switch" aria-label="头歌自动评测">
                    </label>
                    <div class="settings-control-grid">
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>Agent 最大步骤</strong>
                          <small>单个作业允许执行的最大步骤数</small>
                        </span>
                        <span class="settings-number-wrap">
                          <input v-model.number="preferences.assignments.max_steps" class="settings-number" type="number" required min="10" max="500" step="1">
                          <span>步</span>
                        </span>
                      </label>
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>自动执行并发</strong>
                          <small>同时运行的自动作业任务数量</small>
                        </span>
                        <span class="settings-number-wrap">
                          <input v-model.number="preferences.assignments.concurrency" class="settings-number" type="number" required min="1" max="8" step="1">
                          <span>个任务</span>
                        </span>
                      </label>
                    </div>
                  </div>
                </section>

                <section class="settings-card">
                  <header class="settings-card-heading">
                    <span class="settings-card-index">02</span>
                    <div class="settings-card-title">
                      <h3>自动检测与执行</h3>
                      <p>仅处理新发现的可提交作业，已有作业和失败任务不会自动重跑</p>
                    </div>
                    <div class="settings-card-heading-actions">
                      <span class="settings-scope">{{ schedules.length }} 个账号</span>
                    </div>
                  </header>
                  <div class="settings-card-body">
                    <div v-if="!schedules.length" class="settings-empty">
                      <span>暂无支持作业检测的账号</span>
                      <button class="button light" type="button" @click="emit('platforms')">添加账号</button>
                    </div>
                    <div v-else class="automation-list">
                      <article v-for="schedule in schedules" :key="schedule.account_id" class="automation-account">
                        <header class="automation-account-header">
                          <div class="automation-account-name">
                            <PlatformLogo :platform="accountsById.get(schedule.account_id)?.platform" :size="30" />
                            <div>
                              <strong>{{ accountsById.get(schedule.account_id)?.name }}</strong>
                              <span class="automation-account-state" :class="{ 'validation-error': scheduleError(schedule) }">
                                {{ scheduleError(schedule) || scheduleState(accountsById.get(schedule.account_id) || {}) }}
                              </span>
                            </div>
                          </div>
                          <span v-if="schedule.auto_execute" class="automation-enabled">发现后自动执行</span>
                        </header>
                        <div class="automation-controls">
                          <label class="automation-control">
                            <span>自动检测</span>
                            <input
                              v-model="schedule.enabled"
                              class="switch-input"
                              type="checkbox"
                              role="switch"
                              :aria-label="accountsById.get(schedule.account_id)?.name + ' 自动检测'"
                              :disabled="accountsById.get(schedule.account_id)?.status !== 'ready' && !schedule.enabled"
                              @change="disableSchedule(schedule)"
                            >
                          </label>
                          <div class="automation-control">
                            <span>检测间隔</span>
                            <div class="interval-control">
                              <input
                                v-model.number="schedule.interval_value"
                                type="number"
                                min="1"
                                max="604800"
                                step="any"
                                required
                                :aria-label="accountsById.get(schedule.account_id)?.name + ' 检测间隔'"
                              >
                              <select v-model="schedule.interval_unit" :aria-label="accountsById.get(schedule.account_id)?.name + ' 间隔单位'">
                                <option value="s">秒</option>
                                <option value="min">分</option>
                                <option value="h">时</option>
                              </select>
                            </div>
                          </div>
                          <label class="automation-control">
                            <span>自动完成</span>
                            <input
                              v-model="schedule.auto_execute"
                              class="switch-input"
                              type="checkbox"
                              role="switch"
                              :aria-label="accountsById.get(schedule.account_id)?.name + ' 自动完成'"
                              :disabled="!schedule.enabled || (accountsById.get(schedule.account_id)?.status !== 'ready' && !schedule.auto_execute)"
                            >
                          </label>
                        </div>
                      </article>
                    </div>
                  </div>
                </section>
              </template>

              <template v-else-if="current.id === 'learning'">
                <section class="settings-card">
                  <header class="settings-card-heading">
                    <span class="settings-card-index">01</span>
                    <div class="settings-card-title">
                      <h3>浏览器与播放</h3>
                      <p>控制任务浏览器窗口、章节答题和课程播放参数</p>
                    </div>
                    <div class="settings-card-heading-actions">
                      <span class="settings-scope">新任务生效</span>
                    </div>
                  </header>
                  <div class="settings-card-body">
                    <label class="preference-row">
                      <span>
                        <strong>后台运行</strong>
                        <small>{{ preferences.learning.headless ? '不弹出窗口，通过软件内实时画面操控。' : '显示独立浏览器窗口，关闭窗口会中断任务。' }}</small>
                      </span>
                      <input v-model="preferences.learning.headless" class="switch-input" type="checkbox" role="switch" aria-label="网课后台运行">
                    </label>
                    <label class="preference-row">
                      <span>
                        <strong>学习通章节题目自动提交</strong>
                        <small>{{ preferences.learning.auto_submit_quiz ? '使用作业答题体系填写并提交，平台确认章节完成后继续。' : '填写并核验后等待人工提交，不自动交卷。' }}</small>
                      </span>
                      <input v-model="preferences.learning.auto_submit_quiz" class="switch-input" type="checkbox" role="switch" aria-label="学习通章节题目自动提交">
                    </label>
                    <div class="settings-control-grid">
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>播放倍速</strong>
                          <small>新任务开始播放时使用</small>
                        </span>
                        <select v-model.number="preferences.learning.playback_rate" class="settings-select" aria-label="播放倍速">
                          <option v-for="rate in [0.5, 0.75, 1, 1.25, 1.5, 2]" :key="rate" :value="rate">{{ rate }} ×</option>
                        </select>
                      </label>
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>音量</strong>
                          <small>0% 到 100%</small>
                        </span>
                        <span class="settings-range-wrap">
                          <input v-model.number="preferences.learning.volume" type="range" min="0" max="100" step="1" aria-label="音量">
                          <output>{{ preferences.learning.volume }}%</output>
                        </span>
                      </label>
                    </div>
                  </div>
                </section>

                <section class="settings-card">
                  <header class="settings-card-heading">
                    <span class="settings-card-index">02</span>
                    <div class="settings-card-title">
                      <h3>等待与核验</h3>
                      <p>资源加载和平台完成状态需要留出足够的确认时间</p>
                    </div>
                  </header>
                  <div class="settings-card-body">
                    <div class="settings-control-grid three">
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>资源加载等待</strong>
                          <small>控件未出现时的等待时间</small>
                        </span>
                        <span class="settings-number-wrap">
                          <input v-model.number="preferences.learning.resource_wait_seconds" class="settings-number" type="number" required min="5" max="120" step="1">
                          <span>秒</span>
                        </span>
                      </label>
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>平台完成确认</strong>
                          <small>提交后等待进度回写的上限</small>
                        </span>
                        <span class="settings-number-wrap">
                          <input v-model.number="preferences.learning.confirmation_wait_seconds" class="settings-number" type="number" required min="10" max="180" step="1">
                          <span>秒</span>
                        </span>
                      </label>
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>播放无进展判定</strong>
                          <small>连续停滞多久后处理播放器</small>
                        </span>
                        <span class="settings-number-wrap">
                          <input v-model.number="preferences.learning.stall_seconds" class="settings-number" type="number" required min="30" max="300" step="1">
                          <span>秒</span>
                        </span>
                      </label>
                    </div>
                  </div>
                </section>
              </template>

              <template v-else-if="current.id === 'system'">
                <section class="settings-card">
                  <header class="settings-card-heading">
                    <span class="settings-card-index">01</span>
                    <div class="settings-card-title">
                      <h3>调度</h3>
                      <p>限制同时进行的账号检测数量</p>
                    </div>
                  </header>
                  <div class="settings-card-body">
                    <label class="preference-row">
                      <span>
                        <strong>自动检测并发</strong>
                        <small>同时检测的账号数量，避免平台请求过于集中。</small>
                      </span>
                      <span class="settings-number-wrap">
                        <input v-model.number="preferences.system.collection_concurrency" class="settings-number" type="number" required min="1" max="8" step="1">
                        <span>个账号</span>
                      </span>
                    </label>
                  </div>
                </section>

                <section class="settings-card">
                  <header class="settings-card-heading">
                    <span class="settings-card-index">02</span>
                    <div class="settings-card-title">
                      <h3>现场记录</h3>
                      <p>控制任务截图和诊断记录的保留范围</p>
                    </div>
                  </header>
                  <div class="settings-card-body">
                    <div class="settings-control-grid">
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>保留时间</strong>
                          <small>超过期限的现场记录会被清理</small>
                        </span>
                        <span class="settings-number-wrap">
                          <input v-model.number="preferences.system.log_retention_days" class="settings-number" type="number" required min="1" max="365" step="1">
                          <span>天</span>
                        </span>
                      </label>
                      <label class="settings-control-cell">
                        <span class="settings-control-copy">
                          <strong>空间上限</strong>
                          <small>现场记录占用的最大空间</small>
                        </span>
                        <span class="settings-number-wrap">
                          <input v-model.number="preferences.system.log_size_mb" class="settings-number" type="number" required min="20" max="4096" step="1">
                          <span>MiB</span>
                        </span>
                      </label>
                    </div>
                    <p class="settings-note">仅清理任务现场记录；主日志仍按自然日归档。</p>
                  </div>
                </section>

                <section class="settings-card">
                  <header class="settings-card-heading">
                    <span class="settings-card-index">03</span>
                    <div class="settings-card-title">
                      <h3>本地存储</h3>
                      <p>账号、任务和日志保存在当前 Windows 用户目录</p>
                    </div>
                    <div class="settings-card-heading-actions">
                      <span class="settings-scope"><HardDrive :size="13" />本机</span>
                    </div>
                  </header>
                  <div class="settings-card-body">
                    <div class="settings-path-list">
                      <div class="settings-path-row">
                        <span class="settings-path-copy">
                          <strong>数据目录</strong>
                          <code>{{ info?.data_root }}</code>
                        </span>
                        <button class="button light compact" type="button" @click="emit('open-data')">
                          <FolderOpen :size="14" />{{ desktopAvailable ? '打开' : '复制路径' }}
                        </button>
                      </div>
                      <div class="settings-path-row">
                        <span class="settings-path-copy">
                          <strong>今日日志</strong>
                          <code>{{ info?.main_log_path || (info?.logs_root ? info.logs_root + '/main.log' : '') }}</code>
                        </span>
                        <button class="button light compact" type="button" @click="emit('open-logs')">
                          <FolderOpen :size="14" />打开日志目录
                        </button>
                      </div>
                    </div>
                  </div>
                </section>
              </template>
            </fieldset>
            <footer class="settings-action-bar preferences-save-bar">
              <span class="preferences-feedback" :class="{ error: validation || errors[current.id] }" role="status">
                <span v-if="validation || errors[current.id] || dirty(current.id)" class="settings-feedback-dot" :class="{ dirty: dirty(current.id), error: validation || errors[current.id] }" aria-hidden="true" />
                <Check v-if="messages[current.id] && !dirty(current.id)" :size="14" />
                {{ validation || errors[current.id] || (dirty(current.id) ? '有未保存的更改' : messages[current.id]) }}
              </span>
              <button class="button light" type="button" :disabled="!dirty(current.id) || Boolean(pending)" @click="reset">
                <RotateCcw :size="14" />撤销
              </button>
              <button class="button dark" type="submit" :disabled="!dirty(current.id) || Boolean(validation) || Boolean(pending)">
                <LoaderCircle v-if="pending === current.id" class="spinning" :size="14" />
                <Save v-else :size="14" />保存配置
              </button>
            </footer>
          </form>
        </div>
      </Transition>
    </div>
  </main>
</template>
