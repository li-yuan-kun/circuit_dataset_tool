import { ApiClient, ApiError, type ApiRequestEvent } from "./backend_client";
import { CanvasEngine, type NodeRenderMode } from "./canvas_engine";
import { MaskLayer } from "./make_layer";
import { computeLabelLocalApprox } from "./modules/label_local";
import type { Label, Scene } from "./modules/types";
import {
  exportCanvasPNG,
  exportCompositePNG,
  makeLabelJson,
  makeSceneJson,
  packZip,
  suggestSampleId,
} from "./utils/export";

type EditorMode = "circuit" | "mask";
type LabelComputeMode = "frontend_fast" | "backend_precise";

type AppState = {
  mode: EditorMode;
  labelComputeMode: LabelComputeMode;
  scene: Scene;
  label: Label | null;
  maskBlob: Blob | null;
};

type LabelComputeTrigger = "manual" | "auto";

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

function byId<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`缺少 DOM 元素: #${id}`);
  return el as T;
}

function maybeById<T extends HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

function bindOptionalClick(id: string, handler: () => void, log?: (msg: string) => void): void {
  const el = maybeById<HTMLButtonElement>(id);
  if (!el) {
    log?.(`⚠️ 可选按钮缺失，已跳过绑定：#${id}`);
    return;
  }
  el.addEventListener("click", handler);
}

