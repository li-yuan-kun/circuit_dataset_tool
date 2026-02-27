import type { Scene, Endpoint, Point, Resolution, Node, Net } from "./modules/types";

export type CanvasEngineOptions = { resolution: Resolution; vocab: any };
export type NodeRenderMode = "symbol" | "box";
export type NodeRenderOptions = {
  mode?: NodeRenderMode;
  strokeScale?: number;
  netStrokeScale?: number;
  showTypeLabelOnSymbol?: boolean;
};

type CustomSymbol = { img: HTMLImageElement };

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

function drawResistorSymbol(ctx: CanvasRenderingContext2D, w: number): void {
  const start = -w / 2;
  const end = w / 2;
  const bodyW = Math.min(w, Math.max(30, w * 0.62));
  const bodyStart = -bodyW / 2;
  const bodyEnd = bodyW / 2;
  const zigCount = 7;
  const step = (bodyEnd - bodyStart) / zigCount;
  const amp = Math.max(6, Math.min(11, bodyW * 0.16));

  ctx.beginPath();
  ctx.moveTo(start, 0);
  ctx.lineTo(bodyStart, 0);
  for (let i = 1; i <= zigCount; i++) {
    const x = bodyStart + step * i;
    const y = i === zigCount ? 0 : i % 2 === 1 ? -amp : amp;
    ctx.lineTo(x, y);
  }
  ctx.lineTo(end, 0);
  ctx.stroke();
}

function drawCapacitorSymbol(ctx: CanvasRenderingContext2D, w: number, h: number): void {
  const start = -w / 2;
  const end = w / 2;
  const bodyW = Math.min(w, Math.max(30, Math.min(w, h) * 0.9));
  const plateGap = Math.max(8, bodyW * 0.14);
  const leftPlateX = -plateGap;
  const rightPlateX = plateGap;
  const plateHalf = Math.max(14, Math.min(h, bodyW) * 0.42);

  ctx.beginPath();
  ctx.moveTo(start, 0);
  ctx.lineTo(leftPlateX, 0);
  ctx.moveTo(rightPlateX, 0);
  ctx.lineTo(end, 0);
  ctx.moveTo(leftPlateX, -plateHalf);
  ctx.lineTo(leftPlateX, plateHalf);
  ctx.moveTo(rightPlateX, -plateHalf);
  ctx.lineTo(rightPlateX, plateHalf);
  ctx.stroke();
}

function drawNotSymbol(ctx: CanvasRenderingContext2D, w: number, h: number): void {
  const left = -w / 2;
  const right = w / 2;
  const top = -h / 2;
  const bottom = h / 2;
  const bodyW = Math.min(w, h * 0.95);
  const tipX = bodyW / 2 - 8;
  const bubbleR = 5.5;
  const bodyLeft = -bodyW / 2;

  ctx.beginPath();
  ctx.moveTo(left, 0);
  ctx.lineTo(bodyLeft, 0);
  ctx.moveTo(tipX + bubbleR * 2, 0);
  ctx.lineTo(right, 0);
  ctx.stroke();

  ctx.beginPath();
  ctx.moveTo(bodyLeft, top);
  ctx.lineTo(bodyLeft, bottom);
  ctx.lineTo(tipX, 0);
  ctx.closePath();
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(tipX + bubbleR, 0, bubbleR, 0, Math.PI * 2);
  ctx.stroke();
}

function drawAndFamilySymbol(ctx: CanvasRenderingContext2D, w: number, h: number, bubble = false): void {
  const left = -w / 2;
  const right = w / 2;
  const top = -h / 2;
  const bottom = h / 2;
  const bubbleR = 5.5;
  const radius = h * 0.5;
  const arcRight = bubble ? right - bubbleR * 2 : right;
  const bodyRight = arcRight - radius;
  const bodyLeft = left + Math.max(14, w * 0.2);
  const centerY = 0;

  ctx.beginPath();
  ctx.moveTo(left, -h * 0.22);
  ctx.lineTo(bodyLeft, -h * 0.22);
  ctx.moveTo(left, h * 0.22);
  ctx.lineTo(bodyLeft, h * 0.22);
  ctx.stroke();

  ctx.beginPath();
  ctx.moveTo(bodyLeft, top);
  ctx.lineTo(bodyRight, top);
  ctx.arc(bodyRight, centerY, radius, -Math.PI / 2, Math.PI / 2);
  ctx.lineTo(bodyLeft, bottom);
  ctx.closePath();
  ctx.stroke();

  if (bubble) {
    ctx.beginPath();
    ctx.arc(right - bubbleR, 0, bubbleR, 0, Math.PI * 2);
    ctx.stroke();
  }
}

