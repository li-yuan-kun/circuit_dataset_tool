import type { Point, Resolution } from "./modules/types";

export type MaskLayerOptions = { resolution: Resolution };

function makeOffscreenCanvas(w: number, h: number): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  return c;
}

function lerp(a: number, b: number, t: number) {
  return a + (b - a) * t;
}

export class MaskLayer {
  private readonly resolution: Resolution;
  private readonly canvas: HTMLCanvasElement;
  private readonly ctx: CanvasRenderingContext2D;

  private brushWidth = 24;
  private hardness = 0.7; // [0..1]
  private isErase = false;

  private brushStamp: HTMLCanvasElement | null = null;
  private brushStampRadius = 12;

  private strokePts: Point[] = [];
  private hasStroke = false;

  constructor(opts: MaskLayerOptions) {
    this.resolution = opts.resolution;
    this.canvas = makeOffscreenCanvas(this.resolution.w, this.resolution.h);
    const ctx = this.canvas.getContext("2d");
    if (!ctx) throw new Error("MaskLayer: cannot get 2d context");
    this.ctx = ctx;

    this.clear();
    this.setBrush(this.brushWidth, this.hardness);
  }

  clear(): void {
    this.ctx.save();
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.globalCompositeOperation = "source-over";
    this.ctx.clearRect(0, 0, this.resolution.w, this.resolution.h);
    // 透明背景：未遮挡
    this.ctx.restore();
  }

  setBrush(width: number, hardness: number = this.hardness): void {
    this.brushWidth = Math.max(1, Number(width));
    this.hardness = Math.max(0, Math.min(1, Number(hardness)));

    const r = this.brushWidth / 2;
    this.brushStampRadius = r;

    const size = Math.max(2, Math.ceil(r * 2));
    const stamp = makeOffscreenCanvas(size, size);
    const sctx = stamp.getContext("2d");
    if (!sctx) {
      this.brushStamp = null;
      return;
    }

    // 生成一个“软边圆形”alpha stamp：中心 1 -> 边缘 0
    const cx = size / 2;
    const cy = size / 2;
    const grad = sctx.createRadialGradient(cx, cy, r * this.hardness, cx, cy, r);
    grad.addColorStop(0, "rgba(255,255,255,1)");
    grad.addColorStop(1, "rgba(255,255,255,0)");

    sctx.clearRect(0, 0, size, size);
    sctx.fillStyle = grad;
    sctx.beginPath();
    sctx.arc(cx, cy, r, 0, Math.PI * 2);
    sctx.fill();

    this.brushStamp = stamp;
  }

  setEraseMode(isErase: boolean): void {
    this.isErase = Boolean(isErase);
  }

  beginStroke(p: Point): void {
    this.strokePts = [{ ...p }];
    this.hasStroke = true;
    this.stampPoint(p);
  }

  addStrokePoint(p: Point): void {
    if (!this.hasStroke) return;
    const last = this.strokePts[this.strokePts.length - 1];
    this.strokePts.push({ ...p });
    this.stampSegment(last, p);
  }

  endStroke(): void {
    this.hasStroke = false;
    this.strokePts = [];
  }

  paintStroke(points: Point[]): void {
    this.setEraseMode(false);
    if (!points?.length) return;
    this.beginStroke(points[0]);
    for (let i = 1; i < points.length; i++) this.addStrokePoint(points[i]);
    this.endStroke();
  }

  eraseStroke(points: Point[]): void {
    this.setEraseMode(true);
    if (!points?.length) return;
    this.beginStroke(points[0]);
    for (let i = 1; i < points.length; i++) this.addStrokePoint(points[i]);
    this.endStroke();
  }

  private stampPoint(p: Point): void {
    const r = this.brushStampRadius;
    this.ctx.save();
    this.ctx.globalCompositeOperation = this.isErase ? "destination-out" : "source-over";

    if (this.brushStamp) {
      this.ctx.drawImage(this.brushStamp, p.x - r, p.y - r);
    } else {
      // fallback：硬圆
      this.ctx.fillStyle = "rgba(255,255,255,1)";
      this.ctx.beginPath();
      this.ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      this.ctx.fill();
    }
    this.ctx.restore();
  }

  private stampSegment(a: Point, b: Point): void {
    const r = this.brushStampRadius;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.hypot(dx, dy);
    if (dist < 1e-6) {
      this.stampPoint(b);
      return;
    }

    // 步长：半径的一半，保证连续
    const step = Math.max(1, r * 0.5);
    const n = Math.max(1, Math.ceil(dist / step));
    for (let i = 1; i <= n; i++) {
      const t = i / n;
      this.stampPoint({ x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) });
    }
  }

  drawOverlay(ctx: CanvasRenderingContext2D, alpha: number = 0.35): void {
    const a = Math.max(0, Math.min(1, alpha));
    const W = this.resolution.w;
    const H = this.resolution.h;

    // 将 mask（白色 alpha）tint 成红色半透明叠加
    ctx.save();
    ctx.globalAlpha = a;

    // 先把 mask alpha 画上去
    ctx.drawImage(this.canvas, 0, 0);

    // 再用 source-in 填充红色：只在 mask 有 alpha 的地方着色
    ctx.globalCompositeOperation = "source-in";
    ctx.fillStyle = "rgba(255,0,0,1)";
    ctx.fillRect(0, 0, W, H);

    ctx.restore();
  }

  async exportMaskBinaryPNG(): Promise<Blob> {
    const W = this.resolution.w;
    const H = this.resolution.h;

    const src = this.ctx.getImageData(0, 0, W, H);
    const outCanvas = makeOffscreenCanvas(W, H);
    const octx = outCanvas.getContext("2d");
    if (!octx) throw new Error("exportMaskBinaryPNG: cannot get context");

    const out = octx.createImageData(W, H);
    const s = src.data;
    const d = out.data;

    // 本层使用 alpha 表示遮挡，导出为二值 0/255 的灰度 PNG
    for (let i = 0; i < W * H; i++) {
      const a = s[i * 4 + 3]; // alpha
      const v = a > 0 ? 255 : 0;
      d[i * 4 + 0] = v;
      d[i * 4 + 1] = v;
      d[i * 4 + 2] = v;
      d[i * 4 + 3] = 255;
    }

    octx.putImageData(out, 0, 0);

    const blob = await new Promise<Blob>((resolve, reject) => {
      outCanvas.toBlob(
        (b) => (b ? resolve(b) : reject(new Error("toBlob returned null"))),
        "image/png",
        1.0
      );
    });
    return blob;
  }

  async importMaskBlob(blob: Blob): Promise<void> {
    const bmp = await createImageBitmap(blob);
    this.ctx.save();
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.clearRect(0, 0, this.resolution.w, this.resolution.h);
    this.ctx.drawImage(bmp, 0, 0, this.resolution.w, this.resolution.h);
    this.ctx.restore();
    bmp.close();
  }
}