function toNum(value: string, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function getPinsFromVocab(vocab: any, type: string): string[] {
  const pins = vocab?.types?.[type]?.pins;
  if (!pins) return [];
  if (Array.isArray(pins)) {
    return pins
      .map((it) => String(it?.name ?? it?.id ?? it?.pin ?? ""))
      .filter(Boolean);
  }
  if (typeof pins === "object") {
    return Object.keys(pins);
  }
  return [];
}

function downloadBlob(name: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function userFacingError(err: unknown, fallback: string): string {
  if (err instanceof ApiError && err.code === "REQUEST_TIMEOUT") {
    const timeoutMs = Number(err.details?.timeoutMs);
    const seconds = Number.isFinite(timeoutMs) ? (timeoutMs / 1000).toFixed(0) : "30";
    return `请求超时（${seconds}s），请检查后端负载或适当增大超时后重试`;
  }
  if (err instanceof Error && err.name === "AbortError") {
    return "请求超时，请检查后端负载或适当增大超时后重试";
  }
  if (err instanceof Error) return `${fallback}：${err.message}`;
  return fallback;
}

function readApiTimeoutMs(): number {
  const timeoutEl = byId<HTMLInputElement>("api-timeout-ms");
  const parsed = Number(timeoutEl.value);
  const timeoutMs = Number.isFinite(parsed) ? Math.max(1000, Math.round(parsed)) : 30000;
  timeoutEl.value = String(timeoutMs);
  try {
    localStorage.setItem("cdt.apiTimeoutMs", String(timeoutMs));
  } catch {
    // ignore
  }
  return timeoutMs;
}

function isTimeoutError(err: unknown): boolean {
  if (err instanceof ApiError && err.code === "REQUEST_TIMEOUT") return true;
  return err instanceof Error && err.name === "AbortError";
}

function sleepMs(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function bootstrapApp(): Promise<void> {
  // 必需元素：缺失时直接抛错，避免进入半初始化状态
  const statusLog = byId<HTMLPreElement>("status-log");
  const labelJsonEl = byId<HTMLTextAreaElement>("label-json");
  const labelSummaryEl = byId<HTMLDivElement>("label-summary");
  const circuitCanvas = byId<HTMLCanvasElement>("circuit-canvas");
  const uiCanvas = byId<HTMLCanvasElement>("ui-canvas");
  const maskCanvas = byId<HTMLCanvasElement>("mask-canvas");
  const interactionStatusEl = byId<HTMLSpanElement>("interaction-layer-status");
  const statusShortEl = byId<HTMLDivElement>("status-short");
  const requestSummaryEl = byId<HTMLDivElement>("request-summary");
  const shuffleModeStatusEl = byId<HTMLDivElement>("shuffle-mode-status");
  const labelAutoComputeEl = byId<HTMLInputElement>("label-auto-compute");

  const circuitCtx = circuitCanvas.getContext("2d");
  const maskCtx = maskCanvas.getContext("2d");
  if (!circuitCtx || !maskCtx) throw new Error("无法初始化 Canvas 2D 上下文");

  const log = (msg: string): void => {
    const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
    statusLog.textContent = `${line}\n${statusLog.textContent || ""}`;
  };

  const updateRequestSummary = (endpoint: string, elapsedMs: number, status: string): void => {
    const summary = `最近请求：${endpoint} | ${Math.round(elapsedMs)}ms | ${status}`;
    requestSummaryEl.textContent = summary;
    statusShortEl.textContent = summary;
  };

  let relativeBaseUrlHintLogged = false;

  const isTimeoutOrAbortError = (err: unknown): boolean => {
    if (!(err instanceof Error)) return false;
    const msg = err.message.toLowerCase();
    return (
      err.name === "AbortError" ||
      msg.includes("aborted") ||
      msg.includes("abort") ||
      msg.includes("timeout") ||
      msg.includes("signal is aborted without reason")
    );
  };

  const resolution = { w: circuitCanvas.width, h: circuitCanvas.height };
  const badgeResolutionEl = byId<HTMLSpanElement>("badge-resolution");
  const canvasWidthEl = byId<HTMLInputElement>("canvas-width");
  const canvasHeightEl = byId<HTMLInputElement>("canvas-height");
  const applyCanvasSizeBtn = byId<HTMLButtonElement>("btn-apply-canvas-size");
  const vocab = await loadVocab(log);
  const engine = new CanvasEngine({ resolution, vocab });
  const maskLayer = new MaskLayer({ resolution });

  const applyCanvasResolution = (w: number, h: number): void => {
    const width = Math.max(128, Math.min(4096, Math.floor(Number(w) || resolution.w)));
    const height = Math.max(128, Math.min(4096, Math.floor(Number(h) || resolution.h)));
    circuitCanvas.width = width;
    circuitCanvas.height = height;
    uiCanvas.width = width;
    uiCanvas.height = height;
    maskCanvas.width = width;
    maskCanvas.height = height;

    resolution.w = width;
    resolution.h = height;
    engine.setResolution({ w: width, h: height });
    maskLayer.resize({ w: width, h: height });
    badgeResolutionEl.textContent = `${width}×${height}`;
    canvasWidthEl.value = String(width);
    canvasHeightEl.value = String(height);
  };

  applyCanvasResolution(resolution.w, resolution.h);
  applyCanvasSizeBtn.addEventListener("click", () => {
    applyCanvasResolution(Number(canvasWidthEl.value), Number(canvasHeightEl.value));
    render();
    log(`已应用画布尺寸：${resolution.w}×${resolution.h}`);
  });

  const state: AppState = {
    mode: "circuit",
    labelComputeMode: "frontend_fast",
    scene: engine.serializeScene(),
    label: null,
    maskBlob: null,
  };

  const logError = (err: unknown, hint: string): void => {
    const message = userFacingError(err, hint);
    log(`❌ ${message}`);
    console.error(err);
  };

  const getApi = (): ApiClient => {
    const baseUrlEl = byId<HTMLInputElement>("api-base-url");
    const checked = normalizeAndValidateBaseUrl(baseUrlEl.value || "");
    let baseUrl = checked.normalized;
    if (!checked.valid) {
      log(`⚠️ 后端地址不合法：${baseUrlEl.value}。已回退默认值 ${DEFAULT_API_BASE_URL}`);
      baseUrl = DEFAULT_API_BASE_URL;
    }
    baseUrlEl.value = baseUrl;

    if (baseUrl.startsWith("/api/v1") && !relativeBaseUrlHintLogged) {
      log("ℹ️ 当前 baseUrl 为相对路径（/api/v1），请求依赖前端代理/同源反代。");
      relativeBaseUrlHintLogged = true;
    }

    const timeoutMs = readApiTimeoutMs();
    try {
      localStorage.setItem("cdt.apiBaseUrl", baseUrl);
    } catch {
      // ignore
    }
    return new ApiClient({
      baseUrl,
      timeoutMs,
      onRequestEvent: (event: ApiRequestEvent) => {
        if (event.phase === "start") {
          const msg = `请求发起：${event.method} ${event.url} (timeout=${event.timeoutMs}ms)`;
          updateRequestSummary(`${event.method} ${event.url}`, 0, "PENDING");
          log(`🌐 ${msg}`);
          return;
        }

        if (event.phase === "end") {
          updateRequestSummary(`${event.method} ${event.url}`, event.elapsedMs, `HTTP ${event.status}`);
          return;
        }

        const diagnose =
          event.errorType === "TypeError"
            ? "未连通（网络/地址不可达）"
            : event.errorType === "AbortError"
              ? "后端慢或阻塞（超时未返回）"
              : "后端业务错误";
        updateRequestSummary(`${event.method} ${event.url}`, event.elapsedMs, `${event.errorType} / ${diagnose}`);
        log(`⚠️ 请求异常：${event.errorType} @ ${event.url}（${diagnose}）`);
      },
    });
  };

  const syncScene = (): Scene => {
    const scene = engine.serializeScene();
    state.scene = scene;
    return scene;
  };

  const render = (): void => {
    engine.draw(circuitCtx);
    maskCtx.clearRect(0, 0, resolution.w, resolution.h);
    maskLayer.drawOverlay(maskCtx, 0.45);
  };

  const applyNodeRenderSettings = (): void => {
    const mode = byId<HTMLSelectElement>("node-render-mode").value as NodeRenderMode;
    const strokeScale = toNum(byId<HTMLInputElement>("node-stroke-scale").value, 1);
    const netStrokeScale = toNum(byId<HTMLInputElement>("net-stroke-scale").value, 1);
    const showLabel = byId<HTMLInputElement>("node-show-type").checked;
    engine.setNodeRenderOptions({ mode, strokeScale, netStrokeScale, showTypeLabelOnSymbol: showLabel });

    try {
      localStorage.setItem("cdt.nodeRenderMode", mode);
      localStorage.setItem("cdt.nodeStrokeScale", String(strokeScale));
      localStorage.setItem("cdt.nodeShowType", String(showLabel));
      localStorage.setItem("cdt.netStrokeScale", String(netStrokeScale));
    } catch {
      // ignore
    }
  };

  const initNodeRenderSettings = (): void => {
    const modeEl = byId<HTMLSelectElement>("node-render-mode");
    const strokeEl = byId<HTMLInputElement>("node-stroke-scale");
    const strokeText = byId<HTMLSpanElement>("node-stroke-scale-text");
    const showTypeEl = byId<HTMLInputElement>("node-show-type");
    const netStrokeEl = byId<HTMLInputElement>("net-stroke-scale");
    const netStrokeText = byId<HTMLSpanElement>("net-stroke-scale-text");

    try {
      const mode = localStorage.getItem("cdt.nodeRenderMode");
      if (mode === "symbol" || mode === "box") modeEl.value = mode;
      const stroke = Number(localStorage.getItem("cdt.nodeStrokeScale"));
      if (Number.isFinite(stroke)) strokeEl.value = String(Math.max(0.5, Math.min(3, stroke)));
      const showType = localStorage.getItem("cdt.nodeShowType");
      if (showType === "true" || showType === "false") showTypeEl.checked = showType === "true";
      const netStroke = Number(localStorage.getItem("cdt.netStrokeScale"));
      if (Number.isFinite(netStroke)) netStrokeEl.value = String(Math.max(0.5, Math.min(4, netStroke)));
    } catch {
      // ignore
    }

    strokeText.textContent = Number(strokeEl.value).toFixed(1);
    netStrokeText.textContent = Number(netStrokeEl.value).toFixed(1);
    applyNodeRenderSettings();
    modeEl.addEventListener("change", () => {
      applyNodeRenderSettings();
      render();
      log(`器件样式已切换为：${modeEl.value === "box" ? "方框" : "电路符号"}`);
    });
    strokeEl.addEventListener("input", () => {
      strokeText.textContent = Number(strokeEl.value).toFixed(1);
      applyNodeRenderSettings();
      render();
    });
    showTypeEl.addEventListener("change", () => {
      applyNodeRenderSettings();
      render();
    });
    netStrokeEl.addEventListener("input", () => {
      netStrokeText.textContent = Number(netStrokeEl.value).toFixed(1);
      applyNodeRenderSettings();
      render();
    });
  };

  const initCustomSymbolUpload = (refreshPalette: () => void): void => {
    const typeEl = byId<HTMLSelectElement>("custom-symbol-type");
    const statusEl = byId<HTMLDivElement>("custom-symbol-status");
    const fileEl = byId<HTMLInputElement>("file-custom-symbol");

    const types = Object.keys(vocab?.types ?? {}).sort();
    typeEl.innerHTML = "";
    for (const t of types) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      typeEl.appendChild(opt);
    }

    const ensureTypeInVocab = (type: string): void => {
      const key = String(type || "").trim();
      if (!key) return;
      if (!vocab.types || typeof vocab.types !== "object") vocab.types = {};
      if (vocab.types[key]) return;
      vocab.types[key] = {
        category: "custom",
        display_name: key,
        size: { w: 110, h: 70 },
        pins: {
          p0: { x: -55, y: 0 },
          p1: { x: 55, y: 0 },
        },
      };
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = key;
      typeEl.appendChild(opt);
      refreshPalette();
      log(`已新增器件类型：${key}`);
    };

    const refreshStatus = () => {
      const t = typeEl.value;
      statusEl.textContent = engine.hasCustomSymbol(t)
        ? `已为 ${t} 配置自定义符号（优先于内置图形）`
        : `当前 ${t} 使用内置符号`;
    };

    const readFileAsDataUrl = (file: File): Promise<string> =>
      new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(reader.error || new Error("读取文件失败"));
        reader.readAsDataURL(file);
      });

    bindOptionalClick("btn-upload-custom-symbol", () => fileEl.click(), log);
    bindOptionalClick("btn-clear-custom-symbol", () => {
      const t = typeEl.value;
      ensureTypeInVocab(t);
      engine.clearCustomSymbol(t);
      refreshStatus();
      render();
      log(`已移除 ${t} 的自定义符号`);
    }, log);

    fileEl.addEventListener("change", async () => {
      const file = fileEl.files?.[0];
      if (!file) return;
      const t = typeEl.value;
      try {
        ensureTypeInVocab(t);
        const src = await readFileAsDataUrl(file);
        const img = await new Promise<HTMLImageElement>((resolve, reject) => {
          const image = new Image();
          image.onload = () => resolve(image);
          image.onerror = () => reject(new Error("图片解码失败"));
          image.src = src;
        });
        engine.setCustomSymbol(t, img);
        refreshStatus();
        render();
        log(`已为 ${t} 应用自定义符号：${file.name}`);
      } catch (err) {
        logError(err, `上传 ${t} 自定义符号失败`);
      } finally {
        fileEl.value = "";
      }
    });

    typeEl.addEventListener("change", refreshStatus);
    refreshStatus();

    initDrawSymbolPad({
      engine,
      vocab,
      log,
      refreshPalette,
      onApplied: (type) => {
        const exists = Array.from(typeEl.options).some((o) => o.value === type);
        if (!exists) {
          const opt = document.createElement("option");
          opt.value = type;
          opt.textContent = type;
          typeEl.appendChild(opt);
        }
        typeEl.value = type;
        refreshStatus();
        render();
      },
    });
  };

  const syncInteractionCanvas = (): void => {
    const isCircuitMode = state.mode === "circuit";
    uiCanvas.style.pointerEvents = "auto";
    maskCanvas.style.pointerEvents = "none";
    uiCanvas.style.cursor = isCircuitMode ? "crosshair" : "cell";
    maskCanvas.style.cursor = "default";
  };

  const refreshLabelUi = (): void => {
    if (!state.label) {
      labelJsonEl.value = "";
      labelSummaryEl.innerHTML = '<div class="muted">尚未生成 label。</div>';
      return;
    }
    labelJsonEl.value = JSON.stringify(state.label, null, 2);
    labelSummaryEl.innerHTML = `<div>function: ${state.label.function}</div><div>occ_threshold: ${state.label.occ_threshold}</div><div>occlusion 条目: ${state.label.occlusion?.length || 0}</div>`;
  };

  const updateShuffleModeStatus = (useBackend: boolean): void => {
    shuffleModeStatusEl.textContent = useBackend
      ? "Shuffle 模式：当前使用后端 shuffle"
      : "Shuffle 模式：当前使用本地 shuffle";
  };

  const loadAutoComputePreference = (): boolean => {
    try {
      const stored = localStorage.getItem("cdt.labelAutoCompute");
      return stored !== "false";
    } catch {
      return true;
    }
  };

  labelAutoComputeEl.checked = loadAutoComputePreference();
  labelAutoComputeEl.addEventListener("change", () => {
    const enabled = labelAutoComputeEl.checked;
    try {
      localStorage.setItem("cdt.labelAutoCompute", String(enabled));
    } catch {
      // ignore
    }
    log(enabled ? "已开启自动计算 Label" : "已关闭自动计算 Label（可手动点击“计算 Label”）");
  });

  let autoComputeTimer: number | null = null;
  let queuedAutoComputeReason: string | null = null;
  let autoComputeRunning = false;

  const computeLabelAndRefresh = async (reason: string, trigger: LabelComputeTrigger = "manual"): Promise<void> => {
    const selectedMode = byId<HTMLSelectElement>("label-compute-mode").value as LabelComputeMode;
    state.labelComputeMode = selectedMode === "backend_precise" ? "backend_precise" : "frontend_fast";

    const endpoint = "/label/compute";
    const startedAt = performance.now();
    const functionValue = byId<HTMLInputElement>("function-custom").value.trim() || byId<HTMLSelectElement>("function-select").value;
    const occThreshold = toNum(byId<HTMLInputElement>("occ-threshold").value, 0.9);
    const isAuto = trigger === "auto";

    const computeLocal = (fallbackReason?: string): void => {
      const label = computeLabelLocalApprox({
        scene: syncScene(),
        maskImageData: maskLayer.getMaskImageData(),
        occThreshold,
        functionName: functionValue,
      });
      state.label = label;
      refreshLabelUi();
      updateRequestSummary("frontend://label/compute-local", performance.now() - startedAt, "APPROX");

      if (fallbackReason) {
        const degradedHint = isAuto ? "自动计算失败/已降级近似结果" : "后端失败/已降级近似结果";
        statusShortEl.textContent = `${degradedHint}（${reason}）`;
        log(`⚠️ ${fallbackReason}，已使用前端近似结果（${reason}）`);
      } else {
        log(isAuto ? `自动计算成功（前端快速，近似结果）：${reason}` : "Label 计算成功（前端快速，近似结果）");
      }
    };

    if (state.labelComputeMode === "frontend_fast") {
      computeLocal();
      return;
    }

    try {
      const maskBlob = state.maskBlob ?? (await maskLayer.exportMaskBinaryPNG());
      const backendStartedAt = performance.now();
      const { label } = await getApi().computeLabel(syncScene(), maskBlob, occThreshold, functionValue);
      state.label = label;
      refreshLabelUi();
      updateRequestSummary(endpoint, performance.now() - backendStartedAt, "HTTP 200");
      log(isAuto ? `自动计算成功（后端精确）：${reason}` : "Label 计算成功");
    } catch (err) {
      const elapsed = performance.now() - startedAt;
      computeLocal("后端精确计算失败");
      if (isTimeoutOrAbortError(err)) {
        updateRequestSummary(endpoint, elapsed, "TIMEOUT/ABORT");
        log("❌ 计算 Label 超时或请求被取消，请延长超时或降低分辨率/复杂度后重试");
      } else if (err instanceof ApiError && err.code === "MASK_DECODE_ERROR") {
        updateRequestSummary(endpoint, elapsed, `HTTP ${err.status} / ${err.code}`);
        log("❌ 计算 Label 失败：请检查 mask 尺寸是否与 scene.meta.resolution 完全一致");
      } else if (err instanceof ApiError) {
        updateRequestSummary(endpoint, elapsed, `HTTP ${err.status} / ${err.code}`);
        log(`❌ 计算 Label 失败：${err.code} - ${err.message}`);
      } else {
        updateRequestSummary(endpoint, elapsed, "UNKNOWN_ERROR");
        logError(err, "计算 Label 失败");
      }
      console.error(err);
    }
  };

  const scheduleAutoCompute = (reason: string): void => {
    if (!labelAutoComputeEl.checked) return;
    queuedAutoComputeReason = reason;
    if (autoComputeTimer !== null) {
      window.clearTimeout(autoComputeTimer);
    }
    autoComputeTimer = window.setTimeout(async () => {
      autoComputeTimer = null;
      if (autoComputeRunning) return;
      const queuedReason = queuedAutoComputeReason;
      queuedAutoComputeReason = null;
      if (!queuedReason) return;
      autoComputeRunning = true;
      try {
        await computeLabelAndRefresh(queuedReason, "auto");
      } finally {
        autoComputeRunning = false;
        if (queuedAutoComputeReason) {
          scheduleAutoCompute(queuedAutoComputeReason);
        }
      }
    }, 500);
  };

  const shuffleUseBackendEl = byId<HTMLInputElement>("shuffle-use-backend");
  const loadShuffleBackendPreference = (): boolean => {
    try {
      return localStorage.getItem("cdt.shuffleUseBackend") === "true";
    } catch {
      return false;
    }
  };
  shuffleUseBackendEl.checked = loadShuffleBackendPreference();
  updateShuffleModeStatus(shuffleUseBackendEl.checked);
  shuffleUseBackendEl.addEventListener("change", () => {
    const enabled = shuffleUseBackendEl.checked;
    try {
      localStorage.setItem("cdt.shuffleUseBackend", String(enabled));
    } catch {
      // ignore
    }
    updateShuffleModeStatus(enabled);
    log(enabled ? "已开启后端 Shuffle 模式" : "已切换为本地 Shuffle 模式");
  });

  const paletteApi = bindPalette(engine, render, log, vocab);
  initNodeRenderSettings();
  initCustomSymbolUpload(paletteApi.refresh);
  initComponentTemplateEditor({ engine, vocab, refreshPalette: paletteApi.refresh, render, log });
  bindMaskPaint(
    uiCanvas,
    maskLayer,
    () => state.mode === "mask",
    () => {
      state.maskBlob = null;
      scheduleAutoCompute("Mask 涂抹变更");
    },
    render,
  );

  const runInteractionLayerDiagnostic = (): boolean => {
    const uiStyle = getComputedStyle(uiCanvas);
    const maskStyle = getComputedStyle(maskCanvas);
    const uiRect = uiCanvas.getBoundingClientRect();
    const allCanvases = Array.from(document.querySelectorAll("#canvas-container canvas"));
    const parseZIndex = (value: string): number => {
      const parsed = Number.parseInt(value, 10);
      return Number.isFinite(parsed) ? parsed : 0;
    };
    const uiZIndex = parseZIndex(uiStyle.zIndex);
    const maxOtherCanvasZIndex = allCanvases
      .filter((canvas) => canvas !== uiCanvas)
      .reduce((max, canvas) => Math.max(max, parseZIndex(getComputedStyle(canvas).zIndex)), Number.NEGATIVE_INFINITY);

    const checks = {
      pointerEventsAuto: uiStyle.pointerEvents === "auto",
      uiZIndexHigher: uiZIndex > maxOtherCanvasZIndex,
      rectVisible: uiRect.width > 0 && uiRect.height > 0,
    };
    const passed = Object.values(checks).every(Boolean);
    interactionStatusEl.textContent = passed ? "OK" : "FAIL";

    if (!passed) {
      log(
        `⚠️ 交互层状态 FAIL：pointer-events=${uiStyle.pointerEvents}, ui-z=${uiStyle.zIndex}, mask-z=${maskStyle.zIndex}, rect=${uiRect.width.toFixed(1)}x${uiRect.height.toFixed(1)}。请清缓存并强制刷新（Ctrl+F5）。`,
      );
    }
    return passed;
  };

  runInteractionLayerDiagnostic();

  bindCircuitInteractions({
    engine,
    circuitCanvas,
    uiCanvas,
    canEdit: () => state.mode === "circuit",
    render,
    onChange: () => {
      syncScene();
      scheduleAutoCompute("电路编辑变更");
    },
    log,
  });
  bindPresets(engine, vocab, () => {
    state.label = null;
    state.maskBlob = null;
    syncScene();
    refreshLabelUi();
    render();
  }, log);

  // 可选功能元素：缺失不影响主流程
  bindOptionalClick("btn-export-local", async () => {
    try {
      const scene = syncScene();
      const imagePng = await exportCanvasPNG(circuitCanvas);
      const maskPng = state.maskBlob ?? (await maskLayer.exportMaskBinaryPNG());
      const compositePng = await exportCompositePNG(circuitCanvas, state.maskBlob ?? maskLayer.getMaskImageData(), {
        maskColor: "#ff0000",
        maskOpacity: 0.45,
      });
      const label = state.label ?? {
        counts_all: {},
        counts_visible: {},
        occlusion: [],
        occ_threshold: toNum(byId<HTMLInputElement>("occ-threshold").value, 0.9),
        function: byId<HTMLInputElement>("function-custom").value.trim() || byId<HTMLSelectElement>("function-select").value,
      };

      const zipBlob = await packZip([
        { name: "image.png", blob: imagePng },
        { name: "mask.png", blob: maskPng },
        { name: "image_with_mask.png", blob: compositePng },
        { name: "scene.json", blob: makeSceneJson(scene) },
        { name: "label.json", blob: makeLabelJson(label) },
      ]);
      downloadBlob("sample_local.zip", zipBlob);
      log("已导出本地样本包 sample_local.zip（含 image/mask/composite/scene/label）");
    } catch (err) {
      logError(err, "导出本地样本包失败");
    }
  }, log);

  bindOptionalClick("btn-apply-function", () => {
    const customValue = byId<HTMLInputElement>("function-custom").value.trim();
    const selectEl = byId<HTMLSelectElement>("function-select");
    if (!customValue) {
      log("Function 未填写自定义值，保留当前选择");
      return;
    }
    const exists = Array.from(selectEl.options).some((opt) => opt.value === customValue);
    if (!exists) {
      const opt = document.createElement("option");
      opt.value = customValue;
      opt.textContent = customValue;
      selectEl.appendChild(opt);
    }
    selectEl.value = customValue;
    log(`Function 已应用：${customValue}`);
  }, log);

  byId<HTMLButtonElement>("btn-new").addEventListener("click", () => {
    engine.clear();
    maskLayer.clear();
    state.label = null;
    state.maskBlob = null;
    syncScene();
    refreshLabelUi();
    render();
    log("已新建空场景");
  });

  byId<HTMLButtonElement>("btn-save-scene").addEventListener("click", () => {
    const blob = makeSceneJson(syncScene());
    downloadBlob("scene.json", blob);
    log("已导出 scene.json");
  });

  byId<HTMLButtonElement>("btn-load-scene").addEventListener("click", () => {
    byId<HTMLInputElement>("file-scene").click();
  });

  byId<HTMLInputElement>("file-scene").addEventListener("change", async (ev) => {
    try {
      const input = ev.currentTarget as HTMLInputElement;
      const file = input.files?.[0];
      if (!file) return;
      const text = await file.text();
      const scene = JSON.parse(text) as Scene;
      engine.loadScene(scene);
      state.scene = scene;
      render();
      log("已导入 scene.json");
    } catch (err) {
      logError(err, "导入 scene.json 失败，请确认文件内容为合法 JSON");
    }
  });

  byId<HTMLButtonElement>("btn-load-mask").addEventListener("click", () => {
    byId<HTMLInputElement>("file-mask").click();
  });

  byId<HTMLInputElement>("file-mask").addEventListener("change", async (ev) => {
    try {
      const input = ev.currentTarget as HTMLInputElement;
      const file = input.files?.[0];
      if (!file) return;
      await maskLayer.importMaskBlob(file);
      state.maskBlob = file;
      render();
      log("已导入 mask 文件");
    } catch (err) {
      logError(err, "导入 mask 失败，请使用 PNG 文件");
    }
  });

  byId<HTMLButtonElement>("btn-health").addEventListener("click", async () => {
    const baseUrlEl = byId<HTMLInputElement>("api-base-url");
    const checked = normalizeAndValidateBaseUrl(baseUrlEl.value);
    let apiBase = checked.normalized;
    if (!checked.valid) {
      log(`⚠️ 后端地址不合法：${baseUrlEl.value}。已回退默认值 ${DEFAULT_API_BASE_URL}`);
      apiBase = DEFAULT_API_BASE_URL;
      baseUrlEl.value = apiBase;
    }
    if (apiBase.startsWith("/api/v1")) {
      log("ℹ️ 当前 baseUrl 为相对路径（/api/v1），请求依赖前端代理/同源反代。");
    }
    try {
      const resp = await fetch(`${apiBase}/healthz`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      log("Health 检查成功");
    } catch (err) {
      log(`ℹ️ Health 失败时使用的 baseUrl: ${apiBase}`);
      logError(err, "Health 检查失败");
    }
  });

  bindOptionalClick("btn-reset-backend", () => {
    const baseUrlEl = byId<HTMLInputElement>("api-base-url");
    const timeoutEl = byId<HTMLInputElement>("api-timeout-ms");
    baseUrlEl.value = DEFAULT_API_BASE_URL;
    timeoutEl.value = "30000";
    try {
      localStorage.removeItem("cdt.apiBaseUrl");
      localStorage.removeItem("cdt.apiTimeoutMs");
    } catch {
      // ignore
    }
    log("已重置后端地址与超时配置（已清理本地存储 cdt.apiBaseUrl / cdt.apiTimeoutMs）");
  });

  byId<HTMLButtonElement>("btn-generate-mask").addEventListener("click", async () => {
    try {
      const strategy = byId<HTMLSelectElement>("mask-strategy").value;
      const seedInputEl = byId<HTMLInputElement>("mask-seed");
      const seedInput = seedInputEl.value.trim();
      const seed = seedInput || String(Math.floor(Math.random() * 2 ** 31));
      if (!seedInput) {
        seedInputEl.value = seed;
      }
      const params = {
        ratio: toNum(byId<HTMLInputElement>("mask-ratio").value, 0.35),
        scale: toNum(byId<HTMLInputElement>("mask-scale").value, 64),
        seed,
      };
      const { maskPngBlob } = await getApi().generateMask(syncScene(), strategy, params);
      await maskLayer.importMaskBlob(maskPngBlob);
      state.maskBlob = maskPngBlob;
      render();
      log("已调用后端自动生成 Mask");
      scheduleAutoCompute("自动生成 Mask 成功");
    } catch (err) {
      logError(err, "自动生成 Mask 失败，请检查后端地址和场景内容");
    }
  });

  byId<HTMLButtonElement>("btn-validate-scene").addEventListener("click", async () => {
    try {
      const { scene_norm, warnings } = await getApi().validateScene(syncScene(), false);
      engine.loadScene(scene_norm);
      syncScene();
      render();
      log(`Scene 校验通过${warnings.length ? `，warnings: ${warnings.join("; ")}` : ""}`);
    } catch (err) {
      logError(err, "Scene 校验失败，请先修正连接或元数据");
    }
  });

  byId<HTMLButtonElement>("btn-compute-label").addEventListener("click", async () => {
    await computeLabelAndRefresh("手动触发", "manual");
  });

  byId<HTMLButtonElement>("btn-shuffle").addEventListener("click", async () => {
    const parsedSeed = (() => {
      const raw = byId<HTMLInputElement>("shuffle-seed").value;
      if (!raw) return undefined;
      const n = Number(raw);
      return Number.isFinite(n) ? n : undefined;
    })();
    const margin = toNum(byId<HTMLInputElement>("shuffle-margin").value, 20);

    engine.shuffleNodePositions(parsedSeed, margin);
    syncScene();
    render();
    log("已先执行本地 shuffle 并刷新画布");
    scheduleAutoCompute("Shuffle 完成（前端）");

    if (!shuffleUseBackendEl.checked) {
      log("当前为本地 shuffle 模式，已跳过后端请求");
      return;
    }

    const doBackendShuffle = async (): Promise<void> => {
      const params = {
        seed: parsedSeed,
        placement: byId<HTMLSelectElement>("shuffle-placement").value,
        route_mode: byId<HTMLSelectElement>("shuffle-route").value,
        bend_mode: byId<HTMLSelectElement>("shuffle-bend").value,
        margin,
        max_tries: toNum(byId<HTMLInputElement>("shuffle-max-tries").value, 2000),
      };
      const returnPaths = byId<HTMLSelectElement>("shuffle-return-paths").value === "true";
      const startedAt = performance.now();
      const { scene_shuffled } = await getApi().shuffleScene(syncScene(), params, returnPaths);
      log(`Shuffle 请求耗时 ${(performance.now() - startedAt).toFixed(0)}ms`);
      if (scene_shuffled) {
        engine.loadScene(scene_shuffled);
        syncScene();
        render();
        log("后端 Shuffle 已返回 scene_shuffled，已覆盖前端结果");
        scheduleAutoCompute("Shuffle 完成（后端）");
      } else {
        log("后端未返回 scene_shuffled，已使用前端结果");
      }
    };

    try {
      await doBackendShuffle();
    } catch (err) {
      log(`⚠️ ${userFacingError(err, "后端 Shuffle 失败")}`);
      if (isTimeoutError(err)) {
        log("后端 Shuffle 超时，已保留前端结果，不阻塞后续交互");
        return;
      }

      if (err instanceof ApiError) {
        log("后端返回业务错误，已使用前端结果");
      }
    }
  });

  byId<HTMLButtonElement>("btn-save-backend").addEventListener("click", async () => {
    try {
      const imagePng = await renderSceneToImagePng(sceneItem);
      const maskPng = state.maskBlob ?? (await maskLayer.exportMaskBinaryPNG());
      const sceneJson = makeSceneJson(syncScene());
      const label = state.label ?? {
        counts_all: {},
        counts_visible: {},
        occlusion: [],
        occ_threshold: toNum(byId<HTMLInputElement>("occ-threshold").value, 0.9),
        function: byId<HTMLInputElement>("function-custom").value.trim() || byId<HTMLSelectElement>("function-select").value,
      };
      const labelJson = makeLabelJson(label);

      const result = await getApi().saveSampleMultipart({
        imagePng,
        maskPng,
        sceneJson,
        labelJson,
        sampleId: suggestSampleId(),
      });
      log(`样本已保存到后端，sample_id=${result.sample_id}`);
    } catch (err) {
      logError(err, "保存到后端失败，请检查后端服务状态");
    }
  });

  byId<HTMLButtonElement>("btn-generate-batch").addEventListener("click", async () => {
    const statusEl = byId<HTMLDivElement>("batch-job-status");
    const setStatus = (msg: string): void => {
      statusEl.textContent = msg;
    };

    const n = Math.max(1, Math.floor(toNum(byId<HTMLInputElement>("batch-n").value, 10)));
    const seedStart = Math.floor(toNum(byId<HTMLInputElement>("batch-seed-start").value, 1));
    const useBackendShuffle = byId<HTMLInputElement>("batch-use-shuffle").checked;
    const maskStrategy = byId<HTMLSelectElement>("batch-mask-strategy").value;
    const maskParams = {
      ratio: toNum(byId<HTMLInputElement>("batch-mask-ratio").value, 0.35),
      scale: toNum(byId<HTMLInputElement>("batch-mask-scale").value, 64),
    };
    const occThreshold = toNum(byId<HTMLInputElement>("occ-threshold").value, 0.9);
    const functionName = byId<HTMLInputElement>("function-custom").value.trim() || byId<HTMLSelectElement>("function-select").value;

    const payload = {
      job_type: "batch_dataset",
      scene: syncScene(),
      n,
      seed_start: seedStart,
      use_backend_shuffle: useBackendShuffle,
      mask_strategy: maskStrategy,
      mask_params: maskParams,
      occ_threshold: occThreshold,
      function: functionName,
      zip: byId<HTMLInputElement>("batch-zip").checked,
    };

    const renderSceneToImagePng = async (scene: Scene): Promise<Blob> => {
      const canvas = document.createElement("canvas");
      canvas.width = resolution.w;
      canvas.height = resolution.h;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("无法创建批处理离屏画布");
      const tempEngine = new CanvasEngine({ resolution, vocab });
      tempEngine.loadScene(scene);
      tempEngine.draw(ctx);
      return await exportCanvasPNG(canvas);
    };

    const requestWithRetry = async <T>(fn: () => Promise<T>, retries = 2, delayMs = 600): Promise<T> => {
      let lastError: unknown;
      for (let attempt = 0; attempt <= retries; attempt += 1) {
        try {
          return await fn();
        } catch (error) {
          lastError = error;
          if (attempt >= retries) break;
          await sleepMs(delayMs * (attempt + 1));
        }
      }
      throw lastError;
    };

    const runBatchMvp = async (): Promise<void> => {
      const baseScene = syncScene();
      const concurrency = 2;
      let nextIndex = 0;
      let succeeded = 0;
      let failed = 0;
      const maxAttempts = Math.max(n, n * 20);
      const savedItems: Array<{ sampleId: string; savedPaths: Record<string, any> }> = [];

      const worker = async (): Promise<void> => {
        while (true) {
          if (succeeded >= n) return;
          const index = nextIndex;
          nextIndex += 1;
          if (index >= maxAttempts) return;

          const seed = seedStart + index;
          const sampleId = `batch_mvp_${seed}_${index}`;
          const processOne = async (): Promise<void> => {
            let sceneItem = baseScene;
            if (useBackendShuffle) {
              const shuffled = await getApi().shuffleScene(baseScene, { seed }, true);
              sceneItem = shuffled.scene_shuffled ?? baseScene;
            }

            const maskRes = await getApi().generateMask(sceneItem, maskStrategy, { ...maskParams, seed });
            const labelRes = await getApi().computeLabel(sceneItem, maskRes.maskPngBlob, occThreshold, functionName);

            const sceneJson = makeSceneJson(sceneItem);
            const labelJson = makeLabelJson(labelRes.label);
            const imagePng = await renderSceneToImagePng(sceneItem);

            const saveRes = await getApi().saveSampleMultipart({
              imagePng,
              maskPng: maskRes.maskPngBlob,
              sceneJson,
              labelJson,
              sampleId,
            });
            savedItems.push({ sampleId: saveRes.sample_id, savedPaths: saveRes.saved_paths ?? {} });
          };

          try {
            await requestWithRetry(processOne, 2, 700);
            succeeded += 1;
          } catch (error) {
            failed += 1;
            log(`⚠️ MVP 批处理样本失败(index=${index}, seed=${seed})：${userFacingError(error, "处理失败")}`);
          }
          const progress = Math.round((succeeded / Math.max(n, 1)) * 100);
          setStatus(`MVP串行批处理中 | 进度=${progress}% | 成功=${succeeded}/${n} | 尝试失败=${failed}`);
        }
      };

      await Promise.all(Array.from({ length: concurrency }, () => worker()));
      if (succeeded < n) {
        setStatus(`MVP批处理结束 | 仅成功=${succeeded}/${n} | 尝试失败=${failed}`);
        log(`⚠️ MVP 批处理达到最大尝试次数，成功=${succeeded}/${n}，失败=${failed}`);
      } else {
        setStatus(`MVP批处理完成 | 成功=${succeeded}/${n} | 尝试失败=${failed}`);
        log(`MVP 批处理完成：成功=${succeeded}/${n}，失败=${failed}`);
      }
      if (savedItems.length > 0) {
        const first = savedItems[0];
        log(`MVP 保存位置示例：sample_id=${first.sampleId}，image=${first.savedPaths?.image ?? "(未知)"}`);
      }
      log(`MVP 累计已保存 ${savedItems.length} 条样本到后端 DATASET_ROOT。`);
    };

    try {
      const { job_id } = await getApi().submitJob(payload);
      log(`批处理任务已提交，job_id=${job_id}`);
      setStatus(`任务 ${job_id} 已提交，等待执行...`);

      for (let i = 0; i < 600; i += 1) {
        const st = await getApi().getJobStatus(job_id);
        const progress = Math.round((Number(st.progress) || 0) * 100);
        const succeeded = Number(st?.result?.succeeded ?? 0);
        const failed = Number(st?.result?.failed ?? 0);
        setStatus(`状态=${st.status ?? "unknown"} | 进度=${progress}% | 成功=${succeeded} | 失败=${failed}`);

        if (st.status === "succeeded") {
          const zipPath = st?.result?.paths?.zip;
          const dirPath = st?.result?.paths?.dir;
          log(`批处理完成：成功=${succeeded}，失败=${failed}`);
          log(`结果目录：${dirPath ?? "(无)"}${zipPath ? `，zip=${zipPath}` : ""}`);
          return;
        }
        if (st.status === "failed") {
          const errMsg = st?.error?.message ?? "未知错误";
          log(`❌ 批处理失败：${errMsg}`);
          setStatus(`任务失败：${errMsg}`);
          return;
        }
        await sleepMs(1000);
      }
      setStatus("任务轮询超时，请稍后用 job_id 手动查询。");
      log("⚠️ 批处理轮询超时，请稍后重试查询任务状态");
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        log("⚠️ Jobs 接口返回 NotFound，自动回退到前端 MVP 批处理流程");
        setStatus("Jobs 接口不可用，已回退到 MVP 批处理...");
        if (payload.zip) {
          log("ℹ️ 当前为 MVP 回退流程：不生成 jobs zip；样本会直接保存到后端 DATASET_ROOT。");
        }
        try {
          await runBatchMvp();
          return;
        } catch (mvpError) {
          logError(mvpError, "MVP 批处理失败");
          setStatus("MVP 批处理失败，请查看日志。");
          return;
        }
      }
      logError(err, "批处理任务提交/查询失败");
      setStatus("批处理失败，请查看日志。");
    }
  });

  byId<HTMLButtonElement>("btn-export-image").addEventListener("click", async () => {
    const blob = await exportCanvasPNG(circuitCanvas);
    downloadBlob("image.png", blob);
    log("已导出 image.png");
  });

  byId<HTMLButtonElement>("btn-export-mask").addEventListener("click", async () => {
    const blob = await maskLayer.exportMaskBinaryPNG();
    downloadBlob("mask.png", blob);
    log("已导出 mask.png");
  });

  byId<HTMLButtonElement>("btn-export-composite").addEventListener("click", async () => {
    try {
      const compositePng = await exportCompositePNG(circuitCanvas, state.maskBlob ?? maskLayer.getMaskImageData(), {
        maskColor: "#ff0000",
        maskOpacity: 0.45,
      });
      downloadBlob("image_with_mask.png", compositePng);
      log("已导出 image_with_mask.png（电路图 + 红色半透明 Mask 覆盖）");
    } catch (err) {
      logError(err, "导出 image_with_mask.png 失败");
    }
  });

  byId<HTMLButtonElement>("mask-mode-paint").addEventListener("click", () => {
    maskLayer.setEraseMode(false);
  });
  byId<HTMLButtonElement>("mask-mode-erase").addEventListener("click", () => {
    maskLayer.setEraseMode(true);
  });
  byId<HTMLButtonElement>("btn-clear-mask").addEventListener("click", () => {
    maskLayer.clear();
    state.maskBlob = null;
    render();
    log("Mask 已清空");
  });

  byId<HTMLInputElement>("mask-brush-size").addEventListener("input", (ev) => {
    const target = ev.currentTarget as HTMLInputElement;
    maskLayer.setBrush(toNum(target.value, 24), toNum(byId<HTMLInputElement>("mask-brush-hardness").value, 0.8));
  });
  byId<HTMLInputElement>("mask-brush-hardness").addEventListener("input", (ev) => {
    const target = ev.currentTarget as HTMLInputElement;
    maskLayer.setBrush(toNum(byId<HTMLInputElement>("mask-brush-size").value, 24), toNum(target.value, 0.8));
  });

  byId<HTMLButtonElement>("btn-mode-circuit").addEventListener("click", () => {
    state.mode = "circuit";
    syncInteractionCanvas();
    log("已切换到电路编辑模式");
  });
  byId<HTMLButtonElement>("btn-mode-mask").addEventListener("click", () => {
    state.mode = "mask";
    syncInteractionCanvas();
    log("已切换到 Mask 编辑模式");
  });

  syncInteractionCanvas();
  syncScene();
  refreshLabelUi();
  render();
  log("应用初始化完成");
}

