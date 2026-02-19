import type { Scene, Label } from "../modules/types";

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