<script setup>
import { computed, onMounted, ref } from 'vue';
import { Check, CircleAlert, LoaderCircle, Save, Server } from '@lucide/vue';
import { api } from '../../../app/api.js';

const baseUrl = ref('');
const secret = ref('');
const status = ref({});
const loading = ref(true);
const busy = ref('');
const message = ref('');
const error = ref('');
const dirty = computed(() => baseUrl.value.trim() !== (status.value.base_url || '') || Boolean(secret.value.trim()));

async function load() {
  loading.value = true; error.value = '';
  try {
    status.value = await api.attendanceRelay();
    baseUrl.value = status.value.base_url || '';
  } catch (cause) { error.value = cause.message; }
  finally { loading.value = false; }
}

function payload() {
  return { base_url: baseUrl.value.trim(), admin_secret: secret.value.trim() };
}

async function test() {
  busy.value = 'test'; error.value = ''; message.value = '';
  try {
    await api.testAttendanceRelay(payload());
    message.value = '连接成功';
  } catch (cause) { error.value = cause.message; }
  finally { busy.value = ''; }
}

async function save() {
  busy.value = 'save'; error.value = ''; message.value = '';
  try {
    status.value = { ...status.value, ...(await api.saveAttendanceRelay(payload())) };
    baseUrl.value = status.value.base_url || baseUrl.value.trim();
    secret.value = '';
    message.value = '域名配置已保存';
  } catch (cause) { error.value = cause.message; }
  finally { busy.value = ''; }
}

onMounted(load);
</script>

<template>
  <div class="attendance-domain-settings settings-section-stack">
    <section class="settings-card">
      <header class="settings-card-heading">
        <div class="settings-card-title">
          <h3>中继服务</h3>
        </div>
        <div class="settings-card-heading-actions">
          <span class="settings-state" :class="{ ready: status.configured }">{{ status.configured ? '已配置' : '未配置' }}</span>
        </div>
      </header>
      <div class="settings-card-body padded">
        <div v-if="loading" class="settings-loading">
          <LoaderCircle :size="18" class="spinning" />正在读取域名配置
        </div>
        <template v-else>
          <div class="attendance-form-grid">
            <label class="settings-field attendance-address-field">
              <span>中继服务地址</span>
              <input v-model="baseUrl" type="url" placeholder="https://sign.example.com" autocomplete="url" aria-label="中继服务地址">
            </label>
            <label class="settings-field">
              <span>管理密钥</span>
              <input v-model="secret" type="password" :placeholder="status.admin_secret_configured ? '已保存，留空保持不变' : '至少 24 位'" autocomplete="new-password" aria-label="管理密钥">
            </label>
          </div>
          <p v-if="error" class="attendance-feedback error" role="alert"><CircleAlert :size="14" />{{ error }}</p>
          <p v-if="message" class="attendance-feedback" role="status"><Check :size="14" />{{ message }}</p>
          <footer class="attendance-card-actions">
            <span>{{ status.admin_secret_configured ? '管理密钥已保存' : '首次使用需要填写服务端管理密钥' }}</span>
            <div>
              <button class="button light" type="button" :disabled="Boolean(busy) || !baseUrl.trim()" @click="test">
                <LoaderCircle v-if="busy === 'test'" :size="14" class="spinning" />
                <Server v-else :size="14" />测试连接
              </button>
              <button class="button dark" type="button" :disabled="Boolean(busy) || !dirty" @click="save">
                <LoaderCircle v-if="busy === 'save'" :size="14" class="spinning" />
                <Save v-else :size="14" />保存配置
              </button>
            </div>
          </footer>
        </template>
      </div>
    </section>

  </div>
</template>

<style scoped>
.attendance-domain-settings {
  width: 100%;
}

.attendance-form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(240px, .65fr);
  gap: 20px;
}

.attendance-feedback {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 14px;
  color: var(--green);
  font-size: 12px;
}

.attendance-feedback.error {
  color: var(--red);
}

.attendance-card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-top: 20px;
  border-top: 1px solid var(--line);
  padding-top: 16px;
}

.attendance-card-actions > span {
  color: var(--muted);
  font-size: 12px;
}

.attendance-card-actions > div {
  display: flex;
  gap: 8px;
}

@media (max-width: 760px) {
  .attendance-form-grid {
    grid-template-columns: 1fr;
  }

  .attendance-card-actions {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
