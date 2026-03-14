const CARD_VERSION = "1.2.0";

const MODES = [
  { label: "Vacuum & Mop", icon: "mdi:robot-vacuum", value: "Vacuum & Mop" },
  { label: "Vac → Mop", icon: "mdi:robot-vacuum", value: "Vacuum then Mop" },
  { label: "Vacuum", icon: "mdi:vacuum", value: "Vacuum Only" },
  { label: "Mop", icon: "mdi:spray-bottle", value: "Mop Only" },
];

/* ================================================================
 *  Visual Config Editor
 * ================================================================ */

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

    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
    this.shadowRoot.innerHTML = `
      <style>
        .editor { padding: 8px 0; }
        .row { margin-bottom: 16px; }
        .row label { display: block; font-size: 0.85em; font-weight: 500;
          margin-bottom: 4px; color: var(--primary-text-color); }
        .row .hint { font-size: 0.75em; color: var(--secondary-text-color);
          margin-top: 2px; }
        ha-entity-picker { display: block; width: 100%; }
      </style>
      <div class="editor">
        <div class="row">
          <label>Vacuum Entity *</label>
          <ha-entity-picker
            id="entity"
            .hass=${this._hass}
            .value=${this._config.entity || ""}
            .includeDomains=${["vacuum"]}
            allow-custom-entity
          ></ha-entity-picker>
        </div>
        <div class="row">
          <label>Camera Entity (Map)</label>
          <ha-entity-picker
            id="camera_entity"
            .hass=${this._hass}
            .value=${this._config.camera_entity || ""}
            .includeDomains=${["camera"]}
            allow-custom-entity
          ></ha-entity-picker>
          <div class="hint">Leave empty to auto-detect</div>
        </div>
        <div class="row">
          <label>Battery Sensor</label>
          <ha-entity-picker
            id="battery_entity"
            .hass=${this._hass}
            .value=${this._config.battery_entity || ""}
            .includeDomains=${["sensor"]}
            allow-custom-entity
          ></ha-entity-picker>
          <div class="hint">Leave empty to auto-detect</div>
        </div>
        <div class="row">
          <label>Clean Mode Select</label>
          <ha-entity-picker
            id="mode_entity"
            .hass=${this._hass}
            .value=${this._config.mode_entity || ""}
            .includeDomains=${["select"]}
            allow-custom-entity
          ></ha-entity-picker>
          <div class="hint">Leave empty to auto-detect</div>
        </div>
      </div>
    `;

    for (const field of ["entity", "camera_entity", "battery_entity", "mode_entity"]) {
      const el = this.shadowRoot.getElementById(field);
      if (el) {
        el.addEventListener("value-changed", (ev) => {
          const val = ev.detail?.value ?? "";
          if (this._config[field] === val) return;
          this._config = { ...this._config, [field]: val };
          this._fireChanged();
        });
      }
    }
  }

  _fireChanged() {
    const clean = { ...this._config };
    for (const key of ["camera_entity", "battery_entity", "mode_entity"]) {
      if (!clean[key]) delete clean[key];
    }
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: clean },
      bubbles: true,
      composed: true,
    }));
  }
}

customElements.define("narwal-vacuum-card-editor", NarwalVacuumCardEditor);

/* ================================================================
 *  Main Card
 * ================================================================ */

class NarwalVacuumCard extends HTMLElement {
  constructor() {
    super();
    this._selectedRooms = new Set();
    this._selectedMode = "Vacuum & Mop";
    this._initialized = false;
  }

  static getConfigElement() {
    return document.createElement("narwal-vacuum-card-editor");
  }

