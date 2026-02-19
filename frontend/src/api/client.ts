import type { Scene, Label, ApiErrorPayload } from "../modules/types";

export type ApiClientOptions = {
  /** 例如 "/api/v1"；也可传完整 URL（如 "http://127.0.0.1:8000/api/v1"） */
  baseUrl: string;
  timeoutMs?: number;
};

export class ApiError extends Error {
  public readonly status: number;
  public readonly code: string;
  public readonly details: Record<string, any>;

  constructor(status: number, code: string, message: string, details: Record<string, any> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function stripTrailingSlash(s: string) {
  return s.replace(/\/+$/, "");
}

function joinUrl(base: string, path: string) {
  const b = stripTrailingSlash(base);
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${b}${p}`;
}

async function safeReadJson(resp: Response): Promise<any | null> {
  const ct = resp.headers.get("content-type") || "";
  if (!ct.includes("application/json")) return null;
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

function parseError(status: number, body: any): ApiError {
  const fallback = new ApiError(status, "HTTP_ERROR", `Request failed (${status})`, {});
  if (!body || typeof body !== "object") return fallback;

  const maybe: ApiErrorPayload | any = body;
  if (maybe?.error?.code && maybe?.error?.message) {
    return new ApiError(status, String(maybe.error.code), String(maybe.error.message), maybe.error.details || {});
  }
  // 兼容 FastAPI ValidationError 之类的结构
  if (maybe?.detail?.error?.code && maybe?.detail?.error?.message) {
    return new ApiError(status, String(maybe.detail.error.code), String(maybe.detail.error.message), maybe.detail.error.details || {});
  }
  return fallback;
}

async function blobToBase64NoPrefix(blob: Blob): Promise<string> {
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;

  constructor(opts: ApiClientOptions) {
    this.baseUrl = stripTrailingSlash(opts.baseUrl || "");
    this.timeoutMs = Math.max(1000, opts.timeoutMs ?? 20000);
  }

  private async fetchWithTimeout(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const resp = await fetch(input, { ...init, signal: controller.signal });
      return resp;
    } finally {
      clearTimeout(id);
    }
  }

  async validateScene(scene: Scene, strict = false): Promise<{ scene_norm: Scene; warnings: string[] }> {
    const url = joinUrl(this.baseUrl, "/scene/validate");
    const resp = await this.fetchWithTimeout(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ scene, strict }),
    });

    if (!resp.ok) throw parseError(resp.status, await safeReadJson(resp));
    const json = await resp.json();
    // 兼容：{scene_norm, warnings} 或 {ok, scene_norm, warnings}
    return {
      scene_norm: json.scene_norm ?? json.scene ?? json,
      warnings: json.warnings ?? [],
    };
  }

  async generateMask(
    scene: Scene,
    strategy: string,
    params: Record<string, any> = {}
  ): Promise<{ maskPngBlob: Blob; meta: Record<string, any> }> {
    const url = joinUrl(this.baseUrl, "/mask/generate");
    const resp = await this.fetchWithTimeout(url, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "image/png,application/json" },
      body: JSON.stringify({ scene, strategy, params, return_bytes: true }),
    });

    if (!resp.ok) throw parseError(resp.status, await safeReadJson(resp));

    const ct = resp.headers.get("content-type") || "";
    // 优先：raw image/png
    if (ct.includes("image/png")) {
      const metaHeader = resp.headers.get("x-mask-meta");
      let meta: Record<string, any> = {};
      if (metaHeader) {
        try {
          meta = JSON.parse(metaHeader);
        } catch {
          meta = {};
        }
      }
      const blob = await resp.blob();
      return { maskPngBlob: blob, meta };
    }

    // 兼容：json base64
    const json = await resp.json();
    const b64: string = json.mask_png_base64;
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    return { maskPngBlob: new Blob([bytes], { type: "image/png" }), meta: json.meta || {} };
  }

  async computeLabel(
    scene: Scene,
    maskPngBlob: Blob,
    occThreshold = 0.9,
    func = "UNKNOWN"
  ): Promise<{ label: Label }> {
    const url = joinUrl(this.baseUrl, "/label/compute");
    const mask_png_base64 = await blobToBase64NoPrefix(maskPngBlob);

    const resp = await this.fetchWithTimeout(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        scene,
        mask_png_base64,
        occ_threshold: occThreshold,
        function: func,
      }),
    });

    if (!resp.ok) throw parseError(resp.status, await safeReadJson(resp));
    const json = await resp.json();
    // 兼容：{label} 或直接返回 label
    return { label: (json.label ?? json) as Label };
  }

  async shuffleScene(
    scene: Scene,
    params: Record<string, any> = {},
    returnPaths = true
  ): Promise<{ scene_shuffled: Scene; meta: any }> {
    const url = joinUrl(this.baseUrl, "/topology/shuffle");
    const resp = await this.fetchWithTimeout(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ scene, params, return_paths: returnPaths }),
    });

    if (!resp.ok) throw parseError(resp.status, await safeReadJson(resp));
    const json = await resp.json();
    return { scene_shuffled: json.scene_shuffled ?? json.scene ?? json, meta: json.meta ?? {} };
  }

  async saveSampleMultipart(payload: {
    imagePng: Blob;
    maskPng: Blob;
    sceneJson: Blob;
    labelJson: Blob;
    sampleId?: string;
  }): Promise<{ ok: boolean; sample_id: string; saved_paths: any }> {
    const url = joinUrl(this.baseUrl, "/dataset/save");
    const fd = new FormData();

    fd.append("image", payload.imagePng, "image.png");
    fd.append("mask", payload.maskPng, "mask.png");
    fd.append("scene", payload.sceneJson, "scene.json");
    fd.append("label", payload.labelJson, "label.json");
    if (payload.sampleId) fd.append("sample_id", payload.sampleId);

    const resp = await this.fetchWithTimeout(url, { method: "POST", body: fd });

    if (!resp.ok) throw parseError(resp.status, await safeReadJson(resp));
    const json = await resp.json();
    return {
      ok: Boolean(json.ok),
      sample_id: String(json.sample_id),
      saved_paths: json.saved_paths ?? json.savedPaths ?? {},
    };
  }
}