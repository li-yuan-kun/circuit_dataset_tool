import type { Scene, Endpoint, Point, Resolution, Node, Net } from "./models/types";

export type CanvasEngineOptions = { resolution: Resolution; vocab: any };

function deepCopy<T>(obj: T): T {
  // structuredClone 在现代浏览器可用；否则退回 JSON
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const sc = (globalThis as any).structuredClone;
  if (typeof sc === "function") return sc(obj);
  return JSON.parse(JSON.stringify(obj));
}

function nowIso(): string {
  return new Date().toISOString();
}

function safeSizeFromVocab(vocab: any, type: string): { w: number; h: number } {
  try {
    const t = vocab?.types?.[type];
    const s = t?.size ?? t;
    const w = Number(s?.w ?? 80);
    const h = Number(s?.h ?? 80);
    if (Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) return { w, h };
  } catch {
    // ignore
  }
  return { w: 80, h: 80 };
}

function iterPinsFromVocab(vocab: any, type: string): Array<{ name: string; x: number; y: number }> {
  const out: Array<{ name: string; x: number; y: number }> = [];
  const t = vocab?.types?.[type];
  const pins = t?.pins;
  if (!pins) return out;

  // dict: {p0:{x,y}, ...}
  if (typeof pins === "object" && !Array.isArray(pins)) {
    for (const [k, v] of Object.entries(pins)) {
      const vv: any = v;
      if (vv && typeof vv === "object" && "x" in vv && "y" in vv) {
        out.push({ name: String(k), x: Number(vv.x), y: Number(vv.y) });
      }
    }
    return out;
  }

  // list: [{name,x,y}, ...]
  if (Array.isArray(pins)) {
    for (const it of pins) {
      if (!it || typeof it !== "object") continue;
      const name = String((it as any).name ?? (it as any).id ?? (it as any).pin ?? "");
      if (!name) continue;
      const x = Number((it as any).x ?? 0);
      const y = Number((it as any).y ?? 0);
      if (Number.isFinite(x) && Number.isFinite(y)) out.push({ name, x, y });
    }
  }
  return out;
}

// best-effort: treat small magnitude as rad, else deg
function rotToRad(rot?: number): number {
  const r = Number(rot ?? 0);
  if (Math.abs(r) <= 6.6) return r;
  return (r * Math.PI) / 180;
}

function rotateXY(x: number, y: number, rad: number): { x: number; y: number } {
  const c = Math.cos(rad);
  const s = Math.sin(rad);
  return { x: x * c - y * s, y: x * s + y * c };
}

export class CanvasEngine {
  private readonly resolution: Resolution;
  private readonly vocab: any;

  private scene: Scene;
  private sel: { nodeId?: string; netId?: string } | null = null;

  private nodeSeq = 1;
  private netSeq = 1;

  constructor(opts: CanvasEngineOptions) {
    this.resolution = opts.resolution;
    this.vocab = opts.vocab;

    this.scene = this.makeEmptyScene();
  }

  private makeEmptyScene(): Scene {
    const vocabVersion = this.vocab?.vocab_version ?? this.vocab?.version ?? this.vocab?.meta?.vocab_version ?? null;
    return {
      meta: {
        scene_version: "0.3",
        tool_version: "0.3",
        vocab_version: vocabVersion ? String(vocabVersion) : null,
        seed: Math.floor(Math.random() * 2 ** 31),
        resolution: { ...this.resolution },
        params: {},
        timestamp: nowIso(),
      },
      nodes: [],
      nets: [],
    };
  }

  loadScene(scene: Scene): void {
    this.scene = deepCopy(scene);

    // 补齐 meta.resolution
    this.scene.meta = this.scene.meta || {};
    if (!this.scene.meta.resolution) this.scene.meta.resolution = { ...this.resolution };

    // 维护序列号，避免新增 id 冲突
    for (const n of this.scene.nodes) {
      const m = /^n(\d+)$/.exec(n.id);
      if (m) this.nodeSeq = Math.max(this.nodeSeq, Number(m[1]) + 1);
    }
    for (const e of this.scene.nets) {
      const m = /^e(\d+)$/.exec(e.id);
      if (m) this.netSeq = Math.max(this.netSeq, Number(m[1]) + 1);
    }
  }

  serializeScene(): Scene {
    // 导出前刷新 timestamp
    const s = deepCopy(this.scene);
    s.meta = s.meta || {};
    s.meta.timestamp = nowIso();
    s.meta.resolution = s.meta.resolution || { ...this.resolution };
    return s;
  }

  clear(): void {
    this.scene = this.makeEmptyScene();
    this.sel = null;
  }

  addNode(type: string, pos: Point): string {
    const id = `n${this.nodeSeq++}`;
    const node: Node = { id, type, pos: { ...pos }, rot: 0, scale: 1 };
    this.scene.nodes.push(node);
    return id;
  }

  removeNode(nodeId: string): void {
    this.scene.nodes = this.scene.nodes.filter((n) => n.id !== nodeId);
    this.scene.nets = this.scene.nets.filter((e) => e.from.node !== nodeId && e.to.node !== nodeId);
    if (this.sel?.nodeId === nodeId) this.sel = null;
  }

  moveNode(nodeId: string, pos: Point): void {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n.pos = { ...pos };
  }

