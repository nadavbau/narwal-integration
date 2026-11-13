const CARD_VERSION = "2.0.0";

const MODES = [
  { label: "Vacuum & Mop", value: "Vacuum & Mop" },
  { label: "Vac → Mop", value: "Vacuum then Mop" },
  { label: "Vacuum", value: "Vacuum Only" },
  { label: "Mop", value: "Mop Only" },
];

const WETNESS_LABELS = { dry: "Dry", normal: "Normal", wet: "Wet" };

// Dock actions shown when the matching button entity exists.
const DOCK_ACTIONS = [
  { key: "dry_mop", label: "Dry mops", icon: "mdi:fan" },
  { key: "wash_and_dry_mop", label: "Wash & dry", icon: "mdi:autorenew" },
  { key: "empty_dustbin", label: "Empty bin", icon: "mdi:delete-empty" },
];

/* ================================================================
 *  Visual Config Editor  (uses ha-form for reliable rendering)
 * ================================================================ */

const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "vacuum" } } },
  { name: "camera_entity", selector: { entity: { domain: "camera" } } },
  { name: "battery_entity", selector: { entity: { domain: "sensor" } } },
  { name: "mode_entity", selector: { entity: { domain: "select" } } },
];

class NarwalVacuumCardEditor extends HTMLElement {
  _config = {};
  _hass = null;

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  _render() {
    if (!this._hass) return;

    if (!this._form) {
      this.innerHTML = "";
      this._form = document.createElement("ha-form");
      this._form.schema = EDITOR_SCHEMA;
      this._form.computeLabel = (schema) => {
        const labels = {
          entity: "Vacuum Entity",
          camera_entity: "Camera Entity (Map)",
          battery_entity: "Battery Sensor",
          mode_entity: "Clean Mode Select",
        };
        return labels[schema.name] || schema.name;
      };
      this._form.addEventListener("value-changed", (ev) => {
        const newConfig = ev.detail?.value;
        if (!newConfig) return;
        const clean = { ...newConfig };
        for (const key of ["camera_entity", "battery_entity", "mode_entity"]) {
          if (!clean[key]) delete clean[key];
        }
        this._config = clean;
        this.dispatchEvent(new CustomEvent("config-changed", {
          detail: { config: clean },
          bubbles: true,
          composed: true,
        }));
      });
      this.appendChild(this._form);
    }

    this._form.hass = this._hass;
    this._form.data = this._config;
  }
}

if (!customElements.get("narwal-vacuum-card-editor")) {
  customElements.define("narwal-vacuum-card-editor", NarwalVacuumCardEditor);
}

/* ================================================================
 *  Main Card
 * ================================================================ */

const ICON = (icon, size = 20) => `<ha-icon icon="${icon}" style="--mdc-icon-size:${size}px"></ha-icon>`;

class NarwalVacuumCard extends HTMLElement {
  constructor() {
    super();
    this._selectedRooms = new Set();
    this._selectedMode = "Vacuum & Mop";
    this._initialized = false;
    this._mapPicture = null;
    this._lastMapRefresh = 0;
  }

  static getConfigElement() {
    return document.createElement("narwal-vacuum-card-editor");
  }