  static getStubConfig() {
    return {
      entity: "vacuum.narwal_vacuum",
    };
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
    return 6;
  }

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
          .header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
          .header .name { font-size: 1.1em; font-weight: 500; flex: 1; }
          .status-row { display: flex; align-items: center; gap: 16px; margin-bottom: 16px;
            font-size: 0.9em; color: var(--secondary-text-color); }
          .status-row .battery { display: flex; align-items: center; gap: 4px; }
          .status-row .state { text-transform: capitalize; }
          .map-container { width: 100%; border-radius: 12px; overflow: hidden;
            background: var(--card-background-color, #1c1c1c); margin-bottom: 16px;
            aspect-ratio: 4/3; display: flex; align-items: center; justify-content: center; }
          .map-container img { width: 100%; height: 100%; object-fit: contain; }
          .map-container .no-map { color: var(--secondary-text-color); font-size: 0.85em; }
          .section-label { font-size: 0.75em; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.5px; color: var(--secondary-text-color); margin-bottom: 8px; }
          .rooms { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
          .room-btn { border: 1.5px solid var(--divider-color, #444); border-radius: 20px;
            padding: 6px 14px; font-size: 0.85em; cursor: pointer; transition: all 0.15s;
            background: transparent; color: var(--primary-text-color);
            font-family: inherit; user-select: none; }
          .room-btn:hover { border-color: var(--primary); }
          .room-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }
          .modes { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
          .mode-btn { flex: 1; min-width: 70px; border: 1.5px solid var(--divider-color, #444);
            border-radius: 12px; padding: 8px 6px; font-size: 0.8em; cursor: pointer;
            transition: all 0.15s; background: transparent; color: var(--primary-text-color);
            font-family: inherit; text-align: center; user-select: none; }
          .mode-btn:hover { border-color: var(--primary); }
          .mode-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }
          .actions { display: flex; gap: 8px; }
          .action-btn { flex: 1; border: none; border-radius: 12px; padding: 12px 8px;
            font-size: 0.9em; font-weight: 500; cursor: pointer; transition: all 0.15s;
            font-family: inherit; display: flex; align-items: center; justify-content: center; gap: 6px; }
          .action-btn.start { background: var(--primary); color: #fff; }
          .action-btn.start:hover { filter: brightness(1.1); }
          .action-btn.start:disabled { opacity: 0.5; cursor: not-allowed; }
          .action-btn.secondary { background: var(--divider-color, #333);
            color: var(--primary-text-color); }
          .action-btn.secondary:hover { filter: brightness(1.2); }
          .select-actions { display: flex; gap: 8px; margin-bottom: 8px; }
          .select-link { font-size: 0.75em; cursor: pointer; color: var(--primary);
            text-decoration: none; user-select: none; }
          .select-link:hover { text-decoration: underline; }
        </style>
        <div class="card">
          <div class="header">
            <ha-icon icon="mdi:robot-vacuum" style="--mdc-icon-size:28px"></ha-icon>
            <span class="name">Narwal Vacuum</span>
          </div>
          <div class="status-row">
            <span class="battery"><ha-icon icon="mdi:battery" style="--mdc-icon-size:18px"></ha-icon>
              <span class="battery-val">--</span></span>
            <span class="state">--</span>
          </div>
          <div class="map-container">
            <span class="no-map">No map available</span>
          </div>
          <div class="section-label">Rooms</div>
          <div class="select-actions">
            <a class="select-link" id="select-all">Select all</a>
            <a class="select-link" id="select-none">Clear</a>
          </div>
          <div class="rooms" id="rooms"></div>
          <div class="section-label">Cleaning Mode</div>
          <div class="modes" id="modes"></div>
          <div class="actions" id="actions"></div>
        </div>
      </ha-card>
    `;

    this.shadowRoot.getElementById("select-all").addEventListener("click", () => this._selectAll());
    this.shadowRoot.getElementById("select-none").addEventListener("click", () => this._selectNone());
    this._buildModes();
    this._buildActions();
  }

  _buildModes() {
    const container = this.shadowRoot.getElementById("modes");
    container.innerHTML = "";
    for (const m of MODES) {
      const btn = document.createElement("button");
      btn.className = "mode-btn" + (m.value === this._selectedMode ? " active" : "");
      btn.textContent = m.label;
      btn.dataset.mode = m.value;
      btn.addEventListener("click", () => {
        this._selectedMode = m.value;
        this._syncModeEntity();
        this._updateModes();
      });
      container.appendChild(btn);
    }
  }

  _buildActions() {
    const container = this.shadowRoot.getElementById("actions");
    container.innerHTML = "";

    const startBtn = document.createElement("button");
    startBtn.className = "action-btn start";
    startBtn.id = "start-btn";
    startBtn.innerHTML = `<ha-icon icon="mdi:play" style="--mdc-icon-size:20px"></ha-icon> Start`;
    startBtn.addEventListener("click", () => this._startClean());
    container.appendChild(startBtn);

    const stopBtn = document.createElement("button");
    stopBtn.className = "action-btn secondary";
    stopBtn.innerHTML = `<ha-icon icon="mdi:stop" style="--mdc-icon-size:20px"></ha-icon> Stop`;
    stopBtn.addEventListener("click", () => this._callService("vacuum", "stop"));
    container.appendChild(stopBtn);

    const homeBtn = document.createElement("button");
    homeBtn.className = "action-btn secondary";
    homeBtn.innerHTML = `<ha-icon icon="mdi:home" style="--mdc-icon-size:20px"></ha-icon> Dock`;
    homeBtn.addEventListener("click", () => this._callService("vacuum", "return_to_base"));
    container.appendChild(homeBtn);

    const locateBtn = document.createElement("button");
    locateBtn.className = "action-btn secondary";
    locateBtn.innerHTML = `<ha-icon icon="mdi:map-marker" style="--mdc-icon-size:20px"></ha-icon>`;
    locateBtn.addEventListener("click", () => this._callService("vacuum", "locate"));
    container.appendChild(locateBtn);
  }

  /* --- Entity resolution (explicit config > auto-detect) --- */

  _resolveCameraEntity() {
    if (this._config.camera_entity) return this._config.camera_entity;
    return Object.keys(this._hass.states).find(
      e => e.startsWith("camera.") && e.includes("narwal")
    ) || null;
  }

  _resolveBatteryEntity() {
    if (this._config.battery_entity) return this._config.battery_entity;
    return Object.keys(this._hass.states).find(
      e => e.startsWith("sensor.") && e.includes("narwal") && e.includes("battery")
    ) || null;
  }

  _resolveModeEntity() {
    if (this._config.mode_entity) return this._config.mode_entity;
    return Object.keys(this._hass.states).find(
      e => e.startsWith("select.") && e.includes("narwal") && e.includes("clean_mode")
    ) || null;
  }

  /* --- Update cycle --- */

  _update() {
    if (!this._hass || !this._config) return;
    const entity = this._hass.states[this._config.entity];
    if (!entity) return;

    const battery = this._getBatteryLevel();
    const state = entity.state;
    const rooms = entity.attributes.rooms || {};

    const batteryEl = this.shadowRoot.querySelector(".battery-val");
    if (batteryEl) {
      batteryEl.textContent = battery != null ? `${Math.round(battery)}%` : "--";
    }
    const batteryIcon = this.shadowRoot.querySelector(".battery ha-icon");
    if (batteryIcon && battery != null) {
      const level = Math.round(battery / 10) * 10;
      batteryIcon.setAttribute("icon", level >= 100 ? "mdi:battery" : `mdi:battery-${level}`);
    }
    const stateEl = this.shadowRoot.querySelector(".state");
    if (stateEl) stateEl.textContent = state || "--";

    this._updateRooms(rooms);
    this._updateMap();
    this._updateStartButton(state);
    this._syncModeFromEntity();
  }

  _getBatteryLevel() {
    const batteryEntity = this._resolveBatteryEntity();
    if (batteryEntity) {
      const s = this._hass.states[batteryEntity];
      if (s && !isNaN(parseFloat(s.state))) return parseFloat(s.state);
    }
    const entity = this._hass.states[this._config.entity];
    if (entity?.attributes.battery_level != null) return entity.attributes.battery_level;
    return null;
  }

  _updateRooms(rooms) {
    const container = this.shadowRoot.getElementById("rooms");
    const roomEntries = Object.entries(rooms);

    if (container.children.length !== roomEntries.length) {
      container.innerHTML = "";
      for (const [id, name] of roomEntries) {
        const btn = document.createElement("button");
        btn.className = "room-btn" + (this._selectedRooms.has(Number(id)) ? " active" : "");
        btn.textContent = name;
        btn.dataset.roomId = id;
        btn.addEventListener("click", () => {
          const rid = Number(id);
          if (this._selectedRooms.has(rid)) {
            this._selectedRooms.delete(rid);
            btn.classList.remove("active");
          } else {
            this._selectedRooms.add(rid);
            btn.classList.add("active");
          }
          this._updateStartButton();
        });
        container.appendChild(btn);
      }
    } else {
      for (const btn of container.children) {
        const rid = Number(btn.dataset.roomId);
        btn.classList.toggle("active", this._selectedRooms.has(rid));
      }
    }
  }

  _updateModes() {
    const container = this.shadowRoot.getElementById("modes");
    for (const btn of container.children) {
      btn.classList.toggle("active", btn.dataset.mode === this._selectedMode);
    }
  }

  _updateMap() {
    const cameraEntity = this._resolveCameraEntity();
    const mapContainer = this.shadowRoot.querySelector(".map-container");
    if (!cameraEntity || !this._hass.states[cameraEntity]) {
      mapContainer.innerHTML = `<span class="no-map">No map available</span>`;
      return;
    }
    const cam = this._hass.states[cameraEntity];
    const accessToken = cam.attributes.access_token;
    const imgUrl = `/api/camera_proxy/${cameraEntity}?token=${accessToken}&t=${Date.now()}`;

    let img = mapContainer.querySelector("img");
    if (!img) {
      mapContainer.innerHTML = "";
      img = document.createElement("img");
      img.alt = "Vacuum Map";
      img.onerror = () => {
        mapContainer.innerHTML = `<span class="no-map">Map unavailable</span>`;
      };
      mapContainer.appendChild(img);
    }
    if (!img.src || this._shouldRefreshMap()) {
      img.src = imgUrl;
      this._lastMapRefresh = Date.now();
    }
  }

  _shouldRefreshMap() {
    return !this._lastMapRefresh || Date.now() - this._lastMapRefresh > 60000;
  }

  _updateStartButton(state) {
    const btn = this.shadowRoot.getElementById("start-btn");
    if (!btn) return;
    const entity = this._hass?.states[this._config.entity];
    const s = state || entity?.state;
    if (s === "cleaning" || s === "returning") {
      btn.innerHTML = `<ha-icon icon="mdi:pause" style="--mdc-icon-size:20px"></ha-icon> Pause`;
    } else {
      const count = this._selectedRooms.size;
      const label = count > 0 ? `Start (${count} room${count > 1 ? "s" : ""})` : "Start All";
      btn.innerHTML = `<ha-icon icon="mdi:play" style="--mdc-icon-size:20px"></ha-icon> ${label}`;
    }
  }

  _selectAll() {
    const entity = this._hass?.states[this._config.entity];
    if (!entity) return;
    const rooms = entity.attributes.rooms || {};
    this._selectedRooms = new Set(Object.keys(rooms).map(Number));
    this._updateRooms(rooms);
    this._updateStartButton();
  }

  _selectNone() {
    this._selectedRooms.clear();
    const entity = this._hass?.states[this._config.entity];
    if (!entity) return;
    this._updateRooms(entity.attributes.rooms || {});
    this._updateStartButton();
  }

  _syncModeEntity() {
    const modeEntity = this._resolveModeEntity();
    if (!modeEntity) return;
    this._hass.callService("select", "select_option", {
      entity_id: modeEntity,
      option: this._selectedMode,
    });
  }

  _syncModeFromEntity() {
    const modeEntity = this._resolveModeEntity();
    if (!modeEntity) return;
    const state = this._hass.states[modeEntity];
    if (state && state.state !== this._selectedMode && MODES.some(m => m.value === state.state)) {
      this._selectedMode = state.state;
      this._updateModes();
    }
  }

  _startClean() {
    const entity = this._hass.states[this._config.entity];
    if (!entity) return;

    if (entity.state === "cleaning" || entity.state === "returning") {
      this._callService("vacuum", "pause");
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
      this._callService("vacuum", "start");
    }
  }

  _callService(domain, service) {
    this._hass.callService(domain, service, {
      entity_id: this._config.entity,
    });
  }
}

customElements.define("narwal-vacuum-card", NarwalVacuumCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "narwal-vacuum-card",
  name: "Narwal Vacuum",
  description: "Control your Narwal vacuum with room selection, cleaning modes, and map",
  preview: true,
  documentationURL: "https://github.com/nadavbau/narwal-integration",
});

console.info(`%c NARWAL-VACUUM-CARD %c v${CARD_VERSION} `, "color:#fff;background:#03a9f4;padding:2px 6px;border-radius:4px 0 0 4px;", "color:#03a9f4;background:#e3f2fd;padding:2px 6px;border-radius:0 4px 4px 0;");
