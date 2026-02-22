import type { Label, OcclusionItem, Scene } from "./types";

type BBox = { x0: number; y0: number; x1: number; y1: number };

type ComputeLocalLabelInput = {
  scene: Scene;
  maskImageData: ImageData;
  occThreshold: number;
  functionName: string;
};

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function safeNodeBBox(node: Scene["nodes"][number]): BBox {
  const scale = Number(node.scale ?? 1);
  const s = Number.isFinite(scale) && scale > 0 ? scale : 1;
  // 近似算法：使用统一 bbox，忽略旋转
  const bw = 80 * s;
  const bh = 80 * s;
  return {
    x0: node.pos.x - bw / 2,
    y0: node.pos.y - bh / 2,
    x1: node.pos.x + bw / 2,
    y1: node.pos.y + bh / 2,
  };
}

function pixelMasked(data: Uint8ClampedArray, idx: number): boolean {
  const r = data[idx];
  const g = data[idx + 1];
  const b = data[idx + 2];
  const a = data[idx + 3];
  const luminance = (r + g + b) / 3;
  const softMaskStrength = (a / 255) * (luminance / 255);
  return softMaskStrength > 0.05;
}

function countBboxOverlap(mask: ImageData, bbox: BBox): { total: number; masked: number } {
  const w = mask.width;
  const h = mask.height;
  const x0 = clamp(Math.floor(bbox.x0), 0, w);
  const y0 = clamp(Math.floor(bbox.y0), 0, h);
  const x1 = clamp(Math.ceil(bbox.x1), 0, w);
  const y1 = clamp(Math.ceil(bbox.y1), 0, h);

  const data = mask.data;
  let total = 0;
  let masked = 0;

  for (let y = y0; y < y1; y++) {
    for (let x = x0; x < x1; x++) {
      total += 1;
      const idx = (y * w + x) * 4;
      if (pixelMasked(data, idx)) masked += 1;
    }
  }
  return { total, masked };
}

export function computeLabelLocalApprox(input: ComputeLocalLabelInput): Label {
  const { scene, maskImageData, functionName } = input;
  const occThreshold = clamp(Number(input.occThreshold) || 0.9, 0, 1);

  const countsAll: Record<string, number> = {};
  const countsVisible: Record<string, number> = {};
  const occlusion: OcclusionItem[] = [];

  for (const node of scene.nodes ?? []) {
    const type = String(node.type || "UNKNOWN");
    countsAll[type] = (countsAll[type] ?? 0) + 1;

    const bbox = safeNodeBBox(node);
    const { total, masked } = countBboxOverlap(maskImageData, bbox);
    const occRatio = total <= 0 ? 0 : clamp(masked / total, 0, 1);
    if (occRatio < occThreshold) {
      countsVisible[type] = (countsVisible[type] ?? 0) + 1;
    }
    occlusion.push({ node_id: node.id, type, occ_ratio: Number(occRatio.toFixed(4)) });
  }

  return {
    label_version: "0.3",
    counts_all: countsAll,
    counts_visible: countsVisible,
    occlusion,
    occ_threshold: occThreshold,
    function: functionName || "UNKNOWN",
    meta: {
      compute_mode: "frontend_fast",
      approximate: true,
    },
  };
}