  static getStubConfig(hass) {
    const vac = hass && Object.values(hass.entities || {}).find(
      (e) => e.platform === "narwal" && e.entity_id.startsWith("vacuum.")
    );
    return { entity: vac ? vac.entity_id : "vacuum.narwal_vacuum" };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._build();
      this._initialized = true;
    }
    this._update();
  }

  setConfig(config) {
    if (!config.entity) throw new Error("'entity' is required (vacuum entity ID)");
    this._config = { ...config };
    if (this._initialized) {
      this._initialized = false;
      if (this.shadowRoot) this.shadowRoot.innerHTML = "";
    }
  }

  getCardSize() {
    return 8;
  }

  /* --- Entity resolution: explicit config > same device by key > name match --- */

  _sibling(domain, key, configKey, legacyMatch) {
    if (configKey && this._config[configKey]) return this._config[configKey];
    const entities = this._hass.entities || {};
    const vac = entities[this._config.entity];
    if (vac && vac.device_id) {
      const hit = Object.values(entities).find(
        (e) => e.device_id === vac.device_id
          && e.entity_id.startsWith(domain + ".")
          && e.translation_key === key
      );
      if (hit) return hit.entity_id;
    }
    if (!legacyMatch) return null;
    return Object.keys(this._hass.states).find(
      (e) => e.startsWith(domain + ".") && e.includes("narwal") && legacyMatch(e)
    ) || null;
  }

  _camera() { return this._sibling("camera", "map", "camera_entity", (e) => true); }
  _battery() { return this._sibling("sensor", "battery", "battery_entity", (e) => e.includes("battery")); }
  _modeSelect() { return this._sibling("select", "clean_mode", "mode_entity", (e) => e.includes("clean_mode")); }
  _wetnessSelect() { return this._sibling("select", "mop_humidity"); }

  _state(entityId) {
    return entityId ? this._hass.states[entityId] : undefined;
  }

  /* --- Build ------------------------------------------------------ */

  _build() {
    if (this.shadowRoot) {
      this.shadowRoot.innerHTML = "";
    } else {
      this.attachShadow({ mode: "open" });
    }
    this.shadowRoot.innerHTML = `
      <ha-card>
        <style>
          :host { --primary: var(--primary-color, #03a9f4); }
          .card { padding: 16px; font-family: var(--ha-card-header-font-family, inherit); }
          .header { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
          .header .name { font-size: 1.1em; font-weight: 500; flex: 1; }
          .header .battery { display: flex; align-items: center; gap: 4px;
            font-size: 0.9em; color: var(--secondary-text-color); }
          .status { font-size: 0.9em; color: var(--secondary-text-color); margin: 0 0 12px 40px; }
          .banner { display: none; align-items: center; gap: 8px; border-radius: 10px;
            padding: 8px 12px; margin-bottom: 8px; font-size: 0.85em; }
          .banner.show { display: flex; }
          .banner.error { background: rgba(219, 68, 55, 0.15); color: var(--error-color, #db4437); }
          .banner.maint { background: rgba(255, 166, 0, 0.15); color: var(--warning-color, #ffa600); }
          .map-container { width: 100%; border-radius: 12px; overflow: hidden;
            background: var(--card-background-color, #1c1c1c); margin: 8px 0 16px;
            aspect-ratio: 4/3; display: flex; align-items: center; justify-content: center; }
          .map-container img { width: 100%; height: 100%; object-fit: contain; }
          .map-container .no-map { color: var(--secondary-text-color); font-size: 0.85em; }
          .section-label { font-size: 0.75em; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.5px; color: var(--secondary-text-color); margin-bottom: 8px; }
          .row-head { display: flex; align-items: baseline; gap: 12px; }
          .select-link { font-size: 0.75em; cursor: pointer; color: var(--primary); user-select: none; }
          .select-link:hover { text-decoration: underline; }
          .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
          .chip { border: 1.5px solid var(--divider-color, #444); border-radius: 20px;
            padding: 6px 14px; font-size: 0.85em; cursor: pointer; transition: all 0.15s;
            background: transparent; color: var(--primary-text-color);
            font-family: inherit; user-select: none; }
          .chip:hover:not(:disabled) { border-color: var(--primary); }
          .chip.active { background: var(--primary); color: #fff; border-color: var(--primary); }
          .chip:disabled { opacity: 0.4; cursor: not-allowed; text-decoration: line-through; }
          .chips.fill .chip { flex: 1; min-width: 64px; border-radius: 12px; text-align: center; }
          .group.hidden { display: none; }
          .actions { display: flex; gap: 8px; }
          .action-btn { flex: 1; border: none; border-radius: 12px; padding: 12px 8px;
            font-size: 0.9em; font-weight: 500; cursor: pointer; transition: all 0.15s;
            font-family: inherit; display: flex; align-items: center; justify-content: center; gap: 6px; }
          .action-btn.start { background: var(--primary); color: #fff; flex: 2; }
          .action-btn.start:hover { filter: brightness(1.1); }
          .action-btn.secondary { background: var(--divider-color, #333); color: var(--primary-text-color); }
          .action-btn.secondary:hover { filter: brightness(1.2); }
          .dock { display: flex; gap: 8px; margin-top: 8px; }
          .dock .action-btn { padding: 8px 6px; font-size: 0.8em; font-weight: 400; }
        </style>
        <div class="card">
          <div class="header">
            ${ICON("mdi:robot-vacuum", 28)}
            <span class="name">Narwal Vacuum</span>
            <span class="battery">${ICON("mdi:battery", 18)}<span class="battery-val">--</span></span>
          </div>
          <div class="status">--</div>
          <div class="banner error">${ICON("mdi:alert-circle-outline", 18)}<span class="text"></span></div>
          <div class="banner maint">${ICON("mdi:wrench-clock", 18)}<span class="text"></span></div>
          <div class="map-container"><span class="no-map">No map available</span></div>

          <div class="row-head">
            <div class="section-label">Rooms</div>
            <a class="select-link" id="select-all">Select all</a>
            <a class="select-link" id="select-none">Clear</a>
          </div>
          <div class="chips" id="rooms"></div>

          <div class="section-label">Cleaning mode</div>
          <div class="chips fill" id="modes"></div>

          <div class="group" id="suction-group">
            <div class="section-label">Suction</div>
            <div class="chips fill" id="suction"></div>
          </div>

          <div class="group" id="wetness-group">
            <div class="section-label">Mop wetness</div>
            <div class="chips fill" id="wetness"></div>
          </div>

          <div class="actions" id="actions"></div>
          <div class="dock" id="dock"></div>
        </div>
      </ha-card>
    `;
    const root = this.shadowRoot;
    root.getElementById("select-all").addEventListener("click", () => this._selectAll());
    root.getElementById("select-none").addEventListener("click", () => this._selectNone());
    this._buildModes();
    this._buildActions();
  }

  _chip(label, onClick) {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.textContent = label;
    btn.addEventListener("click", onClick);
    return btn;
  }

  _buildModes() {
    const container = this.shadowRoot.getElementById("modes");
    container.innerHTML = "";
    for (const m of MODES) {
      const btn = this._chip(m.label, () => {
        this._selectedMode = m.value;
        this._syncModeEntity();
        this._updateModes();
      });
      btn.dataset.mode = m.value;
      container.appendChild(btn);
    }
  }

  _buildActions() {
    const container = this.shadowRoot.getElementById("actions");
    container.innerHTML = "";
    const add = (cls, html, onClick, id) => {
      const btn = document.createElement("button");
      btn.className = `action-btn ${cls}`;
      if (id) btn.id = id;
      btn.innerHTML = html;
      btn.addEventListener("click", onClick);
      container.appendChild(btn);
      return btn;
    };
    add("start", `${ICON("mdi:play")} Start`, () => this._primaryAction(), "start-btn");
    add("secondary", `${ICON("mdi:stop")} Stop`, () => this._vacuum("stop"));
    add("secondary", `${ICON("mdi:home")} Dock`, () => this._vacuum("return_to_base"));
    add("secondary", ICON("mdi:map-marker"), () => this._vacuum("locate"));
  }

  /* --- Update ----------------------------------------------------- */

  _update() {
    if (!this._hass || !this._config) return;
    const entity = this._hass.states[this._config.entity];
    if (!entity) return;
    const root = this.shadowRoot;
    const attrs = entity.attributes;

    const battery = this._batteryLevel(entity);
    root.querySelector(".battery-val").textContent = battery != null ? `${Math.round(battery)}%` : "--";
    if (battery != null) {
      const level = Math.round(battery / 10) * 10;
      root.querySelector(".battery ha-icon").setAttribute(
        "icon", level >= 100 ? "mdi:battery" : level <= 0 ? "mdi:battery-outline" : `mdi:battery-${level}`
      );
    }

    let status = attrs.status || entity.state || "--";
    if (attrs.current_room && entity.state === "cleaning") status += ` · ${attrs.current_room}`;
    root.querySelector(".status").textContent = status;

    this._updateBanners();
    this._updateRooms(attrs.rooms || {}, new Set((attrs.no_go_rooms || []).map(Number)));
    this._syncModeFromEntity();
    this._updateModes();
    this._updateSuction(attrs);
    this._updateWetness();
    this._updateMap();
    this._updateStartButton(entity.state);
    this._updateDock();
  }

  _batteryLevel(entity) {
    const s = this._state(this._battery());
    if (s && !isNaN(parseFloat(s.state))) return parseFloat(s.state);
    return entity.attributes.battery_level ?? null;
  }

  _updateBanners() {
    const root = this.shadowRoot;
    const err = this._state(this._sibling("sensor", "error"));
    const errBanner = root.querySelector(".banner.error");
    const hasError = err && !["No error", "unknown", "unavailable"].includes(err.state);
    errBanner.classList.toggle("show", !!hasError);
    if (hasError) errBanner.querySelector(".text").textContent = err.state;

    const maint = this._state(this._sibling("sensor", "consumables_due"));
    const items = (maint && maint.attributes.items) || [];
    const maintBanner = root.querySelector(".banner.maint");
    maintBanner.classList.toggle("show", items.length > 0);
    if (items.length) maintBanner.querySelector(".text").textContent = items.join(" · ");
  }

  _updateRooms(rooms, noGo) {
    const container = this.shadowRoot.getElementById("rooms");
    const entries = Object.entries(rooms);
    const signature = JSON.stringify([entries, [...noGo]]);
    if (container.dataset.sig !== signature) {
      container.dataset.sig = signature;
      container.innerHTML = "";
      for (const [id, name] of entries) {
        const rid = Number(id);
        const btn = this._chip(name, () => {
          if (this._selectedRooms.has(rid)) this._selectedRooms.delete(rid);
          else this._selectedRooms.add(rid);
          btn.classList.toggle("active", this._selectedRooms.has(rid));
          this._updateStartButton();
        });
        btn.dataset.roomId = id;
        if (noGo.has(rid)) {
          btn.disabled = true;
          btn.title = "No-go room in the Narwal app";
          this._selectedRooms.delete(rid);
        }
        container.appendChild(btn);
      }
    }
    for (const btn of container.children) {
      btn.classList.toggle("active", this._selectedRooms.has(Number(btn.dataset.roomId)));
    }
  }

  _updateModes() {
    for (const btn of this.shadowRoot.getElementById("modes").children) {
      btn.classList.toggle("active", btn.dataset.mode === this._selectedMode);
    }
    // Suction only matters when vacuuming, wetness only when mopping.
    this.shadowRoot.getElementById("suction-group").classList.toggle("hidden", this._selectedMode === "Mop Only");
    this.shadowRoot.getElementById("wetness-group").classList.toggle(
      "hidden", this._selectedMode === "Vacuum Only" || !this._wetnessSelect()
    );
  }

  _updateSuction(attrs) {
    const container = this.shadowRoot.getElementById("suction");
    const speeds = attrs.fan_speed_list || [];
    if (container.dataset.sig !== speeds.join("|")) {
      container.dataset.sig = speeds.join("|");
      container.innerHTML = "";
      for (const speed of speeds) {
        const btn = this._chip(speed, () => this._hass.callService("vacuum", "set_fan_speed", {
          entity_id: this._config.entity, fan_speed: speed,
        }));
        btn.dataset.value = speed;
        container.appendChild(btn);
      }
    }
    for (const btn of container.children) btn.classList.toggle("active", btn.dataset.value === attrs.fan_speed);
  }

  _updateWetness() {
    const select = this._wetnessSelect();
    const state = this._state(select);
    const container = this.shadowRoot.getElementById("wetness");
    if (!state) { container.innerHTML = ""; return; }
    const options = state.attributes.options || [];
    if (container.dataset.sig !== options.join("|")) {
      container.dataset.sig = options.join("|");
      container.innerHTML = "";
      for (const option of options) {
        const btn = this._chip(WETNESS_LABELS[option] || option, () => this._hass.callService(
          "select", "select_option", { entity_id: select, option }
        ));
        btn.dataset.value = option;
        container.appendChild(btn);
      }
    }
    for (const btn of container.children) btn.classList.toggle("active", btn.dataset.value === state.state);
  }

  _updateMap() {
    const cam = this._camera();
    const mapContainer = this.shadowRoot.querySelector(".map-container");
    const state = this._state(cam);
    if (!state) {
      mapContainer.innerHTML = `<span class="no-map">No map available</span>`;
      this._mapPicture = null;
      return;
    }
    let img = mapContainer.querySelector("img");
    if (!img) {
      mapContainer.innerHTML = "";
      img = document.createElement("img");
      img.alt = "Vacuum map";
      img.onerror = () => { this._mapPicture = null; };
      mapContainer.appendChild(img);
    }
    // The integration rotates the camera's access token whenever the map or
    // the robot's position changes, so a new entity_picture means a new image.
    const picture = state.attributes.entity_picture;
    const stale = Date.now() - this._lastMapRefresh > 60000;
    if (picture && (picture !== this._mapPicture || stale)) {
      this._mapPicture = picture;
      this._lastMapRefresh = Date.now();
      img.src = `${picture}${picture.includes("?") ? "&" : "?"}t=${Date.now()}`;
    }
  }

  _updateStartButton(stateOverride) {
    const btn = this.shadowRoot.getElementById("start-btn");
    if (!btn) return;
    const s = stateOverride || this._hass?.states[this._config.entity]?.state;
    if (s === "cleaning" || s === "returning") {
      btn.innerHTML = `${ICON("mdi:pause")} Pause`;
    } else if (s === "paused") {
      btn.innerHTML = `${ICON("mdi:play")} Resume`;
    } else {
      const count = this._selectedRooms.size;
      const label = count > 0 ? `Start (${count} room${count > 1 ? "s" : ""})` : "Start all";
      btn.innerHTML = `${ICON("mdi:play")} ${label}`;
    }
  }

  _updateDock() {
    const container = this.shadowRoot.getElementById("dock");
    const found = DOCK_ACTIONS
      .map((a) => ({ ...a, entity: this._sibling("button", a.key) }))
      .filter((a) => a.entity && this._state(a.entity));
    const signature = found.map((a) => a.entity).join("|");
    if (container.dataset.sig === signature) return;
    container.dataset.sig = signature;
    container.innerHTML = "";
    for (const a of found) {
      const btn = document.createElement("button");
      btn.className = "action-btn secondary";
      btn.innerHTML = `${ICON(a.icon, 16)} ${a.label}`;
      btn.addEventListener("click", () => this._hass.callService("button", "press", { entity_id: a.entity }));
      container.appendChild(btn);
    }
  }

  /* --- Actions ---------------------------------------------------- */

  _selectAll() {
    const attrs = this._hass?.states[this._config.entity]?.attributes || {};
    const noGo = new Set((attrs.no_go_rooms || []).map(Number));
    this._selectedRooms = new Set(Object.keys(attrs.rooms || {}).map(Number).filter((r) => !noGo.has(r)));
    this._update();
  }

  _selectNone() {
    this._selectedRooms.clear();
    this._update();
  }

  _syncModeEntity() {
    const modeEntity = this._modeSelect();
    if (!modeEntity) return;
    this._hass.callService("select", "select_option", { entity_id: modeEntity, option: this._selectedMode });
  }

  _syncModeFromEntity() {
    const state = this._state(this._modeSelect());
    if (state && MODES.some((m) => m.value === state.state)) this._selectedMode = state.state;
  }

  _primaryAction() {
    const entity = this._hass.states[this._config.entity];
    if (!entity) return;
    if (entity.state === "cleaning" || entity.state === "returning") {
      this._vacuum("pause");
      return;
    }
    if (entity.state === "paused") {
      this._vacuum("start"); // the integration resumes a paused task
      return;
    }
    const roomIds = [...this._selectedRooms];
    if (roomIds.length > 0) {
      this._hass.callService("vacuum", "send_command", {
        entity_id: this._config.entity,
        command: "clean_rooms",
        params: { rooms: roomIds, mode: this._selectedMode },
      });
    } else {
      this._syncModeEntity();
      this._vacuum("start");
    }
  }

  _vacuum(service) {
    this._hass.callService("vacuum", service, { entity_id: this._config.entity });
  }
}

if (!customElements.get("narwal-vacuum-card")) {
  customElements.define("narwal-vacuum-card", NarwalVacuumCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.find((c) => c.type === "narwal-vacuum-card")) window.customCards.push({
  type: "narwal-vacuum-card",
  name: "Narwal Vacuum",
  description: "Control your Narwal vacuum: rooms, cleaning mode, suction, mop wetness, map and dock actions",
  preview: true,
  documentationURL: "https://github.com/nadavbau/narwal-integration",
});

console.info(`%c NARWAL-VACUUM-CARD %c v${CARD_VERSION} `, "color:#fff;background:#03a9f4;padding:2px 6px;border-radius:4px 0 0 4px;", "color:#03a9f4;background:#e3f2fd;padding:2px 6px;border-radius:0 4px 4px 0;");
