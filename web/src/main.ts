import "./style.css";
import type {
  Actor, Bootstrap, Chunk, JoinResult, PlayerEntity, Point, PresenceActor,
  SceneObject, SceneResponse, SceneSnapshot, TileDefinition, WorldCommand,
  WorldStreamMessage,
} from "./types";

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing #${id}`);
  return found as T;
}

const canvas = element<HTMLCanvasElement>("world");
const maybeContext = canvas.getContext("2d");
if (!maybeContext) throw new Error("Canvas 2D is not available");
const context = maybeContext;
const connectForm = element<HTMLFormElement>("connect-form");
const connectButton = element<HTMLButtonElement>("connect");
const tokenInput = element<HTMLInputElement>("token");
const connectionStatus = element<HTMLParagraphElement>("connection-status");
const mapStatus = element<HTMLDivElement>("map-status");
const clockLabel = element<HTMLParagraphElement>("clock");
const sceneStats = element<HTMLParagraphElement>("scene-stats");
const actorsPanel = element<HTMLDivElement>("actors");
const speechPanel = element<HTMLDivElement>("speech");
const stopFollowing = element<HTMLButtonElement>("stop-following");
const sessionBadge = element<HTMLSpanElement>("session-badge");
const playPanel = element<HTMLElement>("play-panel");
const playerEntitySelect = element<HTMLSelectElement>("player-entity");
const joinWorldButton = element<HTMLButtonElement>("join-world");
const playerEntityLabel = element<HTMLLabelElement>("player-entity-label");
const possessRow = element<HTMLDivElement>("possess-row");
const possessButton = element<HTMLButtonElement>("possess-entity");
const releaseButton = element<HTMLButtonElement>("release-entity");
const dpad = element<HTMLDivElement>("dpad");
const sayForm = element<HTMLFormElement>("say-form");
const sayText = element<HTMLInputElement>("say-text");
const playStatus = element<HTMLParagraphElement>("play-status");

let bootstrap: Bootstrap | null = null;
let scene: SceneSnapshot | null = null;
let token = "";
let followId: string | null = null;
let refreshing = false;
let eventStream: EventSource | null = null;
let worldSocket: WebSocket | null = null;
let streamPermanentlyClosed = false;
let pollTimer: number | null = null;
let playerEntities: PlayerEntity[] = [];
let possessedId: string | null = null;
let commandSeq = 0;
let lastMoveVector: Point = { x: 0, y: 0 };
const heldDirections = new Set<string>();
const presenceOverlays = new Map<string, { actor: PresenceActor; receivedAt: number }>();
let pixelRatio = 1;
let viewWidth = 1;
let viewHeight = 1;
const chunks = new Map<string, Chunk>();
const pendingChunks = new Set<string>();
const failedChunks = new Set<string>();
const tileDefinitions = new Map<number, TileDefinition>();
const camera = { x: 64, y: 48, zoom: 1.25 };
const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

function setStatus(text: string): void {
  connectionStatus.textContent = text;
}

function setPlayStatus(text: string): void {
  playStatus.textContent = text;
}

function nextSeq(): number {
  commandSeq += 1;
  return commandSeq;
}

function sendWorldCommand(command: WorldCommand): boolean {
  if (!worldSocket || worldSocket.readyState !== WebSocket.OPEN) {
    setPlayStatus("World stream is not connected.");
    return false;
  }
  worldSocket.send(JSON.stringify(command));
  return true;
}

function updatePlayPanel(): void {
  playPanel.hidden = bootstrap === null;
  playPanel.classList.toggle("possessed", possessedId !== null);
  playerEntitySelect.replaceChildren();
  for (const entity of playerEntities) {
    const option = document.createElement("option");
    option.value = entity.id;
    option.textContent = `${entity.name} · ${entity.place}`;
    playerEntitySelect.append(option);
  }
  if (possessedId) playerEntitySelect.value = possessedId;
  const hasEntities = playerEntities.length > 0;
  joinWorldButton.hidden = hasEntities || possessedId !== null;
  playerEntityLabel.hidden = !hasEntities;
  possessRow.hidden = !hasEntities;
  playerEntitySelect.disabled = possessedId !== null;
  possessButton.disabled = possessedId !== null || !hasEntities;
  possessButton.hidden = possessedId !== null;
  releaseButton.hidden = possessedId === null;
  dpad.hidden = possessedId === null;
  sayForm.hidden = possessedId === null;
  sessionBadge.textContent = possessedId ? "Playing · engine backed" : "Spectator · read only";
}

function clearPossession(reason: string): void {
  if (possessedId) presenceOverlays.delete(possessedId);
  possessedId = null;
  heldDirections.clear();
  lastMoveVector = { x: 0, y: 0 };
  sayText.value = "";
  setPlayStatus(reason);
  updatePlayPanel();
}