function drawOrFamilySymbol(ctx: CanvasRenderingContext2D, w: number, h: number, opts: { xor?: boolean; bubble?: boolean } = {}): void {
  const left = -w / 2;
  const right = w / 2;
  const top = -h / 2;
  const bottom = h / 2;
  const bubbleR = 5.5;
  const gateOutX = opts.bubble ? right - bubbleR * 2 : right;
  const bodyW = Math.max(22, Math.min(w, h) * 0.7);
  const inJoinX = left + Math.max(14, w * 0.2);

  // leads
  ctx.beginPath();
  ctx.moveTo(left, -h * 0.22);
  ctx.lineTo(inJoinX, -h * 0.22);
  ctx.moveTo(left, h * 0.22);
  ctx.lineTo(inJoinX, h * 0.22);
  ctx.stroke();

  // ANSI-like OR body
  ctx.beginPath();
  ctx.moveTo(inJoinX, top);
  const bulgeX = inJoinX + bodyW * 0.7;
  const backX = inJoinX + bodyW * 0.32;
  ctx.quadraticCurveTo(backX, 0, inJoinX, bottom);
  ctx.quadraticCurveTo(bulgeX, bottom, gateOutX, 0);
  ctx.quadraticCurveTo(bulgeX, top, inJoinX, top);
  ctx.stroke();

  if (opts.xor) {
    ctx.beginPath();
    ctx.moveTo(inJoinX - 7, top);
    ctx.quadraticCurveTo(backX - 7, 0, inJoinX - 7, bottom);
    ctx.stroke();
  }

  if (opts.bubble) {
    ctx.beginPath();
    ctx.arc(gateOutX + bubbleR, 0, bubbleR, 0, Math.PI * 2);
    ctx.stroke();
  }
}

function drawComparatorSymbol(ctx: CanvasRenderingContext2D, w: number, h: number): void {
  const left = -w / 2;
  const right = w / 2;
  const top = -h / 2;
  const bottom = h / 2;
  const bodyW = Math.min(w, h);
  const bodyLeft = -bodyW / 2 + 4;
  const bodyRight = bodyW / 2 - 4;

  ctx.beginPath();
  ctx.moveTo(left, -h * 0.23);
  ctx.lineTo(bodyLeft, -h * 0.23);
  ctx.moveTo(left, h * 0.23);
  ctx.lineTo(bodyLeft, h * 0.23);
  ctx.moveTo(bodyRight, 0);
  ctx.lineTo(right, 0);
  ctx.stroke();

  ctx.beginPath();
  ctx.moveTo(bodyLeft, top);
  ctx.lineTo(bodyLeft, bottom);
  ctx.lineTo(bodyRight, 0);
  ctx.closePath();
  ctx.stroke();

  ctx.font = "12px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#ff0000";
  ctx.fillText("+", bodyLeft + 12, -h * 0.2);
  ctx.fillText("−", bodyLeft + 12, h * 0.2);
}

function drawGroundSymbol(ctx: CanvasRenderingContext2D, w: number, h: number): void {
  const left = -w / 2;
  const right = w / 2;
  const top = -h / 2;
  const y0 = h * 0.02;
  const step = Math.max(10, h * 0.16);
  ctx.beginPath();
  ctx.moveTo(0, top);
  ctx.lineTo(0, y0);
  ctx.moveTo(left * 0.46, y0);
  ctx.lineTo(right * 0.46, y0);
  ctx.moveTo(left * 0.3, y0 + step);
  ctx.lineTo(right * 0.3, y0 + step);
  ctx.moveTo(left * 0.16, y0 + step * 2);
  ctx.lineTo(right * 0.16, y0 + step * 2);
  ctx.stroke();
}

function drawSourceSymbol(ctx: CanvasRenderingContext2D, w: number, h: number): void {
  const r = Math.max(12, Math.min(w, h) * 0.3);
  ctx.beginPath();
  ctx.moveTo(-w / 2, 0);
  ctx.lineTo(-r, 0);
  ctx.moveTo(r, 0);
  ctx.lineTo(w / 2, 0);
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.stroke();

  ctx.beginPath();
  ctx.moveTo(-r * 0.5, 0);
  ctx.quadraticCurveTo(-r * 0.25, -r * 0.35, 0, 0);
  ctx.quadraticCurveTo(r * 0.25, r * 0.35, r * 0.5, 0);
  ctx.stroke();
}