function bindPresets(engine: CanvasEngine, vocab: any, afterApply: () => void, log: (msg: string) => void): void {
  const availableTypes = new Set(Object.keys(vocab?.types ?? {}));

  const resolveType = (preferred: string[], fallback: string): string => {
    for (const type of preferred) {
      if (availableTypes.has(type)) return type;
    }
    return fallback;
  };

  const SOURCE_TYPE = resolveType(["V", "VIN", "VSRC"], "R");
  const GROUND_TYPE = resolveType(["GND", "GROUND", "AGND"], "C");
  const RESISTOR_TYPE = resolveType(["R", "RES"], "R");
  const CAPACITOR_TYPE = resolveType(["C", "CAP"], "C");

  const addChain = (types: string[], y: number): string[] => {
    return types.map((type, idx) => engine.addNode(type, { x: 180 + idx * 180, y }));
  };

  const connectByEnds = (nodeA: string, typeA: string, nodeB: string, typeB: string): void => {
    const pinsA = getPinsFromVocab(vocab, typeA);
    const pinsB = getPinsFromVocab(vocab, typeB);
    if (!pinsA.length || !pinsB.length) {
      log(`⚠️ 预设连接跳过：${typeA}(${pinsA.length}) -> ${typeB}(${pinsB.length}) 缺少 pin 定义`);
      return;
    }
    engine.connectPins({ node: nodeA, pin: pinsA[pinsA.length - 1] }, { node: nodeB, pin: pinsB[0] });
  };

  bindOptionalClick("btn-preset-vrcgnd", () => {
    engine.clear();
    const types = [SOURCE_TYPE, RESISTOR_TYPE, CAPACITOR_TYPE, GROUND_TYPE];
    const ids = addChain(types, 360);
    for (let i = 0; i < ids.length - 1; i++) connectByEnds(ids[i], types[i], ids[i + 1], types[i + 1]);
    afterApply();
    log(`已加载预设：${types.join("-")}（4 器件）`);
  }, log);

  bindOptionalClick("btn-preset-rc-parallel", () => {
    engine.clear();
    const source = engine.addNode(SOURCE_TYPE, { x: 200, y: 380 });
    const resistor = engine.addNode(RESISTOR_TYPE, { x: 420, y: 300 });
    const capacitor = engine.addNode(CAPACITOR_TYPE, { x: 420, y: 460 });
    const ground = engine.addNode(GROUND_TYPE, { x: 680, y: 380 });
    connectByEnds(source, SOURCE_TYPE, resistor, RESISTOR_TYPE);
    connectByEnds(source, SOURCE_TYPE, capacitor, CAPACITOR_TYPE);
    connectByEnds(resistor, RESISTOR_TYPE, ground, GROUND_TYPE);
    connectByEnds(capacitor, CAPACITOR_TYPE, ground, GROUND_TYPE);
    afterApply();
    log(`已加载预设：${RESISTOR_TYPE}∥${CAPACITOR_TYPE} + ${GROUND_TYPE}（4 器件）`);
  }, log);

  bindOptionalClick("btn-preset-rcladder", () => {
    engine.clear();
    const types = [SOURCE_TYPE, RESISTOR_TYPE, CAPACITOR_TYPE, RESISTOR_TYPE, GROUND_TYPE];
    const ids = addChain(types, 380);
    for (let i = 0; i < ids.length - 1; i++) connectByEnds(ids[i], types[i], ids[i + 1], types[i + 1]);
    afterApply();
    log(`已加载预设：${RESISTOR_TYPE}-${CAPACITOR_TYPE}-${RESISTOR_TYPE} 梯形（5 器件）`);
  }, log);
}