function applyPresenceActor(actor: PresenceActor): void {
  presenceOverlays.set(actor.id, { actor, receivedAt: performance.now() });
  const row = scene?.actors.find((candidate) => candidate.id === actor.id);
  if (!row) return;
  row.place = actor.place;
  row.position = actor.position;
  row.velocity = actor.velocity;
  row.bounds = actor.bounds;
  row.action = row.action === "idle" ? "walk" : row.action;
  row.control = "player";
}

function removePresenceActor(id: string): void {
  presenceOverlays.delete(id);
  const row = scene?.actors.find((candidate) => candidate.id === id);
  if (!row) return;
  delete row.velocity;
  delete row.bounds;
  delete row.control;
  if (row.action === "walk") row.action = "idle";
}

function syncPresenceFromScene(): void {
  if (!scene) return;
  const live = new Set<string>();
  for (const actor of scene.actors) {
    if (actor.control === "player" && actor.velocity && actor.bounds) {
      live.add(actor.id);
      applyPresenceActor({
        id: actor.id, place: actor.place, position: actor.position,
        velocity: actor.velocity, bounds: actor.bounds,
        action: "walk", control: "player",
      });
    }
  }
  for (const id of [...presenceOverlays.keys()]) {
    if (!live.has(id)) presenceOverlays.delete(id);
  }
}

function renderedPosition(actor: Actor, now = performance.now()): Point {
  const overlay = presenceOverlays.get(actor.id);
  if (!overlay) return actor.position;
  const elapsed = Math.max(0, (now - overlay.receivedAt) / 1000);
  const speed = 2.2;
  const x = overlay.actor.position.x + overlay.actor.velocity.x * speed * elapsed;
  const y = overlay.actor.position.y + overlay.actor.velocity.y * speed * elapsed;
  return {
    x: Math.max(overlay.actor.bounds.x, Math.min(overlay.actor.bounds.x + overlay.actor.bounds.width, x)),
    y: Math.max(overlay.actor.bounds.y, Math.min(overlay.actor.bounds.y + overlay.actor.bounds.height, y)),
  };
}

async function api<T>(path: string, cache: RequestCache = "no-store"): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(body?.detail ?? `${response.status} ${response.statusText}`);
  }
  return await response.json() as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const responseBody = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(responseBody?.detail ?? `${response.status} ${response.statusText}`);
  }
  return await response.json() as T;
}

function closeLiveChannels(): void {
  eventStream?.close();
  eventStream = null;
  const socket = worldSocket;
  worldSocket = null;
  socket?.close();
  if (pollTimer !== null) window.clearInterval(pollTimer);
  pollTimer = null;
  heldDirections.clear();
  if (possessedId) removePresenceActor(possessedId);
  possessedId = null;
  updatePlayPanel();
}

async function connect(): Promise<void> {
  token = tokenInput.value.trim();
  if (!token) return;
  connectButton.disabled = true;
  setStatus("Opening the world…");
  closeLiveChannels();
  try {
    streamPermanentlyClosed = false;
    const next = await api<Bootstrap>("/world/bootstrap");
    bootstrap = next;
    scene = next.scene;
    sessionStorage.setItem("econ.world.token", token);
    chunks.clear();
    pendingChunks.clear();
    failedChunks.clear();
    tileDefinitions.clear();
    presenceOverlays.clear();
    for (const tile of next.assets.tiles) tileDefinitions.set(tile.id, tile);
    followId = null;
    possessedId = null;
    commandSeq = 0;
    heldDirections.clear();
    playerEntities = next.player_entities;
    updatePlayPanel();
    setPlayStatus(next.player_entities.length
      ? "Choose an entity to possess."
      : "Spectating. Join the world to create a founder.");
    document.body.classList.add("connected");
    resizeCanvas();
    fitWorld();
    renderPanels();
    subscribeToClock();
    setStatus(
      next.player_entities.length
        ? `Watching as ${next.session.user_id} · ${next.player_entities.length} entity available`
        : `Watching as ${next.session.user_id} · public scene only`,
    );
  } catch (error) {
    bootstrap = null;
    scene = null;
    playerEntities = [];
    presenceOverlays.clear();
    possessedId = null;
    updatePlayPanel();
    document.body.classList.remove("connected");
    setStatus(error instanceof Error ? error.message : "Could not connect");
  } finally {
    connectButton.disabled = false;
  }
}

function subscribeToClock(): void {
  if (!bootstrap) return;
  eventStream = new EventSource(`${API_BASE}${bootstrap.endpoints.events}`);
  eventStream.addEventListener("tick", () => {
    if (worldSocket?.readyState !== WebSocket.OPEN) void refreshScene();
  });
  eventStream.addEventListener("round_closed", () => {
    if (worldSocket?.readyState !== WebSocket.OPEN) void refreshScene();
  });
  eventStream.onerror = () => {
    if (bootstrap && worldSocket?.readyState !== WebSocket.OPEN) {
      setStatus("Live clock reconnecting · polling still active");
    }
  };
  connectWorldStream();
  pollTimer = window.setInterval(() => void refreshScene(), 10000);
}

