export const LAUNCH_MINIMUM_MS = 900;

export function launchProgressForState(state) {
  if (!state) return 12;
  if (state.error || state.status === "error") return 100;
  if (state.backendReady || state.status === "ready") return 92;
  if (state.status === "starting") return 68;
  if (state.status === "installing") return 38;
  const items = Array.isArray(state.items) ? state.items : [];
  if (!items.length) return state.status === "checking" ? 22 : 12;
  const ready = items.filter(item => item.status === "ready").length;
  return 22 + Math.round((ready / items.length) * 34);
}

export function launchMessageForState(state) {
  if (!state) return "正在检查浏览器与运行环境";
  if (state.error || state.status === "error") return "本地服务启动失败";
  if (state.backendReady || state.status === "ready") return "正在载入工作区";
  if (state.status === "starting") return "正在启动本地服务";
  if (state.status === "installing") return "正在准备运行库";
  return "正在检查浏览器与自动化组件";
}

export function launchRequiresSetup(state) {
  return state?.status === "blocked";
}