  rotateNode(nodeId: string, rot: number): void {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n.rot = rot;
  }

  scaleNode(nodeId: string, scale: number): void {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n.scale = Math.max(0.05, Number(scale));
  }

  connectPins(from: Endpoint, to: Endpoint): string {
    if (!from?.node || !from?.pin || !to?.node || !to?.pin) {
      throw new Error("connectPins: invalid endpoints");
    }
    if (from.node === to.node && from.pin === to.pin) {
      throw new Error("connectPins: cannot connect a pin to itself");
    }

    const id = `e${this.netSeq++}`;
    const net: Net = { id, from: { ...from }, to: { ...to }, path: [] };
    this.scene.nets.push(net);
    // 立即生成一条简单路径，便于显示
    net.path = this.computeDefaultNetPath(net);
    return id;
  }

  removeNet(netId: string): void {
    this.scene.nets = this.scene.nets.filter((e) => e.id !== netId);
    if (this.sel?.netId === netId) this.sel = null;
  }

  setSelection(sel: { nodeId?: string; netId?: string } | null): void {
    this.sel = sel ? { ...sel } : null;
  }

  hitTestNode(p: Point): string | null {
    const nodes = this.scene.nodes;
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      const { w, h } = safeSizeFromVocab(this.vocab, n.type);
      const s = Number(n.scale ?? 1);
      const bw = w * s;
      const bh = h * s;
      const x0 = n.pos.x - bw / 2;
      const y0 = n.pos.y - bh / 2;
      if (p.x >= x0 && p.x <= x0 + bw && p.y >= y0 && p.y <= y0 + bh) return n.id;
    }
    return null;
  }

  hitTestPin(p: Point): Endpoint | null {
    const pinRadius = 8;
    const nodes = this.scene.nodes;

    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      const pins = iterPinsFromVocab(this.vocab, n.type);
      if (pins.length === 0) continue;

      const rad = rotToRad(n.rot);
      const s = Number(n.scale ?? 1);

      for (const pin of pins) {
        // pin.x/y 约定为相对组件中心的局部坐标
        const r = rotateXY(pin.x * s, pin.y * s, rad);
        const gx = n.pos.x + r.x;
        const gy = n.pos.y + r.y;
        const dx = p.x - gx;
        const dy = p.y - gy;
        if (dx * dx + dy * dy <= pinRadius * pinRadius) {
          return { node: n.id, pin: pin.name };
        }
      }
    }
    return null;
  }

  private endpointXY(ep: Endpoint): Point {
    const n = this.scene.nodes.find((x) => x.id === ep.node);
    if (!n) return { x: 0, y: 0 };

    const pins = iterPinsFromVocab(this.vocab, n.type);
    const pin = pins.find((pp) => pp.name === ep.pin);
    if (!pin) return { x: n.pos.x, y: n.pos.y };

    const rad = rotToRad(n.rot);
    const s = Number(n.scale ?? 1);
    const r = rotateXY(pin.x * s, pin.y * s, rad);
    return { x: n.pos.x + r.x, y: n.pos.y + r.y };
  }

  private computeDefaultNetPath(net: Net): Point[] {
    const p0 = this.endpointXY(net.from);
    const p1 = this.endpointXY(net.to);
    // 简单两段折线：hv
    return [{ ...p0 }, { x: p1.x, y: p0.y }, { ...p1 }];
  }

  draw(ctx: CanvasRenderingContext2D): void {
    const W = this.resolution.w;
    const H = this.resolution.h;

    // clear
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, W, H);
    ctx.restore();

    // nets
    for (const e of this.scene.nets) {
      const isSel = this.sel?.netId === e.id;
      const path = (e.path && e.path.length >= 2) ? e.path : this.computeDefaultNetPath(e);

      ctx.save();
      ctx.lineWidth = isSel ? 3 : 2;
      ctx.strokeStyle = isSel ? "#1e88e5" : "#666";
      ctx.beginPath();
      ctx.moveTo(path[0].x, path[0].y);
      for (let i = 1; i < path.length; i++) ctx.lineTo(path[i].x, path[i].y);
      ctx.stroke();
      ctx.restore();
    }

    // nodes
    for (const n of this.scene.nodes) {
      const { w, h } = safeSizeFromVocab(this.vocab, n.type);
      const s = Number(n.scale ?? 1);
      const bw = w * s;
      const bh = h * s;

      const isSel = this.sel?.nodeId === n.id;

      ctx.save();
      ctx.translate(n.pos.x, n.pos.y);
      ctx.rotate(rotToRad(n.rot));
      ctx.fillStyle = "#f6f6f6";
      ctx.strokeStyle = isSel ? "#1e88e5" : "#333";
      ctx.lineWidth = isSel ? 3 : 2;

      ctx.beginPath();
      ctx.rect(-bw / 2, -bh / 2, bw, bh);
      ctx.fill();
      ctx.stroke();

      // type text
      ctx.fillStyle = "#111";
      ctx.font = "14px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(n.type, 0, 0);

      // pins
      const pins = iterPinsFromVocab(this.vocab, n.type);
      if (pins.length) {
        ctx.fillStyle = "#111";
        for (const p of pins) {
          ctx.beginPath();
          ctx.arc(p.x * s, p.y * s, 3, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      ctx.restore();
    }
  }
}