function streamUrl(path: string): string {
  const base = API_BASE ? new URL(API_BASE, window.location.href) : new URL(window.location.href);
  const url = new URL(path, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function connectWorldStream(): void {
  if (!bootstrap || streamPermanentlyClosed) return;
  const socket = new WebSocket(streamUrl(bootstrap.endpoints.stream));
  worldSocket = socket;
  socket.onopen = () => {
    socket.send(JSON.stringify({ type: "auth", token }));
  };
  socket.onmessage = (event) => {
    try {
      handleWorldMessage(JSON.parse(String(event.data)) as WorldStreamMessage);
    } catch (error) {
      console.warn("Bad world-stream message", error);
    }
  };
  socket.onerror = () => {
    setStatus("World stream interrupted · clock/polling fallback active");
  };
  socket.onclose = () => {
    if (worldSocket !== socket || !bootstrap || streamPermanentlyClosed) return;
    worldSocket = null;
    if (possessedId) clearPossession("World stream disconnected; the server released local control.");
    window.setTimeout(connectWorldStream, 3000);
  };
}

function handleWorldMessage(message: WorldStreamMessage): void {
  if (message.type === "hello") {
    if (!bootstrap || message.bootstrap.geometry.cache_key !== bootstrap.geometry.cache_key) {
      void connect();
      return;
    }
    playerEntities = message.bootstrap.player_entities;
    updatePlayPanel();
    if (scene && message.bootstrap.scene.revision !== scene.revision) {
      scene = message.bootstrap.scene;
      syncPresenceFromScene();
      renderPanels();
    }
    setStatus(possessedId ? "Playing live" : "Watching live · public scene only");
    return;
  }
  if (message.type === "scene_snapshot") {
    if (!bootstrap || message.scene.geometry_revision !== bootstrap.geometry.cache_key) {
      void connect();
      return;
    }
    scene = message.scene;
    syncPresenceFromScene();
    if (followId && !scene.actors.some((actor) => actor.id === followId)) followId = null;
    renderPanels();
    return;
  }
  if (message.type === "scene_delta") {
    if (!scene || message.base_revision !== scene.revision) {
      void refreshScene();
      return;
    }
    const next: SceneSnapshot = {
      ...scene,
      revision: message.revision,
      tick: message.tick,
      clock: message.clock,
      unplaced_actor_count: message.unplaced_actor_count,
    };
    if (message.places) next.places = message.places;
    if (message.roads) next.roads = message.roads;
    if (message.actors) next.actors = message.actors;
    if (message.objects) next.objects = message.objects;
    if (message.speech) next.speech = message.speech;
    scene = next;
    syncPresenceFromScene();
    if (followId && !scene.actors.some((actor) => actor.id === followId)) followId = null;
    renderPanels();
    return;
  }
  if (message.type === "presence_delta") {
    for (const actor of message.actors) applyPresenceActor(actor);
    for (const id of message.released) {
      removePresenceActor(id);
      if (id === possessedId) {
        clearPossession(message.reason === "engine_process"
          ? "An engine process took control back."
          : "Local control released.");
      }
    }
    renderPanels();
    return;
  }
  if (message.type === "command_result") {
    if (message.status === "rejected") {
      setPlayStatus(message.reason ?? "Command rejected.");
      return;
    }
    if (message.command === "possess" && message.entity_id && message.actor) {
      possessedId = message.entity_id;
      followId = message.entity_id;
      applyPresenceActor(message.actor);
      setPlayStatus("You are walking as this entity.");
      updatePlayPanel();
    } else if (message.command === "release") {
      clearPossession("Local control released.");
    } else if (message.command === "say") {
      setPlayStatus(message.status === "queued"
        ? "Speech queued for the next tick."
        : "Speech resolved by the engine.");
    } else if (message.command === "move") {
      setPlayStatus("Walking.");
    }
    renderPanels();
    return;
  }
  if (message.type === "error") {
    if (message.reason === "unauthorized" || message.reason === "invalid_world_config") {
      streamPermanentlyClosed = true;
    }
    setStatus(message.detail ?? message.reason);
  }
}

async function refreshScene(): Promise<void> {
  if (!bootstrap || !scene || refreshing) return;
  refreshing = true;
  try {
    const separator = bootstrap.endpoints.scene.includes("?") ? "&" : "?";
    const next = await api<SceneResponse>(
      `${bootstrap.endpoints.scene}${separator}since=${encodeURIComponent(scene.revision)}`,
    );
    if (next.changed) {
      scene = next;
      syncPresenceFromScene();
      if (followId && !scene.actors.some((actor) => actor.id === followId)) followId = null;
      renderPanels();
    }
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "Scene refresh failed");
  } finally {
    refreshing = false;
  }
}

function renderPanels(): void {
  if (!scene) return;
  const clock = scene.clock;
  clockLabel.textContent = clock
    ? `Tick ${scene.tick} · Day ${clock.day} · ${String(clock.hour).padStart(2, "0")}:00 · ${clock.season}`
    : `Tick ${scene.tick} · before the first hour`;
  sceneStats.textContent = `${scene.places.length} places · ${scene.actors.length} placed actors · ${scene.objects.length} objects`
    + (scene.unplaced_actor_count ? ` · ${scene.unplaced_actor_count} off-map` : "");
  mapStatus.textContent = `${scene.places.length} places · ${scene.actors.length} actors`
    + (possessedId ? " · walking" : " · read only");
  stopFollowing.hidden = followId === null;

  actorsPanel.replaceChildren();
  if (!scene.actors.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No placed actors in this world.";
    actorsPanel.append(empty);
  }
  for (const actor of scene.actors) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `actor-row${actor.id === followId ? " following" : ""}`;
    const dot = document.createElement("span");
    dot.className = "actor-dot";
    dot.style.background = actorColor(actor);
    const name = document.createElement("span");
    name.className = "actor-name";
    name.textContent = actor.name;
    const action = document.createElement("span");
    action.className = "actor-state";
    action.textContent = actor.action;
    button.append(dot, name, action);
    button.title = `${actor.name} · ${actor.place}`;
    button.addEventListener("click", () => {
      followId = actor.id;
      renderPanels();
    });
    actorsPanel.append(button);
  }

  speechPanel.replaceChildren();
  if (!scene.speech.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "The world is quiet.";
    speechPanel.append(empty);
  }
  for (const row of [...scene.speech].reverse()) {
    const line = document.createElement("div");
    line.className = "speech-row";
    const speaker = document.createElement("strong");
    speaker.textContent = `${row.name}: `;
    const text = document.createTextNode(`“${row.text}”`);
    const tick = document.createElement("small");
    tick.textContent = `tick ${row.tick}`;
    line.append(speaker, text, tick);
    speechPanel.append(line);
  }
}

