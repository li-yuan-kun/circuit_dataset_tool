export type Point = { x: number; y: number };
export type Resolution = { w: number; h: number };

export type Node = {
  id: string;
  type: string;
  pos: Point;
  rot?: number; // rad or deg (best-effort), keep consistent with backend
  scale?: number;
};

export type Endpoint = { node: string; pin: string };

export type Net = {
  id: string;
  from: Endpoint;
  to: Endpoint;
  path?: Point[];
};

export type SceneMeta = {
  // 后端 schema 允许缺省（会填默认），TS 侧也放宽一些
  scene_version?: "0.3" | string;
  tool_version?: "0.3" | string;
  vocab_version?: string | null;
  seed?: number | null;
  resolution?: Resolution | null;
  params?: Record<string, any>;
  timestamp?: string | null;
};

export type MaskRef = {
  mode: "external" | "generated";
  path?: string;
  hash?: string;
  strategy?: string;
  params?: Record<string, any>;
};

export type Scene = {
  meta: SceneMeta;
  nodes: Node[];
  nets: Net[];
  mask?: MaskRef;
};

export type OcclusionItem = { node_id: string; type: string; occ_ratio: number };

export type Label = {
  label_version?: "0.3" | string;
  counts_all: Record<string, number>;
  counts_visible: Record<string, number>;
  occlusion: OcclusionItem[];
  occ_threshold: number;
  function: string;
  meta?: Record<string, any>;
};

export type ApiErrorPayload = {
  error: {
    code: string;
    message: string;
    details?: Record<string, any>;
  };
};