function bindCircuitInteractions(opts: {
  engine: CanvasEngine;
  circuitCanvas: HTMLCanvasElement;
  uiCanvas: HTMLCanvasElement;
  canEdit: () => boolean;
  render: () => void;
  onChange: () => void;
  log: (msg: string) => void;
}): void {
  const { engine, uiCanvas, canEdit, render, onChange, log } = opts;
  const uiCtx = uiCanvas.getContext("2d");
  let draggingNodeId: string | null = null;
  let dragOffset = { x: 0, y: 0 };
  let wiringFrom: { node: string; pin: string } | null = null;
  let hoverPoint: { x: number; y: number } | null = null;

  const point = (ev: MouseEvent) => {
    const rect = uiCanvas.getBoundingClientRect();
    return {
      x: ((ev.clientX - rect.left) / rect.width) * uiCanvas.width,
      y: ((ev.clientY - rect.top) / rect.height) * uiCanvas.height,
    };
  };

  const drawUi = () => {
    if (!uiCtx) return;
    uiCtx.clearRect(0, 0, uiCanvas.width, uiCanvas.height);
    if (!wiringFrom) return;
    const fromXY = engine.endpointPosition(wiringFrom);
    const to = hoverPoint ?? fromXY;
    uiCtx.save();
    uiCtx.strokeStyle = "#1e88e5";
    uiCtx.lineWidth = 2;
    uiCtx.setLineDash([6, 4]);
    uiCtx.beginPath();
    uiCtx.moveTo(fromXY.x, fromXY.y);
    uiCtx.lineTo(to.x, to.y);
    uiCtx.stroke();
    uiCtx.restore();
  };

  const redrawAll = () => {
    render();
    drawUi();
  };

  uiCanvas.addEventListener("mousedown", (ev) => {
    if (!canEdit()) return;
    const p = point(ev);
    const hitPin = engine.hitTestPin(p);
    if (hitPin) {
      wiringFrom = hitPin;
      hoverPoint = p;
      redrawAll();
      return;
    }

    const hitNodeId = engine.hitTestNode(p);
    if (!hitNodeId) {
      engine.setSelection(null);
      redrawAll();
      return;
    }

    const node = engine.getNodeById(hitNodeId);
    engine.setSelection({ nodeId: hitNodeId });
    if (node) {
      draggingNodeId = hitNodeId;
      dragOffset = { x: p.x - node.pos.x, y: p.y - node.pos.y };
    }
    redrawAll();
  });

  window.addEventListener("mousemove", (ev) => {
    if (!canEdit()) return;
    const p = point(ev);
    hoverPoint = p;
    if (draggingNodeId) {
      engine.moveNode(draggingNodeId, { x: p.x - dragOffset.x, y: p.y - dragOffset.y });
      onChange();
    }
    render();
    drawUi();
  });

  window.addEventListener("mouseup", (ev) => {
    if (!canEdit()) return;
    const p = point(ev);
    if (draggingNodeId) {
      draggingNodeId = null;
      onChange();
      redrawAll();
      return;
    }

    if (!wiringFrom) return;
    const targetPin = engine.hitTestPin(p);
    if (targetPin && !(targetPin.node === wiringFrom.node && targetPin.pin === wiringFrom.pin)) {
      try {
        const { replacedOld } = engine.connectPins(wiringFrom, targetPin);
        onChange();
        const replaceHint = replacedOld ? "（已替换旧连线）" : "";
        log(`已连线：${wiringFrom.node}.${wiringFrom.pin} -> ${targetPin.node}.${targetPin.pin}${replaceHint}`);
      } catch {
        // ignore
      }
    }
    wiringFrom = null;
    redrawAll();
  });

  window.addEventListener("keydown", (ev) => {
    if (!canEdit()) return;
    if (ev.key !== "Delete" && ev.key !== "Backspace") return;
    const sel = engine.getSelection();
    if (sel?.nodeId) {
      engine.removeNode(sel.nodeId);
      onChange();
      redrawAll();
      log(`已删除器件：${sel.nodeId}`);
    } else if (sel?.netId) {
      engine.removeNet(sel.netId);
      onChange();
      redrawAll();
      log(`已删除连线：${sel.netId}`);
    }
  });

  uiCanvas.addEventListener(
    "wheel",
    (ev) => {
      if (!canEdit()) return;
      const sel = engine.getSelection();
      if (!sel?.nodeId) return;
      const node = engine.getNodeById(sel.nodeId);
      if (!node) return;

      ev.preventDefault();
      if (ev.altKey) {
        const step = ev.deltaY < 0 ? 10 : -10;
        engine.rotateNode(node.id, Number(node.rot ?? 0) + step);
      } else {
        const scaleStep = ev.deltaY < 0 ? 1.08 : 0.92;
        engine.scaleNode(node.id, Number(node.scale ?? 1) * scaleStep);
      }
      onChange();
      redrawAll();
    },
    { passive: false }
  );
}

