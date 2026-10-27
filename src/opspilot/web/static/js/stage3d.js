// The shared 3D stage (07-design.md §11.3): renderer, tokens → materials, camera and orbit
// limits, keyboard focus, HTML labels, render on demand, theme changes, fallback and disposal.
// Every scene is a small module on top of it. HTML stays canonical: a scene only highlights,
// scrolls to or focuses HTML on this page, or follows a GET link. It never submits, accepts
// or approves anything, and it never fetches.

const THREE_URL = "/static/vendor/three/three.module.min.js";
const MAX_LIVE = 2; // live WebGL contexts per page; a third scene keeps its fallback
const ORBIT_LIMIT = (35 * Math.PI) / 180;
const TOKENS = ["text", "muted", "surface", "raised", "border", "compare", "data", "negative", "warning", "positive", "ground"];

let THREE = null;
let webgl = null;
let observer = null;
const stages = new Map(); // figure → Stage, or a placeholder while its modules load
const waiting = new Set(); // visible figures waiting for a free context
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// §11.5: any of these keeps the 2D fallback.
function allowed(figure) {
  const root = document.documentElement;
  if (root.dataset.views3d !== "on" || figure.dataset.sceneFailed) return false;
  if (window.innerWidth < 768) return false;
  if (reduced() && (figure.dataset.decorative || root.dataset.views3dExplicit !== "on")) return false;
  if ((navigator.deviceMemory || 8) <= 2 || (navigator.hardwareConcurrency || 8) <= 2) return false;
  if (navigator.connection && navigator.connection.saveData) return false;
  if (webgl === null) {
    try {
      const gl = document.createElement("canvas").getContext("webgl2");
      webgl = Boolean(gl);
      gl?.getExtension("WEBGL_lose_context")?.loseContext();
    } catch { webgl = false; }
  }
  return webgl;
}

export function start() {
  if (observer) return;
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) (entry.isIntersecting ? want : release)(entry.target);
  }, { rootMargin: "64px" });
  for (const figure of document.querySelectorAll("figure[data-scene]")) if (allowed(figure)) observer.observe(figure);
  // The live pipeline's data and fallback arrive out of band every 2 s; the canvas stays.
  document.addEventListener("htmx:oobAfterSwap", (event) => {
    const figure = event.detail?.target?.closest?.("figure[data-scene]");
    stages.get(figure)?.rebuild?.();
  });
  document.addEventListener("htmx:beforeCleanupElement", (event) => {
    for (const figure of [...stages.keys(), ...waiting]) if (event.target.contains(figure)) forget(figure);
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    for (const stage of stages.values()) stage.rebuild?.();
  });
}

function want(figure) {
  if (stages.has(figure) || !allowed(figure)) return;
  if (stages.size >= MAX_LIVE) { waiting.add(figure); return; }
  waiting.delete(figure);
  const pending = {};
  stages.set(figure, pending);
  (async () => {
    THREE = THREE || (await import(THREE_URL));
    const module = await import(`/static/js/scenes/${figure.dataset.scene}.js`);
    if (stages.get(figure) !== pending) return; // left the viewport meanwhile
    try { stages.set(figure, new Stage(figure, module)); }
    catch {
      figure.dataset.sceneFailed = "true";
      figure.querySelector(".scene-stage").replaceChildren();
      release(figure);
    }
  })().catch(() => { stages.delete(figure); });
}

function release(figure) {
  waiting.delete(figure);
  const stage = stages.get(figure);
  if (!stage) return;
  stages.delete(figure);
  stage.dispose?.();
  for (const next of waiting) { want(next); break; }
}

function forget(figure) {
  observer?.unobserve(figure);
  release(figure);
}

