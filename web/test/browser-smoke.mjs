import assert from "node:assert/strict";
import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";
import { WebSocketServer } from "ws";

const root = fileURLToPath(new URL("../dist", import.meta.url));
const cacheKey = "browser-smoke.v1";
const world = { x: 0, y: 0, width: 16, height: 16 };
const clock = {
  tick: 1, day: 1, hour: 8, is_day: true, is_night: false,
  daylight_hours: "06:00-20:00", season: "spring", season_day: 1,
  days_per_season: 24,
};
const geometry = {
  id: "tile2d.v1", kind: "tile2d", version: 1,
  generator_version: "browser-smoke.v1", cache_key: cacheKey,
  seed: "browser-smoke", tile_size: 16, chunk_size: 8, world,
  coordinates: "world tile coordinates",
};
const assets = {
  tiles: [
    { id: 0, key: "water", color: "#477e91", role: "decor" },
    { id: 1, key: "meadow", color: "#668752", role: "decor" },
    { id: 2, key: "forest", color: "#385b3d", role: "decor" },
    { id: 3, key: "stone", color: "#77766e", role: "decor" },
    { id: 4, key: "path", color: "#a78f61", role: "decor" },
    { id: 5, key: "flowers", color: "#718d59", role: "decor" },
    { id: 6, key: "rock", color: "#5f685d", role: "decor" },
  ],
  actor: {
    frame_width: 16, frame_height: 16,
    animations: {
      idle: { frames: [0, 1], frame_ms: 420 },
      work: { frames: [2, 3], frame_ms: 300 },
      walk: { frames: [4, 5], frame_ms: 180 },
      travel: { frames: [4, 5, 6, 7], frame_ms: 120 },
      incapacitated: { frames: [8], frame_ms: 1000 },
    },
  },
  note: "browser smoke fixture",
};
const baseActor = {
  id: "e-aster", name: "Aster", entity_type: "individual",
  status: "active", place: "HEARTH", appearance: "individual",
  action: "idle", action_progress: null, position: { x: 8, y: 8 },
};

const playerActor = {
  id: "e-player", name: "Player One", entity_type: "individual",
  status: "active", place: "HEARTH", appearance: "individual",
  action: "idle", action_progress: null, position: { x: 9, y: 8 },
};

function scene(revision, tick, actor, speech) {
  return {
    changed: true, revision, geometry: "tile2d.v1",
    geometry_revision: cacheKey, tick, clock: { ...clock, tick }, world,
    places: [{
      key: "HEARTH", name: "Hearth", kind: "HEARTH", region_id: "",
      description: "A warm test hearth", position: { x: 8, y: 8 },
    }],
    roads: [], actors: [actor, playerActor], objects: [], unplaced_actor_count: 0,
    speech,
  };
}

const initialScene = scene("1:rev1", 1, baseActor, [
  { tick: 1, entity_id: baseActor.id, name: "Aster", text: "hello, browser" },
]);
const helloScene = scene("2:rev2", 2, { ...baseActor, action: "work" }, initialScene.speech);
const deltaScene = scene("3:rev3", 3, { ...baseActor, action: "travel", action_progress: 0.5 }, [
  ...initialScene.speech,
  { tick: 3, entity_id: baseActor.id, name: "Aster", text: "the websocket is live" },
]);
let joined = false;
const bootstrap = {
  protocol: "econ-world.v1",
  session: { role: "spectator", user_id: "u-browser", entity_id: null, capabilities: [] },
  geometry, assets,
  player_entities: [],
  endpoints: {
    scene: "/world/scene",
    chunks: `/world/chunks/{cx}/{cy}?v=${cacheKey}`,
    events: "/rounds/events",
    stream: "/world/stream",
  },
  scene: initialScene,
};

function currentBootstrap() {
  return {
    ...bootstrap,
    player_entities: joined
      ? [{ id: playerActor.id, name: playerActor.name, place: "HEARTH" }]
      : [],
  };
}

function sendJson(response, value) {
  response.writeHead(200, { "content-type": "application/json" });
  response.end(JSON.stringify(value));
}