function bindMaskPaint(
  interactionCanvas: HTMLCanvasElement,
  maskLayer: MaskLayer,
  canPaint: () => boolean,
  onMaskMutated: () => void,
  render: () => void
): void {
  let painting = false;

  const point = (ev: MouseEvent) => {
    const rect = interactionCanvas.getBoundingClientRect();
    return {
      x: ((ev.clientX - rect.left) / rect.width) * interactionCanvas.width,
      y: ((ev.clientY - rect.top) / rect.height) * interactionCanvas.height,
    };
  };

  interactionCanvas.addEventListener("mousedown", (ev) => {
    if (!canPaint()) return;
    painting = true;
    maskLayer.beginStroke(point(ev));
    onMaskMutated();
    render();
  });
  window.addEventListener("mousemove", (ev) => {
    if (!painting) return;
    maskLayer.addStrokePoint(point(ev));
    onMaskMutated();
    render();
  });
  window.addEventListener("mouseup", () => {
    if (!painting) return;
    painting = false;
    maskLayer.endStroke();
    render();
  });
}

function bindPalette(engine: CanvasEngine, render: () => void, log: (msg: string) => void, vocab: any): { refresh: () => void } {
  const palette = byId<HTMLDivElement>("palette-list");
  const search = byId<HTMLInputElement>("palette-search");

  const redraw = () => {
    const types = Object.keys(vocab?.types ?? {});
    const keyword = search.value.trim().toLowerCase();
    const filtered = types.filter((t) => t.toLowerCase().includes(keyword));
    palette.innerHTML = "";
    const count = document.createElement("div");
    count.className = "muted small";
    count.textContent = `已加载器件类型: ${types.length}`;
    palette.appendChild(count);
    filtered.slice(0, 80).forEach((type, idx) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn";
      btn.textContent = type;
      btn.addEventListener("click", () => {
        engine.addNode(type, { x: 180 + (idx % 8) * 100, y: 150 + Math.floor(idx / 8) * 100 });
        render();
        log(`已添加器件: ${type}`);
      });
      palette.appendChild(btn);
    });

    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = "无匹配器件类型";
      palette.appendChild(empty);
    }
  };

  search.addEventListener("input", redraw);
  redraw();
  return { refresh: redraw };
}

