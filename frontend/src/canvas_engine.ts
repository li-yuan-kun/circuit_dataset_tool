import type { Scene, Endpoint, Point, Resolution, Node, Net } from "./modules/types";

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
    this.refreshNetPathsForNode(nodeId);
  }

  rotateNode(nodeId: string, rot: number): void {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n.rot = rot;
    this.refreshNetPathsForNode(nodeId);
  }

  scaleNode(nodeId: string, scale: number): void {
    const n = this.scene.nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n.scale = Math.max(0.05, Number(scale));
    this.refreshNetPathsForNode(nodeId);
  }

  private refreshNetPathsForNode(nodeId: string): void {
    for (const net of this.scene.nets) {
      if (net.from.node !== nodeId && net.to.node !== nodeId) continue;
      net.path = this.computeDefaultNetPath(net);
    }
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

  private netObstacles(net: Net): BBox[] {
    return this.scene.nodes
      .filter((n) => n.id !== net.from.node && n.id !== net.to.node)
      .map((n) => this.nodeBBox(n.id))
      .filter((bb): bb is BBox => !!bb);
  }

  private validateBackendPath(net: Net, path: Point[]): { ok: boolean; hitObstacle: boolean } {
    if (!Array.isArray(path) || path.length < 2) return { ok: false, hitObstacle: false };
    const obstacles = this.netObstacles(net);
    const hitObstacle = this.pathIntersectsBBoxes(path, obstacles);
    return { ok: !hitObstacle, hitObstacle };
  }

  private reroutePolylineAvoidObstacles(path: Point[], net: Net): Point[] {
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
      if (!this.pathIntersectsBBoxes(candidate, obstacles)) return candidate;
    }
    return path;
  }

  private computeDefaultNetPath(net: Net): Point[] {
    const p0 = this.endpointXY(net.from);
    const p1 = this.endpointXY(net.to);
    const leadLen = 12;
    const p0Out = this.endpointOutPoint(net.from, p0, leadLen);
    const p1Out = this.endpointOutPoint(net.to, p1, leadLen);

    const hv = [{ ...p0 }, { ...p0Out }, { x: p1Out.x, y: p0Out.y }, { ...p1Out }, { ...p1 }];
    const vh = [{ ...p0 }, { ...p0Out }, { x: p0Out.x, y: p1Out.y }, { ...p1Out }, { ...p1 }];

    const fromBox = this.nodeBBox(net.from.node);
    const toBox = this.nodeBBox(net.to.node);
    const endpointBoxes = [fromBox, toBox].filter((bb): bb is BBox => !!bb);

    const hvPenalty = this.pathIntersectsBBoxes(hv.slice(1, hv.length - 1), endpointBoxes) ? 1 : 0;
    const vhPenalty = this.pathIntersectsBBoxes(vh.slice(1, vh.length - 1), endpointBoxes) ? 1 : 0;
    const base = hvPenalty <= vhPenalty ? hv : vh;
    return this.reroutePolylineAvoidObstacles(base, net);
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
      ctx.lineWidth = isSel ? 6 : 5;
      ctx.strokeStyle = "#ffffff";
      ctx.beginPath();
      ctx.moveTo(path[0].x, path[0].y);
      for (let i = 1; i < path.length; i++) ctx.lineTo(path[i].x, path[i].y);
      ctx.stroke();

      ctx.lineWidth = isSel ? 3 : 2;
      const routeFailed = String((e as any).route_status ?? "") === "failed";
      ctx.strokeStyle = routeFailed ? "#e53935" : (isSel ? "#1e88e5" : "#444");
      ctx.stroke();

      const pStart = path[0];
      const pEnd = path[path.length - 1];
      for (const p of [pStart, pEnd]) {
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(p.x, p.y, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = isSel ? "#1e88e5" : "#222";
        ctx.lineWidth = 2;
        ctx.stroke();
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
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(p.x * s, p.y * s, 5, 0, Math.PI * 2);
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(p.x * s, p.y * s, 3.5, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      ctx.restore();
    }
  }
}