function resizeCanvas(): void {
  const rect = canvas.getBoundingClientRect();
  viewWidth = Math.max(1, rect.width);
  viewHeight = Math.max(1, rect.height);
  pixelRatio = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.round(viewWidth * pixelRatio);
  const height = Math.round(viewHeight * pixelRatio);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function scale(): number {
  return (bootstrap?.geometry.tile_size ?? 16) * camera.zoom;
}

function fitWorld(): void {
  if (!scene) return;
  followId = null;
  camera.x = scene.world.x + scene.world.width / 2;
  camera.y = scene.world.y + scene.world.height / 2;
  const tileSize = bootstrap?.geometry.tile_size ?? 16;
  const fitX = (viewWidth - 36) / (scene.world.width * tileSize);
  const fitY = (viewHeight - 36) / (scene.world.height * tileSize);
  camera.zoom = Math.max(0.12, Math.min(4, Math.min(fitX, fitY)));
  renderPanels();
}

function zoomAt(factor: number, clientX?: number, clientY?: number): void {
  if (!scene) return;
  const rect = canvas.getBoundingClientRect();
  const sx = clientX === undefined ? viewWidth / 2 : clientX - rect.left;
  const sy = clientY === undefined ? viewHeight / 2 : clientY - rect.top;
  const worldX = camera.x + (sx - viewWidth / 2) / scale();
  const worldY = camera.y + (sy - viewHeight / 2) / scale();
  camera.zoom = Math.max(0.12, Math.min(4, camera.zoom * factor));
  camera.x = worldX - (sx - viewWidth / 2) / scale();
  camera.y = worldY - (sy - viewHeight / 2) / scale();
}

function chunkKey(cx: number, cy: number): string {
  return `${cx},${cy}`;
}

function ensureChunk(cx: number, cy: number): void {
  if (!bootstrap) return;
  const key = chunkKey(cx, cy);
  if (chunks.has(key) || pendingChunks.has(key) || failedChunks.has(key)) return;
  pendingChunks.add(key);
  const url = bootstrap.endpoints.chunks
    .replace("{cx}", String(cx))
    .replace("{cy}", String(cy));
  void api<Chunk>(url, "force-cache")
    .then((chunk) => chunks.set(key, chunk))
    .catch((error: unknown) => {
      failedChunks.add(key);
      console.warn("Chunk failed", key, error);
    })
    .finally(() => pendingChunks.delete(key));
}

function drawTerrain(time: number): void {
  if (!bootstrap || !scene) return;
  const size = bootstrap.geometry.chunk_size;
  const world = scene.world;
  const left = Math.max(0, Math.floor((camera.x - viewWidth / scale() / 2 - 1) / size));
  const right = Math.min(
    Math.ceil(world.width / size) - 1,
    Math.floor((camera.x + viewWidth / scale() / 2 + 1) / size),
  );
  const top = Math.max(0, Math.floor((camera.y - viewHeight / scale() / 2 - 1) / size));
  const bottom = Math.min(
    Math.ceil(world.height / size) - 1,
    Math.floor((camera.y + viewHeight / scale() / 2 + 1) / size),
  );
  for (let cy = top; cy <= bottom; cy += 1) {
    for (let cx = left; cx <= right; cx += 1) {
      ensureChunk(cx, cy);
      const chunk = chunks.get(chunkKey(cx, cy));
      if (!chunk) continue;
      for (let y = 0; y < chunk.height; y += 1) {
        for (let x = 0; x < chunk.width; x += 1) {
          const id = chunk.tiles[y * chunk.width + x];
          const tile = tileDefinitions.get(id);
          if (!tile) continue;
          const wx = cx * size + x;
          const wy = cy * size + y;
          context.fillStyle = tile.color;
          context.fillRect(wx, wy, 1.02, 1.02);
          if (camera.zoom < 0.45) continue;
          if (id === 0) {
            const drift = ((time / 900 + wx * 0.31 + wy * 0.17) % 1) * 0.45;
            context.fillStyle = "rgba(202, 229, 211, 0.35)";
            context.fillRect(wx + drift, wy + 0.32, 0.42, 0.08);
          } else if (id === 5) {
            context.fillStyle = "#f1cf87";
            context.fillRect(wx + 0.22, wy + 0.25, 0.13, 0.13);
            context.fillStyle = "#d98791";
            context.fillRect(wx + 0.67, wy + 0.63, 0.12, 0.12);
          } else if (id === 6) {
            context.fillStyle = "rgba(52, 63, 55, 0.28)";
            context.fillRect(wx + 0.18, wy + 0.22, 0.28, 0.18);
            context.fillRect(wx + 0.62, wy + 0.67, 0.18, 0.14);
          }
        }
      }
    }
  }
}

function drawRoads(): void {
  if (!scene) return;
  context.lineCap = "round";
  for (const road of scene.roads) {
    context.beginPath();
    context.moveTo(road.path[0].x, road.path[0].y);
    context.lineTo(road.path[1].x, road.path[1].y);
    context.strokeStyle = "rgba(70, 55, 39, 0.38)";
    context.lineWidth = 1.5;
    context.stroke();
    context.strokeStyle = "#b09567";
    context.lineWidth = 0.85;
    context.stroke();
    context.strokeStyle = "rgba(229, 204, 145, 0.32)";
    context.lineWidth = 0.16;
    context.stroke();
  }
}

const PLACE_COLORS: Record<string, string> = {
  HEARTH: "#d9bd7c", THICKET: "#789853", RIVER: "#6c9da2", FLINT: "#a49d88",
  FOREST: "#426c48", POST: "#c48b62", RIDGE: "#a5a49a", SALT: "#d8cfb4", PASTURE: "#9caf68",
};

function drawPlaces(): void {
  if (!scene) return;
  for (const place of scene.places) {
    const color = PLACE_COLORS[place.kind] ?? "#b3a47b";
    context.globalAlpha = 0.26;
    context.fillStyle = color;
    context.beginPath();
    context.arc(place.position.x, place.position.y, 3.5, 0, Math.PI * 2);
    context.fill();
    context.globalAlpha = 0.55;
    context.strokeStyle = color;
    context.lineWidth = 0.16;
    context.stroke();
    context.globalAlpha = 1;
  }
}

function hashText(text: string): number {
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function drawObject(object: SceneObject, time: number): void {
  const { x, y } = object.position;
  if (object.kind === "parcel") {
    context.strokeStyle = "rgba(238, 216, 162, 0.48)";
    context.lineWidth = 0.1;
    context.setLineDash([0.28, 0.22]);
    context.strokeRect(x - 1.25, y - 0.85, 2.5, 1.7);
    context.setLineDash([]);
    return;
  }
  if (object.kind === "facility") {
    if (object.appearance === "fire") {
      context.fillStyle = "#6e6c60";
      for (let index = 0; index < 6; index += 1) {
        const angle = index * Math.PI / 3;
        context.fillRect(x + Math.cos(angle) * 0.48 - 0.12, y + Math.sin(angle) * 0.35 - 0.1, 0.25, 0.2);
      }
      if (object.lit) {
        const flicker = Math.floor(time / 180) % 2;
        context.fillStyle = "#d9683f";
        context.fillRect(x - 0.28, y - 0.62 - flicker * 0.08, 0.56, 0.72);
        context.fillStyle = "#f2c05c";
        context.fillRect(x - 0.13, y - 0.48 - flicker * 0.1, 0.28, 0.5);
      }
      return;
    }
    context.fillStyle = "#72563f";
    context.fillRect(x - 0.65, y - 0.25, 1.3, 0.85);
    context.fillStyle = "#b78b5e";
    context.beginPath();
    context.moveTo(x - 0.85, y - 0.2);
    context.lineTo(x, y - 0.95);
    context.lineTo(x + 0.85, y - 0.2);
    context.fill();
    context.fillStyle = "#332d27";
    context.fillRect(x - 0.14, y + 0.18, 0.28, 0.42);
    return;
  }
  context.globalAlpha = object.depleted ? 0.25 : 1;
  const hue = hashText(object.symbol ?? object.appearance) % 360;
  context.fillStyle = `hsl(${hue} 42% 52%)`;
  for (let index = 0; index < 5; index += 1) {
    const angle = index * 2.4;
    context.fillRect(x + Math.cos(angle) * 0.38 - 0.13, y + Math.sin(angle) * 0.28 - 0.13, 0.27, 0.27);
  }
  context.globalAlpha = 1;
}

const ACTOR_COLORS = ["#e8b76c", "#c98770", "#85b7a0", "#a7a1cf", "#d49aae", "#9dbd75", "#79a9c4"];

function actorColor(actor: Actor): string {
  return ACTOR_COLORS[hashText(actor.id) % ACTOR_COLORS.length];
}

function drawActor(actor: Actor, time: number): void {
  const { x, y } = renderedPosition(actor, time);
  const walking = actor.action === "travel" || actor.action === "walk";
  const frame = Math.floor(time / (walking ? 160 : 420)) % 2;
  const bob = actor.action === "idle" || actor.action === "work" ? frame * -0.07 : 0;
  context.save();
  context.translate(x, y + bob);
  if (actor.action === "incapacitated") context.rotate(Math.PI / 2);
  context.fillStyle = "rgba(27, 37, 30, 0.28)";
  context.fillRect(-0.43, 0.43, 0.86, 0.22);
  context.fillStyle = "#3b332d";
  const stride = walking ? (frame ? 0.15 : -0.15) : 0;
  context.fillRect(-0.3 - stride, 0.18, 0.22, 0.42);
  context.fillRect(0.08 + stride, 0.18, 0.22, 0.42);
  context.fillStyle = actorColor(actor);
  context.fillRect(-0.43, -0.48, 0.86, 0.75);
  const arm = actor.action === "work" ? (frame ? 0.22 : -0.1) : 0;
  context.fillRect(-0.58, -0.35 + arm, 0.16, 0.55);
  context.fillRect(0.42, -0.35 - arm, 0.16, 0.55);
  context.fillStyle = "#d9ad83";
  context.fillRect(-0.3, -0.96, 0.6, 0.5);
  context.fillStyle = "#4b3b32";
  context.fillRect(-0.3, -1.0, 0.6, 0.16);
  context.fillRect(-0.18, -0.75, 0.08, 0.08);
  context.fillRect(0.1, -0.75, 0.08, 0.08);
  context.restore();
}

function worldToScreen(point: Point): Point {
  return {
    x: (point.x - camera.x) * scale() + viewWidth / 2,
    y: (point.y - camera.y) * scale() + viewHeight / 2,
  };
}

function drawLabel(text: string, x: number, y: number, accent = false): void {
  context.font = `${accent ? 12 : 11}px ui-monospace, monospace`;
  context.textAlign = "center";
  context.textBaseline = "middle";
  const width = Math.min(230, context.measureText(text).width + 12);
  context.fillStyle = "rgba(27, 43, 36, 0.82)";
  context.fillRect(x - width / 2, y - 8, width, 16);
  context.fillStyle = accent ? "#f2cf86" : "#f4ead1";
  const shortened = text.length > 42 ? `${text.slice(0, 41)}…` : text;
  context.fillText(shortened, x, y + 0.5, width - 8);
}

function drawLabels(time: number): void {
  if (!scene) return;
  if (camera.zoom >= 0.38) {
    for (const place of scene.places) {
      const point = worldToScreen({ x: place.position.x, y: place.position.y + 3.9 });
      drawLabel(place.name, point.x, point.y, true);
    }
  }
  const latestSpeech = new Map<string, string>();
  for (const row of scene.speech) {
    if (scene.tick - row.tick <= 1) latestSpeech.set(row.entity_id, row.text);
  }
  for (const actor of scene.actors) {
    if (camera.zoom < 0.7 && actor.id !== followId) continue;
    const actorPosition = renderedPosition(actor, time);
    const point = worldToScreen({ x: actorPosition.x, y: actorPosition.y - 1.5 });
    drawLabel(actor.name, point.x, point.y);
    const speech = latestSpeech.get(actor.id);
    if (speech) {
      const bubble = worldToScreen({ x: actorPosition.x, y: actorPosition.y - 2.65 });
      drawLabel(`“${speech}”`, bubble.x, bubble.y);
    }
  }
}

function draw(time: number): void {
  resizeCanvas();
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.fillStyle = "#172d29";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.imageSmoothingEnabled = false;
  if (!scene) {
    requestAnimationFrame(draw);
    return;
  }
  if (followId) {
    const actor = scene.actors.find((row) => row.id === followId);
    if (actor) {
      camera.x += (renderedPosition(actor, time).x - camera.x) * 0.14;
      camera.y += (renderedPosition(actor, time).y - camera.y) * 0.14;
    }
  }

  context.save();
  context.translate(viewWidth / 2, viewHeight / 2);
  context.scale(scale(), scale());
  context.translate(-camera.x, -camera.y);
  drawTerrain(time);
  drawRoads();
  drawPlaces();
  const order: Record<string, number> = { parcel: 0, deposit: 1, facility: 2 };
  for (const object of [...scene.objects].sort((a, b) => order[a.kind] - order[b.kind])) {
    drawObject(object, time);
  }
  for (const actor of scene.actors) drawActor(actor, time);
  context.restore();
  drawLabels(time);
  requestAnimationFrame(draw);
}

interface Drag {
  pointerId: number;
  x: number;
  y: number;
  cameraX: number;
  cameraY: number;
  moved: boolean;
}
interface Pinch {
  distance: number;
  zoom: number;
  worldX: number;
  worldY: number;
}
let drag: Drag | null = null;
let pinch: Pinch | null = null;
let touchWasMulti = false;
const activePointers = new Map<number, Point>();

function pinchFacts(): { distance: number; x: number; y: number } | null {
  const points = [...activePointers.values()];
  if (points.length < 2) return null;
  const [a, b] = points;
  return {
    distance: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)),
    x: (a.x + b.x) / 2,
    y: (a.y + b.y) / 2,
  };
}