function initDrawSymbolPad(opts: {
  engine: CanvasEngine;
  vocab: any;
  log: (msg: string) => void;
  refreshPalette: () => void;
  onApplied: (type: string) => void;
}): void {
  const { engine, vocab, log, refreshPalette, onApplied } = opts;
  const canvas = byId<HTMLCanvasElement>("draw-symbol-canvas");
  const ctx = canvas.getContext("2d");
  const typeInput = byId<HTMLInputElement>("draw-symbol-type");
  const widthEl = byId<HTMLInputElement>("draw-symbol-width");
  const widthText = byId<HTMLSpanElement>("draw-symbol-width-text");
  const statusEl = byId<HTMLDivElement>("draw-symbol-status");
  const penBtn = byId<HTMLButtonElement>("btn-draw-symbol-pen");
  const eraserBtn = byId<HTMLButtonElement>("btn-draw-symbol-eraser");
  const lineBtn = byId<HTMLButtonElement>("btn-draw-symbol-line");
  const curveBtn = byId<HTMLButtonElement>("btn-draw-symbol-curve");
  if (!ctx) return;

  let drawing = false;
  let toolMode: "pen" | "eraser" | "line" | "curve" = "pen";
  let lineStart: { x: number; y: number } | null = null;
  let curveStart: { x: number; y: number } | null = null;
  let curveControl: { x: number; y: number } | null = null;

  const updateToolState = () => {
    penBtn.classList.toggle("is-active", toolMode === "pen");
    eraserBtn.classList.toggle("is-active", toolMode === "eraser");
    lineBtn.classList.toggle("is-active", toolMode === "line");
    curveBtn.classList.toggle("is-active", toolMode === "curve");
    penBtn.setAttribute("aria-pressed", String(toolMode === "pen"));
    eraserBtn.setAttribute("aria-pressed", String(toolMode === "eraser"));
    lineBtn.setAttribute("aria-pressed", String(toolMode === "line"));
    curveBtn.setAttribute("aria-pressed", String(toolMode === "curve"));
  };

  const point = (ev: MouseEvent) => {
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((ev.clientX - rect.left) / rect.width) * canvas.width,
      y: ((ev.clientY - rect.top) / rect.height) * canvas.height,
    };
  };

  const clearCanvas = () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#e2e8f0";
    ctx.strokeRect(0.5, 0.5, canvas.width - 1, canvas.height - 1);
  };

  const setStrokeStyle = () => {
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = Math.max(1, Number(widthEl.value) || 4);
    ctx.globalCompositeOperation = toolMode === "eraser" ? "destination-out" : "source-over";
    ctx.strokeStyle = "#111827";
  };

  clearCanvas();
  widthText.textContent = widthEl.value;
  updateToolState();

  canvas.addEventListener("mousedown", (ev) => {
    const p = point(ev);
    if (toolMode === "line") {
      lineStart = p;
      statusEl.textContent = "直线模式：拖拽并松开绘制一条直线。";
      return;
    }
    if (toolMode === "curve") {
      if (!curveStart) {
        curveStart = p;
        curveControl = null;
        statusEl.textContent = "曲线模式：已设置起点，请点击控制点。";
      } else if (!curveControl) {
        curveControl = p;
        statusEl.textContent = "曲线模式：已设置控制点，请点击终点。";
      } else {
        const end = p;
        setStrokeStyle();
        ctx.beginPath();
        ctx.moveTo(curveStart.x, curveStart.y);
        ctx.quadraticCurveTo(curveControl.x, curveControl.y, end.x, end.y);
        ctx.stroke();
        curveStart = null;
        curveControl = null;
        statusEl.textContent = "曲线已绘制。";
      }
      return;
    }

    drawing = true;
    setStrokeStyle();
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
  });
  window.addEventListener("mousemove", (ev) => {
    if (!drawing || (toolMode !== "pen" && toolMode !== "eraser")) return;
    const p = point(ev);
    setStrokeStyle();
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
  });
  window.addEventListener("mouseup", (ev) => {
    if (toolMode === "line" && lineStart) {
      const p = point(ev);
      setStrokeStyle();
      ctx.beginPath();
      ctx.moveTo(lineStart.x, lineStart.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      lineStart = null;
      statusEl.textContent = "直线已绘制。";
    }
    drawing = false;
  });

  widthEl.addEventListener("input", () => {
    widthText.textContent = widthEl.value;
  });

  bindOptionalClick("btn-draw-symbol-pen", () => {
    toolMode = "pen";
    curveStart = null;
    curveControl = null;
    updateToolState();
    statusEl.textContent = "已切换到画笔模式。";
  }, log);
  bindOptionalClick("btn-draw-symbol-eraser", () => {
    toolMode = "eraser";
    curveStart = null;
    curveControl = null;
    updateToolState();
    statusEl.textContent = "已切换到橡皮擦模式。";
  }, log);
  bindOptionalClick("btn-draw-symbol-line", () => {
    toolMode = "line";
    lineStart = null;
    curveStart = null;
    curveControl = null;
    updateToolState();
    statusEl.textContent = "已切换到直线模式。";
  }, log);
  bindOptionalClick("btn-draw-symbol-curve", () => {
    toolMode = "curve";
    lineStart = null;
    curveStart = null;
    curveControl = null;
    updateToolState();
    statusEl.textContent = "已切换到曲线模式：依次点击起点→控制点→终点。";
  }, log);
  bindOptionalClick("btn-draw-symbol-clear", () => {
    clearCanvas();
    lineStart = null;
    curveStart = null;
    curveControl = null;
    statusEl.textContent = "画板已清空。";
  }, log);

  bindOptionalClick("btn-draw-symbol-apply", () => {
    const type = typeInput.value.trim();
    if (!type) {
      statusEl.textContent = "请先输入器件类型名。";
      return;
    }

    if (!vocab.types || typeof vocab.types !== "object") vocab.types = {};
    if (!vocab.types[type]) {
      vocab.types[type] = {
        category: "custom",
        display_name: type,
        size: { w: 110, h: 70 },
        pins: {
          p0: { x: -55, y: 0 },
          p1: { x: 55, y: 0 },
        },
      };
      refreshPalette();
      log(`已通过手绘新增器件类型：${type}`);
    }

    const dataUrl = canvas.toDataURL("image/png");
    const img = new Image();
    img.onload = () => {
      engine.setCustomSymbol(type, img);
      onApplied(type);
      statusEl.textContent = `已将手绘符号应用到 ${type}`;
      log(`已应用手绘符号：${type}`);
    };
    img.onerror = () => {
      statusEl.textContent = "手绘符号导出失败，请重试。";
    };
    img.src = dataUrl;
  }, log);
}

