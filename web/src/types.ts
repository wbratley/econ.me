export interface Point {
  x: number;
  y: number;
}

export interface WorldBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Geometry {
  id: "tile2d.v1";
  kind: "tile2d";
  version: 1;
  generator_version: string;
  cache_key: string;
  seed: string;
  tile_size: number;
  chunk_size: number;
  world: WorldBounds;
  coordinates: string;
}

export interface TileDefinition {
  id: number;
  key: string;
  color: string;
  role: "decor";
}

export interface Assets {
  tiles: TileDefinition[];
  actor: {
    frame_width: number;
    frame_height: number;
    animations: Record<string, { frames: number[]; frame_ms: number }>;
  };
  note: string;
}

export interface Place {
  key: string;
  name: string;
  kind: string;
  region_id: string;
  description: string;
  position: Point;
}

export interface Road {
  id: string;
  from: string;
  to: string;
  mode: string;
  cost_ticks: number;
  bidirectional: boolean;
  path: [Point, Point];
}

export type ActorAction = "idle" | "walk" | "work" | "travel" | "incapacitated";

export interface Actor {
  id: string;
  name: string;
  entity_type: string;
  status: string;
  place: string;
  appearance: string;
  action: ActorAction;
  action_progress: number | null;
  position: Point;
  velocity?: Point;
  bounds?: WorldBounds;
  control?: "player";
}

export interface SceneObject {
  id: string;
  kind: "parcel" | "facility" | "deposit";
  appearance: string;
  name: string;
  place: string;
  position: Point;
  parcel_id: string;
  lit?: boolean;
  symbol?: string;
  depleted?: boolean;
}

export interface Speech {
  tick: number;
  entity_id: string;
  name: string;
  text: string;
}

export interface Clock {
  tick: number;
  day: number;
  hour: number;
  is_day: boolean;
  is_night: boolean;
  daylight_hours: string;
  season: string;
  season_day: number;
  days_per_season: number;
}

export interface SceneSnapshot {
  changed: true;
  revision: string;
  geometry: "tile2d.v1";
  geometry_revision: string;
  tick: number;
  clock: Clock | null;
  world: WorldBounds;
  places: Place[];
  roads: Road[];
  actors: Actor[];
  objects: SceneObject[];
  unplaced_actor_count: number;
  speech: Speech[];
}

export interface SceneUnchanged {
  changed: false;
  revision: string;
  tick: number;
}

export type SceneResponse = SceneSnapshot | SceneUnchanged;

export interface PlayerEntity {
  id: string;
  name: string;
  place: string;
}

export interface JoinResult {
  entity: {
    id: string;
    name: string;
  };
}

export interface Bootstrap {
  protocol: "econ-world.v1";
  session: {
    role: "spectator";
    user_id: string;
    entity_id: null;
    capabilities: [];
  };
  geometry: Geometry;
  assets: Assets;
  player_entities: PlayerEntity[];
  endpoints: {
    scene: string;
    chunks: string;
    events: string;
    stream: string;
  };
  scene: SceneSnapshot;
}

export interface SceneDelta {
  type: "scene_delta";
  base_revision: string;
  revision: string;
  tick: number;
  clock: Clock | null;
  unplaced_actor_count: number;
  places?: Place[];
  roads?: Road[];
  actors?: Actor[];
  objects?: SceneObject[];
  speech?: Speech[];
}

export interface SceneSnapshotMessage {
  type: "scene_snapshot";
  scene: SceneSnapshot;
}

export interface PresenceActor {
  id: string;
  place: string;
  position: Point;
  velocity: Point;
  bounds: WorldBounds;
  action: "walk";
  control: "player";
}

export interface PresenceDelta {
  type: "presence_delta";
  revision: number;
  actors: PresenceActor[];
  released: string[];
  reason?: string;
}

export interface CommandResult {
  type: "command_result";
  command: string;
  status: "accepted" | "applied" | "queued" | "rejected";
  entity_id?: string;
  seq?: number;
  reason?: string;
  actor?: PresenceActor;
}

export type WorldCommand =
  | { type: "possess"; entity_id: string; seq: number }
  | { type: "release"; entity_id: string; seq: number }
  | { type: "move"; entity_id: string; seq: number; dx: number; dy: number }
  | { type: "say"; entity_id: string; seq: number; text: string };

export type WorldStreamMessage =
  | { type: "hello"; bootstrap: Bootstrap }
  | SceneDelta
  | SceneSnapshotMessage
  | PresenceDelta
  | CommandResult
  | { type: "ping"; tick: number; revision: string }
  | { type: "error"; reason: string; detail?: string };

export interface Chunk {
  geometry: "tile2d.v1";
  generator_version: string;
  cache_key: string;
  revision: string;
  cx: number;
  cy: number;
  width: number;
  height: number;
  tiles: number[];
}