function beginPinch(): void {
  const facts = pinchFacts();
  if (!facts || !scene) return;
  const rect = canvas.getBoundingClientRect();
  pinch = {
    distance: facts.distance,
    zoom: camera.zoom,
    worldX: camera.x + (facts.x - rect.left - viewWidth / 2) / scale(),
    worldY: camera.y + (facts.y - rect.top - viewHeight / 2) / scale(),
  };
  drag = null;
}

function updatePinch(): void {
  const facts = pinchFacts();
  if (!facts || !pinch) return;
  camera.zoom = Math.max(0.12, Math.min(4, pinch.zoom * facts.distance / pinch.distance));
  const rect = canvas.getBoundingClientRect();
  camera.x = pinch.worldX - (facts.x - rect.left - viewWidth / 2) / scale();
  camera.y = pinch.worldY - (facts.y - rect.top - viewHeight / 2) / scale();
}

canvas.addEventListener("pointerdown", (event) => {
  if (!scene || (event.pointerType === "mouse" && event.button !== 0)) return;
  canvas.setPointerCapture(event.pointerId);
  activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (activePointers.size === 1) {
    drag = {
      pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      cameraX: camera.x, cameraY: camera.y, moved: false,
    };
  } else {
    touchWasMulti = true;
    beginPinch();
  }
});