function initComponentTemplateEditor(opts: {
  engine: CanvasEngine;
  vocab: any;
  refreshPalette: () => void;
  render: () => void;
  log: (msg: string) => void;
}): void {
  const { engine, vocab, refreshPalette, render, log } = opts;
  const typeEl = byId<HTMLSelectElement>("component-template-type");
  const sizeWEl = byId<HTMLInputElement>("component-size-w");
  const sizeHEl = byId<HTMLInputElement>("component-size-h");
  const pinNameEl = byId<HTMLSelectElement>("component-pin-name");
  const pinXEl = byId<HTMLInputElement>("component-pin-x");
  const pinYEl = byId<HTMLInputElement>("component-pin-y");
  const newPinEl = byId<HTMLInputElement>("component-new-pin-name");
  const statusEl = byId<HTMLDivElement>("component-template-status");
  const previewCanvas = byId<HTMLCanvasElement>("component-template-preview");
  const previewCtx = previewCanvas.getContext("2d");
  const previewEngine = new CanvasEngine({
    resolution: { w: previewCanvas.width, h: previewCanvas.height },
    vocab,
  });

  const ensureTypeConfig = (type: string): any => {
    if (!vocab.types || typeof vocab.types !== "object") vocab.types = {};
    if (!vocab.types[type]) {
      vocab.types[type] = {
        category: "custom",
        display_name: type,
        size: { w: 110, h: 70 },
        pins: { p0: { x: -55, y: 0 }, p1: { x: 55, y: 0 } },
      };
    }
    const t = vocab.types[type];
    if (!t.size || typeof t.size !== "object") t.size = { w: 110, h: 70 };
    if (!t.pins || typeof t.pins !== "object") t.pins = { p0: { x: -55, y: 0 }, p1: { x: 55, y: 0 } };
    return t;
  };

  const refreshTypeList = () => {
    const prev = typeEl.value;
    typeEl.innerHTML = "";
    const types = Object.keys(vocab?.types ?? {}).sort();
    for (const t of types) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      typeEl.appendChild(opt);
    }
    if (prev && types.includes(prev)) typeEl.value = prev;
  };

  const refreshPinList = () => {
    const type = typeEl.value;
    if (!type) return;
    const cfg = ensureTypeConfig(type);
    sizeWEl.value = String(Math.round(Number(cfg.size?.w ?? 110)));
    sizeHEl.value = String(Math.round(Number(cfg.size?.h ?? 70)));

    const prevPin = pinNameEl.value;
    pinNameEl.innerHTML = "";
    const pins = Object.keys(cfg.pins ?? {});
    for (const p of pins) {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      pinNameEl.appendChild(opt);
    }
    if (!pins.length) {
      cfg.pins = { p0: { x: -55, y: 0 }, p1: { x: 55, y: 0 } };
      refreshPinList();
      return;
    }
    if (prevPin && pins.includes(prevPin)) pinNameEl.value = prevPin;
    const pin = cfg.pins[pinNameEl.value] ?? cfg.pins[pins[0]];
    pinXEl.value = String(Math.round(Number(pin?.x ?? 0)));
    pinYEl.value = String(Math.round(Number(pin?.y ?? 0)));
    redrawTemplatePreview();
  };

  const applyAndRender = (message: string) => {
    refreshPalette();
    refreshTypeList();
    refreshPinList();
    render();
    statusEl.textContent = message;
    log(message);
    redrawTemplatePreview();
  };

  const redrawTemplatePreview = () => {
    if (!previewCtx) return;
    previewEngine.clear();
    const type = typeEl.value;
    if (type) {
      previewEngine.addNode(type, { x: previewCanvas.width / 2, y: previewCanvas.height / 2 });
    }
    previewEngine.draw(previewCtx);
  };

  bindOptionalClick("btn-apply-component-size", () => {
    const type = typeEl.value;
    if (!type) return;
    const cfg = ensureTypeConfig(type);
    cfg.size.w = Math.max(20, Math.round(Number(sizeWEl.value) || 110));
    cfg.size.h = Math.max(20, Math.round(Number(sizeHEl.value) || 70));
    applyAndRender(`已更新 ${type} 尺寸：${cfg.size.w} x ${cfg.size.h}`);
  }, log);

  bindOptionalClick("btn-apply-component-pin", () => {
    const type = typeEl.value;
    const pinName = pinNameEl.value;
    if (!type || !pinName) return;
    const cfg = ensureTypeConfig(type);
    cfg.pins[pinName] = {
      ...(cfg.pins[pinName] || {}),
      x: Math.round(Number(pinXEl.value) || 0),
      y: Math.round(Number(pinYEl.value) || 0),
    };
    applyAndRender(`已更新 ${type}.${pinName} 位置：(${cfg.pins[pinName].x}, ${cfg.pins[pinName].y})`);
  }, log);

  bindOptionalClick("btn-add-component-pin", () => {
    const type = typeEl.value;
    const pinName = newPinEl.value.trim();
    if (!type || !pinName) {
      statusEl.textContent = "请先选择器件并输入新 Pin 名称。";
      return;
    }
    const cfg = ensureTypeConfig(type);
    if (!cfg.pins[pinName]) cfg.pins[pinName] = { x: 0, y: 0 };
    pinNameEl.value = pinName;
    newPinEl.value = "";
    applyAndRender(`已新增 Pin：${type}.${pinName}`);
  }, log);

  typeEl.addEventListener("change", refreshPinList);
  typeEl.addEventListener("focus", refreshTypeList);
  pinNameEl.addEventListener("change", refreshPinList);

  refreshTypeList();
  refreshPinList();
  redrawTemplatePreview();
}

