import "../styles/style.css";

const DEFAULT_API_BASE_URL = "http://localhost:8000/api/v1";

function normalizeAndValidateBaseUrl(raw: string): { valid: boolean; normalized: string } {
  const value = raw.trim();
  const withoutTrailingSlash = value.replace(/\/+$/, "");
  const validAbsolute = /^https?:\/\/.+\/api\/v1$/i.test(withoutTrailingSlash);
  const validRelative = withoutTrailingSlash === "/api/v1";
  return {
    valid: validAbsolute || validRelative,
    normalized: withoutTrailingSlash,
  };
}

function emitStartupNotice(message: string): void {
  const statusLog = document.getElementById("status-log");
  if (statusLog) {
    const line = `[${new Date().toLocaleTimeString()}] ${message}`;
    statusLog.textContent = `${line}\n${statusLog.textContent || ""}`;
  }
  const statusShort = document.getElementById("status-short");
  if (statusShort) {
    statusShort.textContent = message;
  }
}

// 轻量初始化：恢复 Base URL 与 range 显示
const baseUrlEl = document.getElementById("api-base-url") as HTMLInputElement | null;
const timeoutEl = document.getElementById("api-timeout-ms") as HTMLInputElement | null;
try {
  const saved = localStorage.getItem("cdt.apiBaseUrl");
  if (baseUrlEl) {
    const initialValue = saved || baseUrlEl.value || DEFAULT_API_BASE_URL;
    const checked = normalizeAndValidateBaseUrl(initialValue);
    if (!checked.valid) {
      baseUrlEl.value = DEFAULT_API_BASE_URL;
      localStorage.removeItem("cdt.apiBaseUrl");
      emitStartupNotice(`⚠️ 检测到无效后端地址：${initialValue}，已回退默认值 ${DEFAULT_API_BASE_URL}`);
    } else {
      baseUrlEl.value = checked.normalized;
    }
  }
  const savedTimeoutMs = localStorage.getItem("cdt.apiTimeoutMs");
  if (savedTimeoutMs && timeoutEl) timeoutEl.value = savedTimeoutMs;
} catch {
  // ignore storage errors in private mode
}

function bindRangeText(rangeId: string, textId: string, fmt?: (value: number) => string) {
  const r = document.getElementById(rangeId) as HTMLInputElement | null;
  const t = document.getElementById(textId);
  if (!r || !t) return;
  const update = () => {
    const v = Number(r.value);
    t.textContent = fmt ? fmt(v) : String(v);
  };
  r.addEventListener("input", update);
  update();
}

bindRangeText("mask-brush-size", "mask-brush-size-text");
bindRangeText("mask-brush-hardness", "mask-brush-hardness-text", (v) => v.toFixed(2));

import("./app")
  .then(({ bootstrapApp }) => bootstrapApp())
  .catch((err) => {
    console.error(err);
    const msgRaw = String((err as Error)?.message || err || "未知错误");
    const missingId = msgRaw.match(/#([\w-]+)/)?.[1];
    const detail = missingId
      ? `初始化失败：缺少 DOM 元素 #${missingId}（可能是页面模板未更新或缓存未刷新）`
      : `初始化失败：${msgRaw}`;

    const logEl = document.getElementById("status-log");
    if (logEl) {
      const stack = String((err as Error)?.stack || err || "");
      logEl.textContent = `${detail}\n${stack}`.trim();
    }

    const shortEl = document.getElementById("status-short");
    if (shortEl) {
      shortEl.textContent = detail;
    }
  });