class Stage {
  constructor(figure, module) {
    const T = THREE;
    this.figure = figure;
    this.module = module;
    this.decorative = Boolean(figure.dataset.decorative);
    this.host = figure.querySelector(".scene-stage");
    this.listeners = [];
    this.owned = [];
    this.labels = [];
    this.items = [];
    this.focused = -1;
    this.hovered = -1;
    this.orbit = { yaw: 0, pitch: 0, zoom: 1 };
    this.frame = 0;

    this.canvas = document.createElement("canvas");
    this.overlay = Object.assign(document.createElement("div"), { className: "scene-labels" });
    this.overlay.setAttribute("aria-hidden", "true");
    this.status = Object.assign(document.createElement("p"), { className: "visually-hidden" });
    this.status.setAttribute("aria-live", "polite");
    if (this.decorative) this.canvas.setAttribute("aria-hidden", "true");
    else { this.canvas.tabIndex = 0; this.canvas.setAttribute("role", "img"); }
    this.host.append(this.canvas, this.overlay, this.status);

    this.renderer = new T.WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: true, powerPreference: "low-power" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.scene = new T.Scene();
    this.camera = new T.PerspectiveCamera(28, 1, 0.1, 500);
    this.raycaster = new T.Raycaster();
    this.root = new T.Group();
    this.scene.add(this.root);

    this.listen(this.canvas, "webglcontextlost", (event) => {
      event.preventDefault();
      figure.dataset.sceneFailed = "true";
      release(figure);
    });
    this.listenInput();
    this.resizer = new ResizeObserver(() => this.resize());
    this.resizer.observe(this.host);
    this.build();
    this.resize();
    this.render();
    figure.classList.add("is-live");
    this.fallback().setAttribute("aria-hidden", "true");
  }

  fallback() { return this.figure.querySelector(".scene-fallback"); }

  listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    this.listeners.push(() => target.removeEventListener(type, handler, options));
  }

  // ------------------------------------------------------------ building

  build() {
    const payload = JSON.parse(this.figure.querySelector("[data-scene-payload]").textContent);
    this.colors = tokens();
    this.items = [];
    this.view = this.module.build(this.kit(), payload);
    this.items = this.view.items || [];
    if (!this.decorative) this.canvas.setAttribute("aria-label", payload.label + (this.items.length ? " Tab moves through the items, Enter opens one." : ""));
    if (this.focused >= this.items.length) this.focused = this.items.length - 1;
    if (this.focused >= 0) this.items[this.focused].focus(true);
    if (this.view.idle && !reduced()) this.loop();
  }

  clear() {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    for (const object of this.owned) object.dispose();
    this.owned = [];
    this.root.clear();
    for (const label of this.labels) label.el.remove();
    this.labels = [];
  }

  rebuild() {
    this.clear();
    this.build();
    this.fallback().setAttribute("aria-hidden", "true");
    this.render();
  }

  kit() {
    const T = THREE;
    const stage = this;
    const own = (thing) => { stage.owned.push(thing); return thing; };
    const vector = (p) => (p instanceof T.Vector3 ? p : new T.Vector3(...p));
    const fill = (color, opacity = 1) => own(new T.MeshBasicMaterial({ color, transparent: opacity < 1, opacity }));
    return {
      T,
      colors: this.colors,
      aspect: this.host.clientWidth / Math.max(this.host.clientHeight, 1), // for layouts that fill the width
      add: (object) => { stage.root.add(object); return object; },
      // A box with ink edges: the one solid every scene is drawn from.
      // `outline: "front"` draws 4 segments instead of 12, for scenes with many boxes (§11.4).
      box(w, h, d, { fill: color, edge, opacity = 1, outline = "all" } = {}) {
        const group = new T.Group();
        const geometry = own(new T.BoxGeometry(w, h, d));
        if (color) group.add(new T.Mesh(geometry, fill(color, opacity)));
        const ink = edge && own(new T.LineBasicMaterial({ color: edge }));
        if (edge && outline === "front") {
          const [x, y, z] = [w / 2, h / 2, d / 2 + 0.001];
          const corners = [[-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]].map((p) => new T.Vector3(...p));
          group.add(new T.LineLoop(own(new T.BufferGeometry().setFromPoints(corners)), ink));
        } else if (edge) group.add(new T.LineSegments(own(new T.EdgesGeometry(geometry)), ink));
        return group;
      },
      // An invisible box that only catches the pointer.
      hit(w, h, d) {
        return new T.Mesh(own(new T.BoxGeometry(w, h, d)), own(new T.MeshBasicMaterial({ visible: false })));
      },
      line(points, { color, dashed = false } = {}) {
        const geometry = own(new T.BufferGeometry().setFromPoints(points.map(vector)));
        const material = own(dashed
          ? new T.LineDashedMaterial({ color, dashSize: 0.18, gapSize: 0.14 })
          : new T.LineBasicMaterial({ color }));
        const line = new T.Line(geometry, material);
        if (dashed) line.computeLineDistances();
        return line;
      },
      // A solid rod between two points: lines are 1px in WebGL, a break must read.
      rod(a, b, radius, color) {
        const from = vector(a), to = vector(b);
        const length = from.distanceTo(to);
        const mesh = new T.Mesh(own(new T.CylinderGeometry(radius, radius, length, 8)), fill(color));
        mesh.position.copy(from).add(to).multiplyScalar(0.5);
        mesh.quaternion.setFromUnitVectors(new T.Vector3(0, 1, 0), to.clone().sub(from).normalize());
        return mesh;
      },
      // Text stays crisp HTML, placed from 3D coordinates on every render.
      label(text, position, className = "", align = "center") {
        const el = document.createElement("span");
        el.className = `scene-label ${className}`.trim();
        el.textContent = text;
        stage.overlay.append(el);
        const handle = { el, position: vector(position), align };
        stage.labels.push(handle);
        return handle;
      },
      item({ object, text, label = null, activate = () => {}, focus = null, hover = null }) {
        return {
          object, text, label, activate,
          focus(on) { label?.el.classList.toggle("focus", on); focus?.(on); },
          hover(on) { hover?.(on); },
        };
      },
      listen: (target, type, handler) => stage.listen(target, type, handler),
      refresh: () => stage.rebuild(),
      go: (id) => goTo(id, true),
      mark: (id) => goTo(id, false),
      open: (href) => window.location.assign(href),
    };
  }

  // ------------------------------------------------------------ drawing

  resize() {
    const width = this.host.clientWidth, height = this.host.clientHeight;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    const before = this.camera.aspect;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    // A layout that spreads across the width is laid out again when the width changes.
    if (this.view?.fillsWidth && Math.abs(before - this.camera.aspect) / this.camera.aspect > 0.1) this.rebuild();
    this.invalidate();
  }

  invalidate() {
    if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = 0; this.render(); });
  }

  render() {
    const T = THREE;
    const c = this.view.camera;
    const yaw = c.yaw + this.orbit.yaw, pitch = c.pitch + this.orbit.pitch, distance = this.fit(c) / this.orbit.zoom;
    const target = new T.Vector3(...c.target);
    this.camera.position.set(
      target.x + distance * Math.sin(yaw) * Math.cos(pitch),
      target.y + distance * Math.sin(pitch),
      target.z + distance * Math.cos(yaw) * Math.cos(pitch),
    );
    this.camera.lookAt(target);
    this.renderer.render(this.scene, this.camera);
    const width = this.host.clientWidth, height = this.host.clientHeight;
    for (const { el, position, align } of this.labels) {
      const p = position.clone().project(this.camera);
      el.hidden = p.z > 1;
      const x = ((p.x + 1) / 2) * width, y = ((1 - p.y) / 2) * height;
      el.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px) translate(${align === "left" ? "6px" : "-50%"}, -50%)`;
    }
  }

  // How far back the camera sits for the scene's width and height to fill this canvas.
  fit({ fit: [width, height], depth = 0 }) {
    const tan = Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2));
    return Math.max(height / 2 / tan, width / 2 / (tan * this.camera.aspect)) * 1.12 + depth / 2;
  }

  // The sign-in hero only: a slow idle turn, paused while the tab is hidden.
  loop() {
    let last = performance.now();
    const step = (now) => {
      if (!document.hidden) {
        this.orbit.yaw += ((this.view.idle * Math.PI) / 180) * ((now - last) / 1000);
        this.render();
      }
      last = now;
      this.frame = requestAnimationFrame(step);
    };
    this.frame = requestAnimationFrame(step);
  }

  // ------------------------------------------------------------ input

  pick(event) {
    const box = this.canvas.getBoundingClientRect();
    const pointer = new THREE.Vector2(((event.clientX - box.left) / box.width) * 2 - 1, -((event.clientY - box.top) / box.height) * 2 + 1);
    this.raycaster.setFromCamera(pointer, this.camera);
    const hit = this.raycaster.intersectObjects(this.items.map((i) => i.object), true)[0];
    if (!hit) return -1;
    return this.items.findIndex((item) => { for (let o = hit.object; o; o = o.parent) if (o === item.object) return true; return false; });
  }

  setFocus(index) {
    if (index === this.focused) return;
    this.items[this.focused]?.focus(false);
    this.focused = index;
    const item = this.items[index];
    if (item) { item.focus(true); this.status.textContent = item.text; }
    this.invalidate();
  }

  setHover(index) {
    if (index === this.hovered) return;
    this.items[this.hovered]?.hover(false);
    this.hovered = index;
    this.items[index]?.hover(true);
    this.canvas.classList.toggle("pointing", index >= 0);
    this.invalidate();
  }

  turn(yaw, pitch) {
    const clamp = (v) => Math.max(-ORBIT_LIMIT, Math.min(ORBIT_LIMIT, v));
    this.orbit.yaw = clamp(this.orbit.yaw + yaw);
    this.orbit.pitch = clamp(this.orbit.pitch + pitch);
    this.invalidate();
  }

  zoom(factor) {
    this.orbit.zoom = Math.max(0.7, Math.min(2.2, this.orbit.zoom * factor));
    this.invalidate();
  }

  listenInput() {
    if (this.decorative) return;
    let drag = null;
    this.listen(this.canvas, "pointerdown", (event) => { drag = { x: event.clientX, y: event.clientY, moved: 0 }; });
    this.listen(window, "pointerup", () => { setTimeout(() => { drag = null; }, 0); });
    this.listen(this.canvas, "pointermove", (event) => {
      if (drag && event.buttons && this.view.orbit) {
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
        drag = { x: event.clientX, y: event.clientY, moved: drag.moved + Math.abs(dx) + Math.abs(dy) };
        this.turn(-dx * 0.006, dy * 0.006);
      } else this.setHover(this.pick(event));
    });
    this.listen(this.canvas, "pointerleave", () => this.setHover(-1));
    this.listen(this.canvas, "click", (event) => {
      if (drag && drag.moved > 4) return;
      const index = this.pick(event);
      if (index < 0) return;
      this.setFocus(index);
      this.items[index].activate();
    });
    this.listen(this.canvas, "wheel", (event) => {
      if (!this.view.orbit || document.activeElement !== this.canvas) return; // the page keeps scrolling
      event.preventDefault();
      this.zoom(event.deltaY < 0 ? 1.08 : 1 / 1.08);
    }, { passive: false });
    this.listen(this.canvas, "blur", () => this.setFocus(-1));
    this.listen(this.canvas, "keydown", (event) => {
      const count = this.items.length;
      const move = (step) => {
        const next = this.focused < 0 ? (step > 0 ? 0 : count - 1) : this.focused + step;
        if (next < 0 || next >= count) return false;
        this.setFocus(next);
        return true;
      };
      const orbitKeys = { ArrowLeft: [-0.12, 0], ArrowRight: [0.12, 0], ArrowUp: [0, 0.08], ArrowDown: [0, -0.08] };
      if (event.key === "Tab" && !event.altKey && !event.ctrlKey && !event.metaKey) {
        if (move(event.shiftKey ? -1 : 1)) event.preventDefault(); // past either end, Tab leaves the scene
      } else if (event.key in orbitKeys && this.view.orbit) {
        event.preventDefault();
        this.turn(...orbitKeys[event.key]);
      } else if (event.key in orbitKeys) {
        event.preventDefault();
        move(event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1);
      } else if ((event.key === "+" || event.key === "=") && this.view.orbit) { this.zoom(1.12); }
      else if (event.key === "-" && this.view.orbit) { this.zoom(1 / 1.12); }
      else if (event.key === "Enter" && this.focused >= 0) {
        event.preventDefault();
        this.items[this.focused].activate();
      }
    });
  }

  dispose() {
    this.clear();
    for (const remove of this.listeners) remove();
    this.resizer.disconnect();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.canvas.remove();
    this.overlay.remove();
    this.status.remove();
    this.figure.classList.remove("is-live");
    this.fallback()?.removeAttribute("aria-hidden");
  }
}

function tokens() {
  const style = getComputedStyle(document.documentElement);
  const colors = {};
  for (const name of TOKENS) colors[name] = new THREE.Color(style.getPropertyValue(`--${name}`).trim() || "#888888");
  return colors;
}

// Highlight (and with `focus`, scroll to and focus) HTML on this page, opening any closed
// disclosure around it. The one thing a scene does to the page.
function goTo(id, focus) {
  const target = id && document.getElementById(id);
  if (!target) return;
  for (let d = target.closest("details"); d; d = d.parentElement?.closest("details")) d.open = true;
  for (const old of document.querySelectorAll(focus ? ".scene-selected" : ".scene-marked")) old.classList.remove(focus ? "scene-selected" : "scene-marked");
  target.classList.add(focus ? "scene-selected" : "scene-marked");
  if (!focus) return;
  if (!target.matches("a, button, input, select, textarea, [tabindex]")) target.tabIndex = -1;
  target.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
  target.focus({ preventScroll: true });
}