function drawComponentSymbol(ctx: CanvasRenderingContext2D, type: string, w: number, h: number): boolean {
  switch (String(type).toUpperCase()) {
    case "R":
    case "RESISTOR":
      drawResistorSymbol(ctx, w);
      return true;
    case "C":
    case "CAPACITOR":
      drawCapacitorSymbol(ctx, w, h);
      return true;
    case "NOT":
    case "INV":
    case "INVERTER":
      drawNotSymbol(ctx, w, h);
      return true;
    case "AND":
      drawAndFamilySymbol(ctx, w, h, false);
      return true;
    case "NAND":
      drawAndFamilySymbol(ctx, w, h, true);
      return true;
    case "OR":
      drawOrFamilySymbol(ctx, w, h);
      return true;
    case "NOR":
      drawOrFamilySymbol(ctx, w, h, { bubble: true });
      return true;
    case "XOR":
      drawOrFamilySymbol(ctx, w, h, { xor: true });
      return true;
    case "XNOR":
      drawOrFamilySymbol(ctx, w, h, { xor: true, bubble: true });
      return true;
    case "COMPARATOR":
      drawComparatorSymbol(ctx, w, h);
      return true;
    case "GND":
    case "GROUND":
      drawGroundSymbol(ctx, w, h);
      return true;
    case "V":
    case "VSOURCE":
    case "VCC":
      drawSourceSymbol(ctx, w, h);
      return true;
    default:
      return false;
  }
}

type BBox = { x0: number; y0: number; x1: number; y1: number };