function validateVocab(vocab: any): string | null {
  const types = vocab?.types;
  if (!types || typeof types !== "object" || Array.isArray(types)) {
    return "vocab.types 缺失或格式非法";
  }
  const typeCount = Object.keys(types).length;
  if (typeCount <= 1) {
    return `vocab.types 数量不足（当前 ${typeCount}，要求 > 1）`;
  }
  return null;
}

async function loadVocab(log: (msg: string) => void): Promise<any> {
  const candidates = ["/shared/vocab.json", "/vocab.json", "../shared/vocab.json", "../../shared/vocab.json"];
  const errors: string[] = [];
  for (const url of candidates) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) {
        const reason = `HTTP ${resp.status}`;
        errors.push(`${url}: ${reason}`);
        log(`⚠️ vocab 候选加载失败：${url}（${reason}）`);
        continue;
      }
      const data = await resp.json();
      const validationError = validateVocab(data);
      if (validationError) {
        errors.push(`${url}: ${validationError}`);
        log(`⚠️ vocab 候选校验失败：${url}（${validationError}）`);
        continue;
      }
      log(`✅ vocab 加载成功：${url}（types=${Object.keys(data.types).length}）`);
      return data;
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      errors.push(`${url}: ${reason}`);
      log(`⚠️ vocab 候选加载异常：${url}（${reason}）`);
    }
  }

  const detail = errors.length ? errors.join("; ") : "无可用候选";
  throw new Error(`vocab 加载失败：${detail}`);
}