async function waitFor(predicate, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("Timed out waiting for browser smoke condition");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

function serveStatic(request, response) {
  const url = new URL(request.url ?? "/", "http://world.test");
  const pathname = url.pathname === "/" ? "/index.html" : url.pathname;
  const path = normalize(join(root, pathname));
  if (!path.startsWith(root) || !existsSync(path) || !statSync(path).isFile()) {
    response.writeHead(404);
    response.end("not found");
    return;
  }
  const types = {
    ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
    ".svg": "image/svg+xml", ".png": "image/png",
  };
  response.writeHead(200, { "content-type": types[extname(path)] ?? "application/octet-stream" });
  createReadStream(path).pipe(response);
}

const sockets = new Set();
const server = createServer((request, response) => {
  const url = new URL(request.url ?? "/", "http://world.test");
  if (url.pathname === "/favicon.ico") {
    response.writeHead(204);
    response.end();
    return;
  }
  const needsAuth = url.pathname.startsWith("/world/") || url.pathname === "/join";
  if (needsAuth && request.headers.authorization !== "Bearer browser-token") {
    response.writeHead(401, { "content-type": "application/json" });
    response.end(JSON.stringify({ detail: "missing bearer token" }));
    return;
  }
  if (url.pathname === "/join") {
    if (request.method !== "POST") {
      response.writeHead(405);
      response.end();
      return;
    }
    request.resume();
    request.on("end", () => {
      joined = true;
      sendJson(response, { entity: { id: playerActor.id, name: playerActor.name } });
    });
  } else if (url.pathname === "/world/bootstrap") {
    sendJson(response, currentBootstrap());
  } else if (url.pathname === "/world/scene") {
    const since = url.searchParams.get("since");
    sendJson(response, since === deltaScene.revision
      ? { changed: false, revision: deltaScene.revision, tick: deltaScene.tick }
      : deltaScene);
  } else if (url.pathname === "/world/chunks/0/0" || url.pathname === "/world/chunks/1/0"
      || url.pathname === "/world/chunks/0/1" || url.pathname === "/world/chunks/1/1") {
    const tiles = Array.from({ length: 64 }, (_, index) => {
      const x = index % 8;
      const y = Math.floor(index / 8);
      return x < 2 ? 0 : y < 2 ? 3 : (x + y) % 7;
    });
    sendJson(response, {
      geometry: "tile2d.v1", generator_version: "browser-smoke.v1",
      cache_key: cacheKey, revision: `${cacheKey}:chunk`, cx: 0, cy: 0,
      width: 8, height: 8, tiles,
    });
  } else if (url.pathname === "/rounds/events") {
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    response.write(": browser smoke\n\n");
  } else {
    serveStatic(request, response);
  }
});
server.on("connection", (socket) => {
  sockets.add(socket);
  socket.on("close", () => sockets.delete(socket));
});

const wss = new WebSocketServer({ server, path: "/world/stream" });
let streamConnections = 0;
const receivedMoves = [];
wss.on("connection", (socket) => {
  streamConnections += 1;
  const connectionNumber = streamConnections;
  let authenticated = false;
  const presence = (velocity = { x: 0, y: 0 }) => ({
    id: playerActor.id, place: "HEARTH",
    position: { ...playerActor.position }, velocity,
    bounds: { x: 4, y: 4, width: 8, height: 8 },
    action: "walk", control: "player",
  });
  socket.on("message", (raw) => {
    const message = JSON.parse(String(raw));
    if (!authenticated) {
      if (message.type !== "auth" || message.token !== "browser-token") {
        socket.send(JSON.stringify({ type: "error", reason: "unauthorized" }));
        socket.close(4401);
        return;
      }
      authenticated = true;
      const hello = connectionNumber === 1 ? helloScene : deltaScene;
      socket.send(JSON.stringify({
        type: "hello", bootstrap: { ...currentBootstrap(), scene: hello },
      }));
      if (connectionNumber !== 1) return;
      setTimeout(() => {
        socket.send(JSON.stringify({
          type: "scene_delta",
          base_revision: helloScene.revision,
          revision: deltaScene.revision,
          tick: deltaScene.tick,
          clock: deltaScene.clock,
          unplaced_actor_count: 0,
          actors: deltaScene.actors,
          speech: deltaScene.speech,
        }));
        setTimeout(() => socket.close(), 2500);
      }, 100);
      return;
    }
    if (message.type === "possess") {
      socket.send(JSON.stringify({
        type: "command_result", command: "possess", status: "accepted",
        entity_id: playerActor.id, seq: message.seq, actor: presence(),
      }));
      socket.send(JSON.stringify({
        type: "presence_delta", revision: 1, actors: [presence()], released: [],
      }));
    } else if (message.type === "move") {
      receivedMoves.push({ dx: message.dx, dy: message.dy });
      const actor = presence({ x: message.dx, y: message.dy });
      socket.send(JSON.stringify({
        type: "command_result", command: "move", status: "accepted",
        entity_id: playerActor.id, seq: message.seq, actor,
      }));
      socket.send(JSON.stringify({
        type: "presence_delta", revision: 2, actors: [actor], released: [],
      }));
    } else if (message.type === "say") {
      socket.send(JSON.stringify({
        type: "command_result", command: "say", status: "queued",
        entity_id: playerActor.id, seq: message.seq,
      }));
    } else if (message.type === "release") {
      socket.send(JSON.stringify({
        type: "command_result", command: "release", status: "accepted",
        entity_id: playerActor.id, seq: message.seq,
      }));
      socket.send(JSON.stringify({
        type: "presence_delta", revision: 3, actors: [], released: [playerActor.id],
      }));
    }
  });
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const { port } = server.address();
const executablePath = process.env.CHROME_BIN
  ?? ["/snap/bin/chromium", "/usr/bin/chromium", "/usr/bin/google-chrome"]
    .find((candidate) => existsSync(candidate));
assert.ok(executablePath, "No system Chromium found; set CHROME_BIN");

const browser = await puppeteer.launch({
  executablePath,
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const consoleErrors = [];
try {
  const page = await browser.newPage();
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1, isMobile: true });
  await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "networkidle0" });
  await page.locator("#token").fill("browser-token");
  await page.locator("#connect").click();
  await page.waitForFunction(
    () => document.querySelector("#map-status")?.textContent?.includes("1 places"),
  );
  await page.waitForFunction(
    () => document.querySelector("#connection-status")?.textContent?.includes("Watching live"),
  );
  await page.waitForFunction(
    () => document.querySelector(".actor-row")?.textContent?.includes("travel"),
  );
  await page.waitForFunction(
    () => document.querySelector(".speech-row")?.textContent?.includes("the websocket is live"),
  );

  await page.waitForFunction(
    () => document.querySelector("#join-world") && !document.querySelector("#join-world").hidden,
  );
  await page.locator("#join-world").click();
  await page.waitForFunction(
    () => document.querySelector("#possess-entity") && !document.querySelector("#possess-entity").hidden,
  );

  await page.locator("#possess-entity").click();
  await page.waitForFunction(
    () => document.querySelector("#session-badge")?.textContent?.includes("Playing"),
  );
  await page.keyboard.down("ArrowRight");
  await page.waitForFunction(
    () => document.querySelector("#play-status")?.textContent?.includes("Walking"),
  );
  await page.keyboard.up("ArrowRight");
  await page.tap("#dpad [data-direction='down']");
  await waitFor(() => receivedMoves.some((move) => move.dy === 1));
  await waitFor(() => receivedMoves.filter((move) => move.dy === 0).length >= 2);
  await page.locator("#say-text").fill("hello from the browser");
  await page.locator("#say-form button[type='submit']").click();
  await page.waitForFunction(
    () => document.querySelector("#play-status")?.textContent?.includes("Speech queued"),
  );
  await page.locator("#release-entity").click();
  await page.waitForFunction(
    () => document.querySelector("#session-badge")?.textContent?.includes("Spectator"),
  );

  const phoneCanvas = await page.evaluate(() => {
    const canvas = document.querySelector("#world");
    const context = canvas.getContext("2d");
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    const colors = new Set();
    for (let index = 0; index < pixels.length; index += 401 * 4) {
      colors.add(`${pixels[index]},${pixels[index + 1]},${pixels[index + 2]},${pixels[index + 3]}`);
    }
    const rect = canvas.getBoundingClientRect();
    return { colors: colors.size, width: rect.width, height: rect.height };
  });
  assert.ok(phoneCanvas.colors > 8, `expected a varied rendered map, got ${phoneCanvas.colors} colors`);
  assert.ok(phoneCanvas.width > 250 && phoneCanvas.height > 300);

  await page.locator(".actor-row").click();
  await page.waitForFunction(() => !document.querySelector("#stop-following").hidden);
  assert.match(
    await page.$eval(".actor-row", (node) => node.className),
    /following/,
  );

  await page.setViewport({ width: 1280, height: 800, deviceScaleFactor: 1, isMobile: false });
  await new Promise((resolve) => setTimeout(resolve, 150));
  const desktopCanvas = await page.evaluate(() => {
    const rect = document.querySelector("#world").getBoundingClientRect();
    return { width: rect.width, height: rect.height };
  });
  assert.ok(desktopCanvas.width > phoneCanvas.width * 1.5);
  assert.ok(desktopCanvas.height > 500);

  const screenshot = await page.screenshot({ fullPage: true });
  assert.ok(screenshot.length > 20_000, "expected a non-trivial rendered page screenshot");

  await waitFor(() => streamConnections >= 2);
  assert.ok(streamConnections >= 2, "expected the client to reconnect the world stream");
  await page.waitForFunction(
    () => document.querySelector("#connection-status")?.textContent?.includes("Watching live"),
  );
  assert.deepEqual(consoleErrors, []);
} finally {
  await browser.close();
  wss.close();
  server.close();
  for (const socket of sockets) socket.destroy();
}

console.log("browser smoke: rendered phone/desktop, join, live delta, keyboard/touch possession, follow mode");