canvas.addEventListener("pointermove", (event) => {
  if (!activePointers.has(event.pointerId)) return;
  activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (pinch) {
    updatePinch();
    return;
  }
  if (!drag || drag.pointerId !== event.pointerId) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
  if (drag.moved) {
    if (followId) {
      followId = null;
      renderPanels();
    }
    camera.x = drag.cameraX - dx / scale();
    camera.y = drag.cameraY - dy / scale();
  }
});

function endPointer(event: PointerEvent): void {
  const wasMulti = touchWasMulti;
  const endedDrag = drag?.pointerId === event.pointerId ? drag : null;
  activePointers.delete(event.pointerId);
  if (activePointers.size < 2) pinch = null;
  if (endedDrag && !endedDrag.moved && !wasMulti && scene) {
    const rect = canvas.getBoundingClientRect();
    const point = {
      x: camera.x + (event.clientX - rect.left - viewWidth / 2) / scale(),
      y: camera.y + (event.clientY - rect.top - viewHeight / 2) / scale(),
    };
    const nearest = [...scene.actors].sort((a, b) => {
      const pa = renderedPosition(a);
      const pb = renderedPosition(b);
      const da = (pa.x - point.x) ** 2 + (pa.y - point.y) ** 2;
      const db = (pb.x - point.x) ** 2 + (pb.y - point.y) ** 2;
      return da - db;
    })[0];
    if (nearest) {
      const nearestPosition = renderedPosition(nearest);
      const distance = Math.hypot(nearestPosition.x - point.x, nearestPosition.y - point.y);
      if (distance < 1.2) followId = nearest.id;
    }
  }
  if (activePointers.size === 0) touchWasMulti = false;
  drag = null;
  renderPanels();
}
canvas.addEventListener("pointerup", endPointer);
canvas.addEventListener("pointercancel", endPointer);
canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  followId = null;
  zoomAt(event.deltaY < 0 ? 1.2 : 1 / 1.2, event.clientX, event.clientY);
}, { passive: false });

connectForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void connect();
});
element<HTMLButtonElement>("zoom-in").addEventListener("click", () => zoomAt(1.25));
element<HTMLButtonElement>("zoom-out").addEventListener("click", () => zoomAt(1 / 1.25));
element<HTMLButtonElement>("fit-world").addEventListener("click", fitWorld);
stopFollowing.addEventListener("click", () => {
  followId = null;
  renderPanels();
});

joinWorldButton.addEventListener("click", () => {
  if (!bootstrap || joinWorldButton.disabled) return;
  joinWorldButton.disabled = true;
  setPlayStatus("Joining the world…");
  void post<JoinResult>("/join", {})
    .then(async (joined) => {
      await connect();
      setPlayStatus(`Joined as ${joined.entity.name}. Possess the founder to walk.`);
    })
    .catch((error: unknown) => {
      setPlayStatus(error instanceof Error ? error.message : "Join failed");
    })
    .finally(() => {
      joinWorldButton.disabled = false;
      updatePlayPanel();
    });
});

possessButton.addEventListener("click", () => {
  const entityId = playerEntitySelect.value;
  if (!entityId) return;
  sendWorldCommand({ type: "possess", entity_id: entityId, seq: nextSeq() });
});

releaseButton.addEventListener("click", () => {
  if (!possessedId) return;
  sendWorldCommand({ type: "release", entity_id: possessedId, seq: nextSeq() });
});

sayForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!possessedId) return;
  const text = sayText.value.trim();
  if (!text) return;
  if (sendWorldCommand({
    type: "say", entity_id: possessedId, seq: nextSeq(), text,
  })) {
    sayText.value = "";
    setPlayStatus("Speaking through the engine…");
  }
});

const KEY_DIRECTIONS: Record<string, string> = {
  arrowup: "up", w: "up",
  arrowdown: "down", s: "down",
  arrowleft: "left", a: "left",
  arrowright: "right", d: "right",
};

function updateMovement(): void {
  const rawX = (heldDirections.has("right") ? 1 : 0) - (heldDirections.has("left") ? 1 : 0);
  const rawY = (heldDirections.has("down") ? 1 : 0) - (heldDirections.has("up") ? 1 : 0);
  const length = Math.hypot(rawX, rawY);
  const vector = length > 1
    ? { x: rawX / length, y: rawY / length }
    : { x: rawX, y: rawY };
  if (vector.x === lastMoveVector.x && vector.y === lastMoveVector.y) return;
  lastMoveVector = vector;
  for (const button of dpad.querySelectorAll("button")) {
    button.classList.toggle("held", heldDirections.has((button as HTMLButtonElement).dataset.direction ?? ""));
  }
  if (!possessedId) return;
  sendWorldCommand({
    type: "move", entity_id: possessedId, seq: nextSeq(),
    dx: vector.x, dy: vector.y,
  });
}

function editingText(target: EventTarget | null): boolean {
  return target instanceof HTMLInputElement
    || target instanceof HTMLSelectElement
    || target instanceof HTMLTextAreaElement;
}

window.addEventListener("keydown", (event) => {
  if (!possessedId || editingText(event.target)) return;
  const direction = KEY_DIRECTIONS[event.key.toLowerCase()];
  if (!direction) return;
  event.preventDefault();
  if (!heldDirections.has(direction)) {
    heldDirections.add(direction);
    updateMovement();
  }
});

window.addEventListener("keyup", (event) => {
  const direction = KEY_DIRECTIONS[event.key.toLowerCase()];
  if (!direction || !heldDirections.delete(direction)) return;
  updateMovement();
});

window.addEventListener("blur", () => {
  if (!heldDirections.size) return;
  heldDirections.clear();
  updateMovement();
});

for (const button of dpad.querySelectorAll<HTMLButtonElement>("button[data-direction]")) {
  const direction = button.dataset.direction ?? "";
  button.addEventListener("pointerdown", (event) => {
    if (!possessedId) return;
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    heldDirections.add(direction);
    updateMovement();
  });
  const release = (event: PointerEvent) => {
    if (button.hasPointerCapture(event.pointerId)) button.releasePointerCapture(event.pointerId);
    if (heldDirections.delete(direction)) updateMovement();
  };
  button.addEventListener("pointerup", release);
  button.addEventListener("pointercancel", release);
}

window.addEventListener("beforeunload", closeLiveChannels);

const savedToken = sessionStorage.getItem("econ.world.token");
if (savedToken) {
  tokenInput.value = savedToken;
  void connect();
}
requestAnimationFrame(draw);
