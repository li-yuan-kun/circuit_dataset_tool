import { ApiClient, ApiError } from "./backend_client";
import { CanvasEngine } from "./canvas_engine";
import { MaskLayer } from "./make_layer";
import type { Label, Scene } from "./modules/types";
import { exportCanvasPNG, makeLabelJson, makeSceneJson, suggestSampleId } from "./utils/export";

type EditorMode = "circuit" | "mask";

type AppState = {
  mode: EditorMode;
  scene: Scene;
  label: Label | null;
  maskBlob: Blob | null;
};

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
  const vocab = await loadVocab(log);
  const engine = new CanvasEngine({ resolution, vocab });
  const maskLayer = new MaskLayer({ resolution });

  const state: AppState = {
    mode: "circuit",
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
    const baseUrl = (baseUrlEl.value || "").trim();
    const timeoutMs = readApiTimeoutMs();
    try {
      localStorage.setItem("cdt.apiBaseUrl", baseUrl);
    } catch {
      // ignore
    }
    return new ApiClient({ baseUrl, timeoutMs });
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

  bindPalette(engine, render, log, vocab);
  bindMaskPaint(uiCanvas, maskLayer, () => state.mode === "mask", render);

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
    onChange: syncScene,
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
  bindOptionalClick("btn-export-local", () => {
    log("本地样本包导出功能暂未接入，已跳过");
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
    try {
      const apiBase = byId<HTMLInputElement>("api-base-url").value.trim().replace(/\/$/, "");
      const resp = await fetch(`${apiBase}/healthz`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      log("Health 检查成功");
    } catch (err) {
      logError(err, "Health 检查失败");
    }
  });

  byId<HTMLButtonElement>("btn-generate-mask").addEventListener("click", async () => {
    try {
      const strategy = byId<HTMLSelectElement>("mask-strategy").value;
      const params = {
        ratio: toNum(byId<HTMLInputElement>("mask-ratio").value, 0.35),
        scale: toNum(byId<HTMLInputElement>("mask-scale").value, 64),
        seed: byId<HTMLInputElement>("mask-seed").value || undefined,
      };
      const { maskPngBlob } = await getApi().generateMask(syncScene(), strategy, params);
      await maskLayer.importMaskBlob(maskPngBlob);
      state.maskBlob = maskPngBlob;
      render();
      log("已调用后端自动生成 Mask");
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
    const endpoint = "/label/compute";
    const startedAt = performance.now();
    try {
      const functionValue = byId<HTMLInputElement>("function-custom").value.trim() || byId<HTMLSelectElement>("function-select").value;
      const occThreshold = toNum(byId<HTMLInputElement>("occ-threshold").value, 0.9);
      const maskBlob = state.maskBlob ?? (await maskLayer.exportMaskBinaryPNG());
      const startedAt = performance.now();
      const { label } = await getApi().computeLabel(syncScene(), maskBlob, occThreshold, functionValue);
      state.label = label;
      refreshLabelUi();
      updateRequestSummary(endpoint, performance.now() - startedAt, "HTTP 200");
      log("Label 计算成功");
    } catch (err) {
      const elapsed = performance.now() - startedAt;
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
      const imagePng = await exportCanvasPNG(circuitCanvas);
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
}

function bindMaskPaint(
  interactionCanvas: HTMLCanvasElement,
  maskLayer: MaskLayer,
  canPaint: () => boolean,
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
    render();
  });
  window.addEventListener("mousemove", (ev) => {
    if (!painting) return;
    maskLayer.addStrokePoint(point(ev));
    render();
  });
  window.addEventListener("mouseup", () => {
    if (!painting) return;
    painting = false;
    maskLayer.endStroke();
    render();
  });
}

function bindPalette(engine: CanvasEngine, render: () => void, log: (msg: string) => void, vocab: any): void {
  const palette = byId<HTMLDivElement>("palette-list");
  const search = byId<HTMLInputElement>("palette-search");
  const types = Object.keys(vocab?.types ?? {});

  const redraw = () => {
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
