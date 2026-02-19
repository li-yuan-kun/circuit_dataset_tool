import { ApiClient } from "./api/client";
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

function toNum(value: string, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
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
  if (err instanceof Error) return `${fallback}：${err.message}`;
  return fallback;
}

export async function bootstrapApp(): Promise<void> {
  const statusLog = byId<HTMLPreElement>("status-log");
  const labelJsonEl = byId<HTMLTextAreaElement>("label-json");
  const labelSummaryEl = byId<HTMLDivElement>("label-summary");

  const circuitCanvas = byId<HTMLCanvasElement>("circuit-canvas");
  const maskCanvas = byId<HTMLCanvasElement>("mask-canvas");

  const circuitCtx = circuitCanvas.getContext("2d");
  const maskCtx = maskCanvas.getContext("2d");
  if (!circuitCtx || !maskCtx) throw new Error("无法初始化 Canvas 2D 上下文");

  const resolution = { w: circuitCanvas.width, h: circuitCanvas.height };
  const vocab = await loadVocab();
  const engine = new CanvasEngine({ resolution, vocab });
  const maskLayer = new MaskLayer({ resolution });

  const state: AppState = {
    mode: "circuit",
    scene: engine.serializeScene(),
    label: null,
    maskBlob: null,
  };

  const log = (msg: string): void => {
    const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
    statusLog.textContent = `${line}\n${statusLog.textContent || ""}`;
  };

  const logError = (err: unknown, hint: string): void => {
    const message = userFacingError(err, hint);
    log(`❌ ${message}`);
    console.error(err);
  };

  const getApi = (): ApiClient => {
    const baseUrlEl = byId<HTMLInputElement>("api-base-url");
    const baseUrl = (baseUrlEl.value || "").trim();
    try {
      localStorage.setItem("cdt.apiBaseUrl", baseUrl);
    } catch {
      // ignore
    }
    return new ApiClient({ baseUrl, timeoutMs: 30000 });
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

  const refreshLabelUi = (): void => {
    if (!state.label) {
      labelJsonEl.value = "";
      labelSummaryEl.innerHTML = '<div class="muted">尚未生成 label。</div>';
      return;
    }
    labelJsonEl.value = JSON.stringify(state.label, null, 2);
    labelSummaryEl.innerHTML = `<div>function: ${state.label.function}</div><div>occ_threshold: ${state.label.occ_threshold}</div><div>occlusion 条目: ${state.label.occlusion?.length || 0}</div>`;
  };

  bindPalette(engine, render, log, vocab);
  bindMaskPaint(maskCanvas, maskLayer, () => state.mode === "mask", render);

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
    try {
      const functionValue = byId<HTMLInputElement>("function-custom").value.trim() || byId<HTMLSelectElement>("function-select").value;
      const occThreshold = toNum(byId<HTMLInputElement>("occ-threshold").value, 0.9);
      const maskBlob = state.maskBlob ?? (await maskLayer.exportMaskBinaryPNG());
      const { label } = await getApi().computeLabel(syncScene(), maskBlob, occThreshold, functionValue);
      state.label = label;
      refreshLabelUi();
      log("Label 计算成功");
    } catch (err) {
      logError(err, "计算 Label 失败，请确认已生成有效 Mask");
    }
  });

  byId<HTMLButtonElement>("btn-shuffle").addEventListener("click", async () => {
    try {
      const params = {
        seed: byId<HTMLInputElement>("shuffle-seed").value || undefined,
        placement: byId<HTMLSelectElement>("shuffle-placement").value,
        route_mode: byId<HTMLSelectElement>("shuffle-route").value,
        bend_mode: byId<HTMLSelectElement>("shuffle-bend").value,
        margin: toNum(byId<HTMLInputElement>("shuffle-margin").value, 20),
        max_tries: toNum(byId<HTMLInputElement>("shuffle-max-tries").value, 2000),
      };
      const returnPaths = byId<HTMLSelectElement>("shuffle-return-paths").value === "true";
      const { scene_shuffled } = await getApi().shuffleScene(syncScene(), params, returnPaths);
      engine.loadScene(scene_shuffled);
      syncScene();
      render();
      log("Shuffle 完成并已刷新画布");
    } catch (err) {
      logError(err, "Shuffle 失败，请稍后重试");
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
    log("已切换到电路编辑模式");
  });
  byId<HTMLButtonElement>("btn-mode-mask").addEventListener("click", () => {
    state.mode = "mask";
    log("已切换到 Mask 编辑模式");
  });

  syncScene();
  refreshLabelUi();
  render();
  log("应用初始化完成");
}

function bindMaskPaint(
  maskCanvas: HTMLCanvasElement,
  maskLayer: MaskLayer,
  canPaint: () => boolean,
  render: () => void
): void {
  let painting = false;

  const point = (ev: MouseEvent) => {
    const rect = maskCanvas.getBoundingClientRect();
    return {
      x: ((ev.clientX - rect.left) / rect.width) * maskCanvas.width,
      y: ((ev.clientY - rect.top) / rect.height) * maskCanvas.height,
    };
  };

  maskCanvas.addEventListener("mousedown", (ev) => {
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
      palette.innerHTML = '<div class="muted">无匹配器件类型</div>';
    }
  };

  search.addEventListener("input", redraw);
  redraw();
}

async function loadVocab(): Promise<any> {
  const candidates = ["/vocab.json", "../shared/vocab.json", "../../shared/vocab.json"];
  for (const url of candidates) {
    try {
      const resp = await fetch(url);
      if (resp.ok) return await resp.json();
    } catch {
      // continue
    }
  }
  return { types: { R: { size: { w: 90, h: 40 }, pins: [{ name: "A", x: -45, y: 0 }, { name: "B", x: 45, y: 0 }] } } };
}
