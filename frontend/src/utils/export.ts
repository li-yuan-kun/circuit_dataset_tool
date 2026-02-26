import type { Scene, Label } from "../modules/types";

export type CompositeExportOptions = {
  maskColor?: string;
  maskOpacity?: number;
  includeBase?: boolean;
};

type CompositeMaskSource = HTMLCanvasElement | ImageData | ImageBitmap | HTMLImageElement | Blob;

export async function exportCanvasPNG(canvas: HTMLCanvasElement): Promise<Blob> {
  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error("exportCanvasPNG: toBlob returned null"))),
      "image/png",
      1.0
    );
  });
  return blob;
}

export function exportJSON(obj: any): Blob {
  const text = JSON.stringify(obj, null, 2);
  return new Blob([text], { type: "application/json" });
}

export function makeSceneJson(scene: Scene): Blob {
  return exportJSON(scene);
}

export function makeLabelJson(label: Label): Blob {
  return exportJSON(label);
}

async function normalizeMaskToImageData(
  maskSource: CompositeMaskSource,
  width: number,
  height: number,
): Promise<ImageData> {
  if (maskSource instanceof ImageData) return maskSource;

  const tmpCanvas = document.createElement("canvas");
  tmpCanvas.width = width;
  tmpCanvas.height = height;
  const tmpCtx = tmpCanvas.getContext("2d");
  if (!tmpCtx) throw new Error("normalizeMaskToImageData: 2d context unavailable");

  if (maskSource instanceof Blob) {
    const bitmap = await createImageBitmap(maskSource);
    tmpCtx.drawImage(bitmap, 0, 0, width, height);
    bitmap.close();
  } else {
    tmpCtx.drawImage(maskSource, 0, 0, width, height);
  }
  return tmpCtx.getImageData(0, 0, width, height);
}

export async function exportCompositePNG(
  circuitCanvas: HTMLCanvasElement,
  maskCanvasOrImageData: CompositeMaskSource,
  options: CompositeExportOptions = {},
): Promise<Blob> {
  const width = circuitCanvas.width;
  const height = circuitCanvas.height;
  const outCanvas = document.createElement("canvas");
  outCanvas.width = width;
  outCanvas.height = height;
  const outCtx = outCanvas.getContext("2d");
  if (!outCtx) throw new Error("exportCompositePNG: 2d context unavailable");

  const includeBase = options.includeBase ?? true;
  const maskOpacity = Math.max(0, Math.min(1, options.maskOpacity ?? 0.45));
  const maskColor = options.maskColor ?? "#ff0000";

  if (includeBase) {
    outCtx.drawImage(circuitCanvas, 0, 0);
  }

  const maskData = await normalizeMaskToImageData(maskCanvasOrImageData, width, height);
  const { data } = maskData;
  outCtx.save();
  outCtx.fillStyle = maskColor;
  outCtx.globalAlpha = maskOpacity;

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const idx = (y * width + x) * 4;
      const alpha = data[idx + 3];
      const intensity = data[idx];
      if (alpha > 0 && intensity > 0) {
        outCtx.fillRect(x, y, 1, 1);
      }
    }
  }
  outCtx.restore();
  return exportCanvasPNG(outCanvas);
}

/** optional：需要项目依赖 jszip；没有也不影响主流程 */
export async function packZip(files: { name: string; blob: Blob }[]): Promise<Blob> {
  const mod = await import("jszip");
  const JSZip = mod.default;
  const zip = new JSZip();

  for (const f of files) {
    zip.file(f.name, f.blob);
  }
  const out = await zip.generateAsync({ type: "blob" });
  return out;
}

let _sidSeq = 1;

/** 仅用于前端建议；后端仍会校验/分配最终 sample_id */
export function suggestSampleId(prefix: string = "sample_", width: number = 6): string {
  const w = Math.max(2, Math.min(12, Math.floor(width)));
  const n = (_sidSeq++ + Math.floor(Date.now() / 1000)) % 10 ** w;
  return `${prefix}${String(n).padStart(w, "0")}`;
}