function makeSeededRandom(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6D2B79F5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t ^= t + Math.imul(t ^ (t >>> 7), 61 | t);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class CanvasEngine {
  private resolution: Resolution;
  private readonly vocab: any;

  private scene: Scene;
  private sel: { nodeId?: string; netId?: string } | null = null;

  private nodeSeq = 1;
  private netSeq = 1;
  private readonly pathCache = new Map<string, { hash: string; path: Point[]; failed: boolean }>();
  private pendingPathRefreshNodes = new Set<string>();
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private nodeRenderMode: NodeRenderMode = "symbol";
  private nodeStrokeScale = 1;
  private netStrokeScale = 1;
  private showTypeLabelOnSymbol = false;
  private readonly customSymbols = new Map<string, CustomSymbol>();

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

  setResolution(resolution: Resolution): void {
    const w = Math.max(1, Math.floor(Number(resolution?.w ?? this.resolution.w)));
    const h = Math.max(1, Math.floor(Number(resolution?.h ?? this.resolution.h)));
    this.resolution = { w, h };
    this.scene.meta = this.scene.meta || {};
    this.scene.meta.resolution = { w, h };
  }


  setNodeRenderOptions(opts: NodeRenderOptions): void {
    if (opts.mode === "symbol" || opts.mode === "box") this.nodeRenderMode = opts.mode;
    if (Number.isFinite(opts.strokeScale)) this.nodeStrokeScale = Math.max(0.5, Math.min(3, Number(opts.strokeScale)));
    if (Number.isFinite(opts.netStrokeScale)) this.netStrokeScale = Math.max(0.5, Math.min(4, Number(opts.netStrokeScale)));
    if (typeof opts.showTypeLabelOnSymbol === "boolean") this.showTypeLabelOnSymbol = opts.showTypeLabelOnSymbol;
  }

  setCustomSymbol(type: string, img: HTMLImageElement): void {
    if (!type) return;
    this.customSymbols.set(String(type), { img });
  }

  clearCustomSymbol(type: string): void {
    if (!type) return;
    this.customSymbols.delete(String(type));
  }

  hasCustomSymbol(type: string): boolean {
    return this.customSymbols.has(String(type));
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
    this.scheduleRefreshNetPathsForNode(nodeId);
  }

  rotateNode(nodeId: string, rot: number): void {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n.rot = rot;
    this.scheduleRefreshNetPathsForNode(nodeId);
  }

  scaleNode(nodeId: string, scale: number): void {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n.scale = Math.max(0.05, Number(scale));
    this.scheduleRefreshNetPathsForNode(nodeId);
  }

  private refreshNetPathsForNode(nodeId: string): void {
    this.pathCache.clear();
    for (const net of this.scene.nets) {
      if (net.from.node !== nodeId && net.to.node !== nodeId) continue;
      net.path = this.computeDefaultNetPath(net);
    }
  }

  private scheduleRefreshNetPathsForNode(nodeId: string): void {
    this.pendingPathRefreshNodes.add(nodeId);
    if (this.refreshTimer) return;
    this.refreshTimer = setTimeout(() => {
      const changed = new Set(this.pendingPathRefreshNodes);
      this.pendingPathRefreshNodes.clear();
      this.refreshTimer = null;
      this.pathCache.clear();
      for (const net of this.scene.nets) {
        if (!changed.has(net.from.node) && !changed.has(net.to.node)) continue;
        net.path = this.computeDefaultNetPath(net);
      }
    }, 16);
  }

  connectPins(from: Endpoint, to: Endpoint): { id: string; replacedOld: boolean } {
    if (!from?.node || !from?.pin || !to?.node || !to?.pin) {
      throw new Error("connectPins: invalid endpoints");
    }
    if (from.node === to.node && from.pin === to.pin) {
      throw new Error("connectPins: cannot connect a pin to itself");
    }

    const isSameEndpoint = (a: Endpoint, b: Endpoint): boolean => a.node === b.node && a.pin === b.pin;
    const isSameConnection = (net: Net): boolean => {
      const sameDirection = isSameEndpoint(net.from, from) && isSameEndpoint(net.to, to);
      const reverseDirection = isSameEndpoint(net.from, to) && isSameEndpoint(net.to, from);
      return sameDirection || reverseDirection;
    };

    const existingNetIds = this.scene.nets.filter((net) => isSameConnection(net)).map((net) => net.id);
    const replacedOld = existingNetIds.length > 0;
    if (replacedOld) {
      this.scene.nets = this.scene.nets.filter((net) => !existingNetIds.includes(net.id));
      if (this.sel?.netId && existingNetIds.includes(this.sel.netId)) this.sel = null;
    }

    const id = `e${this.netSeq++}`;
    const net: Net = { id, from: { ...from }, to: { ...to }, path: [] };
    this.scene.nets.push(net);
    // 立即生成一条简单路径，便于显示
    net.path = this.computeDefaultNetPath(net);
    return { id, replacedOld };
  }

  removeNet(netId: string): void {
    this.scene.nets = this.scene.nets.filter((e) => e.id !== netId);
    if (this.sel?.netId === netId) this.sel = null;
  }

  setSelection(sel: { nodeId?: string; netId?: string } | null): void {
    this.sel = sel ? { ...sel } : null;
  }
  getNodeById(nodeId: string): Node | null {
    return this.scene.nodes.find((x) => x.id === nodeId) ?? null;
  }

  getSelection(): { nodeId?: string; netId?: string } | null {
    return this.sel ? { ...this.sel } : null;
  }

  endpointPosition(ep: Endpoint): Point {
    return this.endpointXY(ep);
  }

  shuffleNodePositions(seed?: number, margin = 20): void {
    const nodes = this.scene.nodes;
    if (!nodes.length) {
      this.scene.meta = this.scene.meta || {};
      this.scene.meta.seed = Number.isFinite(seed) ? Number(seed) : Math.floor(Math.random() * 2 ** 31);
      return;
    }

    const W = this.resolution.w;
    const H = this.resolution.h;
    const pad = Math.max(0, Number(margin) || 0);
    const chosenSeed = Number.isFinite(seed) ? Number(seed) : Math.floor(Math.random() * 2 ** 31);
    const rand = makeSeededRandom(chosenSeed);

    type BBox = { x0: number; y0: number; x1: number; y1: number };
    const bboxes: BBox[] = [];
    const maxTries = Math.max(200, nodes.length * 500);

    const collides = (a: BBox, b: BBox): boolean => {
      return !(a.x1 <= b.x0 || a.x0 >= b.x1 || a.y1 <= b.y0 || a.y0 >= b.y1);
    };

    const originalPos = new Map<string, Point>();
    for (const n of nodes) originalPos.set(n.id, { ...n.pos });

    for (const n of nodes) {
      const { w, h } = safeSizeFromVocab(this.vocab, n.type);
      const s = Number(n.scale ?? 1);
      const bw = Math.max(1, w * s);
      const bh = Math.max(1, h * s);

      const minX = pad + bw / 2;
      const maxX = W - pad - bw / 2;
      const minY = pad + bh / 2;
      const maxY = H - pad - bh / 2;

      let placed = false;
      for (let i = 0; i < maxTries; i++) {
        const x = minX <= maxX ? minX + rand() * (maxX - minX) : W / 2;
        const y = minY <= maxY ? minY + rand() * (maxY - minY) : H / 2;
        const box = { x0: x - bw / 2, y0: y - bh / 2, x1: x + bw / 2, y1: y + bh / 2 };
        if (bboxes.some((b) => collides(box, b))) continue;
        n.pos = { x, y };
        bboxes.push(box);
        placed = true;
        break;
      }

      if (!placed) {
        const old = originalPos.get(n.id) || { x: W / 2, y: H / 2 };
        n.pos = old;
        bboxes.push({ x0: old.x - bw / 2, y0: old.y - bh / 2, x1: old.x + bw / 2, y1: old.y + bh / 2 });
      }
    }

    for (const net of this.scene.nets) {
      net.path = this.computeDefaultNetPath(net);
    }

    this.scene.meta = this.scene.meta || {};
    this.scene.meta.seed = chosenSeed;
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
    const nodes = this.scene.nodes;

    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      const pins = iterPinsFromVocab(this.vocab, n.type);
      if (pins.length === 0) continue;

      const rad = rotToRad(n.rot);
      const s = Number(n.scale ?? 1);
      const { w, h } = safeSizeFromVocab(this.vocab, n.type);
      const bw = w * s;
      const bh = h * s;
      const pinRadius = Math.max(1.6, Math.min(4.5, Math.min(bw, bh) * 0.22));

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

  private nodeBBox(nodeId: string): BBox | null {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return null;
    const { w, h } = safeSizeFromVocab(this.vocab, n.type);
    const s = Number(n.scale ?? 1);
    const bw = w * s;
    const bh = h * s;
    return { x0: n.pos.x - bw / 2, y0: n.pos.y - bh / 2, x1: n.pos.x + bw / 2, y1: n.pos.y + bh / 2 };
  }

  private endpointOutPoint(ep: Endpoint, p: Point, leadLen: number): Point {
    const node = this.scene.nodes.find((x) => x.id === ep.node);
    if (!node) return { ...p };
    const pins = iterPinsFromVocab(this.vocab, node.type);
    const pin = pins.find((pp) => pp.name === ep.pin);

    let vx = p.x - node.pos.x;
    let vy = p.y - node.pos.y;
    if (pin) {
      const rad = rotToRad(node.rot);
      const s = Number(node.scale ?? 1);
      const local = rotateXY(pin.x * s, pin.y * s, rad);
      vx = local.x;
      vy = local.y;
    }
    const norm = Math.hypot(vx, vy);
    if (norm < 1e-3) return { x: p.x + leadLen, y: p.y };
    return { x: p.x + (vx / norm) * leadLen, y: p.y + (vy / norm) * leadLen };
  }

  private segmentIntersectsBBox(a: Point, b: Point, bb: BBox): boolean {
    const eps = 0.001;
    const x0 = bb.x0 - eps;
    const y0 = bb.y0 - eps;
    const x1 = bb.x1 + eps;
    const y1 = bb.y1 + eps;

    if (Math.abs(a.y - b.y) < eps) {
      const y = a.y;
      if (y < y0 || y > y1) return false;
      const sx0 = Math.min(a.x, b.x);
      const sx1 = Math.max(a.x, b.x);
      return !(sx1 < x0 || sx0 > x1);
    }
    if (Math.abs(a.x - b.x) < eps) {
      const x = a.x;
      if (x < x0 || x > x1) return false;
      const sy0 = Math.min(a.y, b.y);
      const sy1 = Math.max(a.y, b.y);
      return !(sy1 < y0 || sy0 > y1);
    }
    return false;
  }

  private pathIntersectsBBoxes(path: Point[], bboxes: BBox[]): boolean {
    for (let i = 0; i < path.length - 1; i++) {
      for (const bb of bboxes) {
        if (this.segmentIntersectsBBox(path[i], path[i + 1], bb)) return true;
      }
    }
    return false;
  }

  private netObstacles(_net: Net): BBox[] {
    const margin = 2;
    return this.scene.nodes
      .map((n) => this.nodeBBox(n.id))
      .map((bb) => (bb ? { x0: bb.x0 - margin, y0: bb.y0 - margin, x1: bb.x1 + margin, y1: bb.y1 + margin } : null))
      .filter((bb): bb is BBox => !!bb);
  }

  private pathForObstacleCheck(path: Point[], net: Net): Point[] {
    if (!Array.isArray(path) || path.length < 2) return path;
    const leadLen = 12;
    const p0 = this.endpointXY(net.from);
    const p1 = this.endpointXY(net.to);
    const p0Out = this.endpointOutPoint(net.from, p0, leadLen);
    const p1Out = this.endpointOutPoint(net.to, p1, leadLen);
    const adjusted = path.map((p) => ({ ...p }));
    adjusted[0] = p0Out;
    adjusted[adjusted.length - 1] = p1Out;
    return adjusted;
  }

  private validateBackendPath(net: Net, path: Point[]): { ok: boolean; hitObstacle: boolean } {
    if (!Array.isArray(path) || path.length < 2) return { ok: false, hitObstacle: false };
    const obstacles = this.netObstacles(net);
    const hitObstacle = this.pathIntersectsBBoxes(this.pathForObstacleCheck(path, net), obstacles);
    return { ok: !hitObstacle, hitObstacle };
  }

  private reroutePolylineAvoidObstacles(path: Point[], net: Net): Point[] {
    const p0 = this.endpointXY(net.from);
    const p1 = this.endpointXY(net.to);
    const leadLen = 12;
    const p0Out = this.endpointOutPoint(net.from, p0, leadLen);
    const p1Out = this.endpointOutPoint(net.to, p1, leadLen);

    const astar = this.findOrthogonalGridRoute(net, p0, p0Out, p1, p1Out);
    if (astar) {
      (net as any).route_status = "ok";
      (net as any).route_message = undefined;
      return astar;
    }

    const obstacles = this.netObstacles(net);

    const tryPaths: Point[][] = [path];
    if (path.length >= 5) {
      const p0 = path[0];
      const p0Out = path[1];
      const p1In = path[path.length - 2];
      const p1 = path[path.length - 1];
      tryPaths.push([{ ...p0 }, { ...p0Out }, { x: p0Out.x, y: p1In.y }, { ...p1In }, { ...p1 }]);

      const axisOffset = 22;
      const midX = (p0Out.x + p1In.x) / 2;
      const midY = (p0Out.y + p1In.y) / 2;
      tryPaths.push([{ ...p0 }, { ...p0Out }, { x: midX + axisOffset, y: p0Out.y }, { x: midX + axisOffset, y: p1In.y }, { ...p1In }, { ...p1 }]);
      tryPaths.push([{ ...p0 }, { ...p0Out }, { x: p0Out.x, y: midY - axisOffset }, { x: p1In.x, y: midY - axisOffset }, { ...p1In }, { ...p1 }]);
    }

    for (const candidate of tryPaths) {
      if (!this.pathIntersectsBBoxes(this.pathForObstacleCheck(candidate, net), obstacles)) {
        (net as any).route_status = "ok";
        (net as any).route_message = undefined;
        return candidate;
      }
    }
    (net as any).route_status = "failed";
    (net as any).route_message = "避障失败";
    return path;
  }

  private computeDefaultNetPath(net: Net): Point[] {
    const hash = this.routingHash(net);
    const cached = this.pathCache.get(net.id);
    if (cached && cached.hash === hash) {
      (net as any).route_status = cached.failed ? "failed" : "ok";
      (net as any).route_message = cached.failed ? "避障失败" : undefined;
      return cached.path.map((p) => ({ ...p }));
    }

    const p0 = this.endpointXY(net.from);
    const p1 = this.endpointXY(net.to);
    const leadLen = 12;
    const p0Out = this.endpointOutPoint(net.from, p0, leadLen);
    const p1Out = this.endpointOutPoint(net.to, p1, leadLen);

    const hv = [{ ...p0 }, { ...p0Out }, { x: p1Out.x, y: p0Out.y }, { ...p1Out }, { ...p1 }];
    const vh = [{ ...p0 }, { ...p0Out }, { x: p0Out.x, y: p1Out.y }, { ...p1Out }, { ...p1 }];

    const obstacles = this.netObstacles(net);

    const hvPenalty = this.pathIntersectsBBoxes(this.pathForObstacleCheck(hv, net), obstacles) ? 1 : 0;
    const vhPenalty = this.pathIntersectsBBoxes(this.pathForObstacleCheck(vh, net), obstacles) ? 1 : 0;
    const base = hvPenalty <= vhPenalty ? hv : vh;
    const routed = this.reroutePolylineAvoidObstacles(base, net);
    const failed = String((net as any).route_status ?? "") === "failed";
    this.pathCache.set(net.id, { hash, path: routed.map((p) => ({ ...p })), failed });
    return routed;
  }

  private routingHash(net: Net): string {
    const nodeSig = this.scene.nodes
      .map((n) => `${n.id}:${n.pos.x.toFixed(1)},${n.pos.y.toFixed(1)},${Number(n.rot ?? 0).toFixed(3)},${Number(n.scale ?? 1).toFixed(3)}`)
      .sort()
      .join("|");
    return `${net.from.node}.${net.from.pin}->${net.to.node}.${net.to.pin}#${nodeSig}`;
  }

  private findOrthogonalGridRoute(net: Net, p0: Point, p0Out: Point, p1: Point, p1Out: Point): Point[] | null {
    const grid = Math.max(8, Math.min(16, 12));
    const inflate = grid;
    const cols = Math.ceil(this.resolution.w / grid);
    const rows = Math.ceil(this.resolution.h / grid);
    const blocked = new Uint8Array(cols * rows);
    const idx = (x: number, y: number) => y * cols + x;
    const clampCell = (v: number, max: number) => Math.max(0, Math.min(max - 1, v));
    const toCell = (p: Point) => ({ x: clampCell(Math.round(p.x / grid), cols), y: clampCell(Math.round(p.y / grid), rows) });
    const cellCenter = (x: number, y: number): Point => ({ x: x * grid, y: y * grid });

    const markBox = (bb: BBox) => {
      const x0 = clampCell(Math.floor(bb.x0 / grid), cols);
      const y0 = clampCell(Math.floor(bb.y0 / grid), rows);
      const x1 = clampCell(Math.ceil(bb.x1 / grid), cols);
      const y1 = clampCell(Math.ceil(bb.y1 / grid), rows);
      for (let y = y0; y <= y1; y++) {
        for (let x = x0; x <= x1; x++) blocked[idx(x, y)] = 1;
      }
    };

    for (const n of this.scene.nodes) {
      const bb = this.nodeBBox(n.id);
      if (!bb) continue;
      markBox({ x0: bb.x0 - inflate, y0: bb.y0 - inflate, x1: bb.x1 + inflate, y1: bb.y1 + inflate });
    }

    const clearLead = (a: Point, b: Point): void => {
      const n = Math.max(2, Math.ceil(Math.hypot(a.x - b.x, a.y - b.y) / (grid / 2)));
      for (let i = 0; i <= n; i++) {
        const t = i / n;
        const cx = a.x + (b.x - a.x) * t;
        const cy = a.y + (b.y - a.y) * t;
        const c = toCell({ x: cx, y: cy });
        blocked[idx(c.x, c.y)] = 0;
      }
    };
    clearLead(p0, p0Out);
    clearLead(p1, p1Out);

    const start = toCell(p0Out);
    const goal = toCell(p1Out);
    blocked[idx(start.x, start.y)] = 0;
    blocked[idx(goal.x, goal.y)] = 0;

    const open: Array<{ x: number; y: number; f: number; g: number }> = [{ x: start.x, y: start.y, g: 0, f: Math.abs(goal.x - start.x) + Math.abs(goal.y - start.y) }];
    const gScore = new Float64Array(cols * rows);
    gScore.fill(Number.POSITIVE_INFINITY);
    gScore[idx(start.x, start.y)] = 0;
    const parent = new Int32Array(cols * rows);
    parent.fill(-1);
    const closed = new Uint8Array(cols * rows);

    while (open.length) {
      open.sort((a, b) => a.f - b.f);
      const cur = open.shift()!;
      const cid = idx(cur.x, cur.y);
      if (closed[cid]) continue;
      closed[cid] = 1;
      if (cur.x === goal.x && cur.y === goal.y) break;

      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]] as const) {
        const nx = cur.x + dx;
        const ny = cur.y + dy;
        if (nx < 0 || nx >= cols || ny < 0 || ny >= rows) continue;
        const nid = idx(nx, ny);
        if (blocked[nid] || closed[nid]) continue;
        const ng = cur.g + 1;
        if (ng >= gScore[nid]) continue;
        gScore[nid] = ng;
        parent[nid] = cid;
        const h = Math.abs(goal.x - nx) + Math.abs(goal.y - ny);
        open.push({ x: nx, y: ny, g: ng, f: ng + h });
      }
    }

    if (parent[idx(goal.x, goal.y)] < 0 && (start.x !== goal.x || start.y !== goal.y)) return null;

    const cells: Array<{ x: number; y: number }> = [];
    let curId = idx(goal.x, goal.y);
    cells.push({ x: goal.x, y: goal.y });
    while (curId !== idx(start.x, start.y)) {
      curId = parent[curId];
      if (curId < 0) return null;
      cells.push({ x: curId % cols, y: Math.floor(curId / cols) });
    }
    cells.reverse();

    const mids: Point[] = [];
    for (let i = 0; i < cells.length; i++) {
      if (i === 0 || i === cells.length - 1) {
        mids.push(cellCenter(cells[i].x, cells[i].y));
        continue;
      }
      const a = cells[i - 1];
      const b = cells[i];
      const c = cells[i + 1];
      const dir1x = b.x - a.x;
      const dir1y = b.y - a.y;
      const dir2x = c.x - b.x;
      const dir2y = c.y - b.y;
      if (dir1x !== dir2x || dir1y !== dir2y) mids.push(cellCenter(b.x, b.y));
    }

    const merged = this.simplifyOrthogonalPath([p0, p0Out, ...mids, p1Out, p1]);
    const obstacles = this.netObstacles(net);
    if (this.pathIntersectsBBoxes(this.pathForObstacleCheck(merged, net), obstacles)) return null;
    return merged;
  }

  private simplifyOrthogonalPath(path: Point[]): Point[] {
    const out: Point[] = [];
    for (const p of path) {
      const last = out[out.length - 1];
      if (last && Math.abs(last.x - p.x) < 0.01 && Math.abs(last.y - p.y) < 0.01) continue;
      out.push({ ...p });
      while (out.length >= 3) {
        const a = out[out.length - 3];
        const b = out[out.length - 2];
        const c = out[out.length - 1];
        const sameX = Math.abs(a.x - b.x) < 0.01 && Math.abs(b.x - c.x) < 0.01;
        const sameY = Math.abs(a.y - b.y) < 0.01 && Math.abs(b.y - c.y) < 0.01;
        if (!sameX && !sameY) break;
        out.splice(out.length - 2, 1);
      }
    }
    return out;
  }

  draw(ctx: CanvasRenderingContext2D): void {
    const W = this.resolution.w;
    const H = this.resolution.h;

    // clear
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, W, H);
    ctx.restore();

    // nets
    for (const e of this.scene.nets) {
      const isSel = this.sel?.netId === e.id;
      let path = (e.path && e.path.length >= 2) ? e.path : this.computeDefaultNetPath(e);
      const backendCheck = this.validateBackendPath(e, path);
      if (!backendCheck.ok) {
        const fallback = this.reroutePolylineAvoidObstacles(this.computeDefaultNetPath(e), e);
        const repaired = this.validateBackendPath(e, fallback);
        if (repaired.ok) {
          path = fallback;
          e.path = fallback;
          (e as any).route_status = "degraded";
          (e as any).route_constraint_satisfied = false;
        } else {
          // 明显告警：高亮问题线网而不是静默绘制
          (e as any).route_status = "failed";
        }
      }

      ctx.save();
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.lineWidth = (isSel ? 6 : 5) * this.netStrokeScale;
      ctx.strokeStyle = "#ff0000";
      ctx.beginPath();
      ctx.moveTo(path[0].x, path[0].y);
      for (let i = 1; i < path.length; i++) ctx.lineTo(path[i].x, path[i].y);
      ctx.stroke();

      ctx.lineWidth = (isSel ? 3 : 2) * this.netStrokeScale;
      const routeFailed = String((e as any).route_status ?? "") === "failed";
      ctx.strokeStyle = routeFailed ? "#00ff00" : "#ff0000";
      ctx.stroke();

      if (routeFailed) {
        const mid = path[Math.floor(path.length / 2)] ?? path[0];
        ctx.fillStyle = "#ffffff";
        ctx.font = "12px sans-serif";
        ctx.fillText("避障失败", mid.x + 8, mid.y - 8);
      }

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
      ctx.strokeStyle = "#ff0000";
      ctx.lineWidth = (isSel ? 3 : 2) * this.nodeStrokeScale;

      let rendered = false;
      const custom = this.customSymbols.get(String(n.type));
      const hasCustom = Boolean(custom?.img?.complete);
      if (this.nodeRenderMode === "symbol") {
        if (hasCustom && custom) {
          const iw = custom.img.naturalWidth || bw;
          const ih = custom.img.naturalHeight || bh;
          const fit = Math.min(bw / iw, bh / ih);
          const dw = iw * fit;
          const dh = ih * fit;
          ctx.drawImage(custom.img, -dw / 2, -dh / 2, dw, dh);
          rendered = true;
        } else {
          rendered = drawComponentSymbol(ctx, n.type, bw, bh);
        }
      }
      if (!hasCustom && (!rendered || this.nodeRenderMode === "box")) {
        ctx.fillStyle = "#000000";
        ctx.beginPath();
        ctx.rect(-bw / 2, -bh / 2, bw, bh);
        ctx.fill();
        ctx.stroke();

        // fallback text
        ctx.fillStyle = "#ff0000";
        ctx.font = "14px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(n.type, 0, 0);
      } else if (this.showTypeLabelOnSymbol) {
        ctx.fillStyle = "#ff0000";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(n.type, 0, bh / 2 + 6);
      }

      // pins
      const pins = iterPinsFromVocab(this.vocab, n.type);
      if (pins.length) {
        const pinOuter = Math.max(0.8, Math.min(3.2, Math.min(bw, bh) * 0.14));
        const pinInner = Math.max(0.5, pinOuter * 0.68);
        ctx.fillStyle = "#ff0000";
        for (const p of pins) {
          ctx.strokeStyle = "#ff0000";
          ctx.lineWidth = Math.max(0.5, pinOuter * 0.4);
          ctx.beginPath();
          ctx.arc(p.x * s, p.y * s, pinOuter, 0, Math.PI * 2);
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(p.x * s, p.y * s, pinInner, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      ctx.restore();
    }
  }
}
