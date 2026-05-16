Module.register("MMM-Roborock", {
  defaults: {
    pythonPath: "venv/bin/python",
    sessionDir: "data",
    deviceName: null,
    deviceDuid: null,
    preferCategory: "mower",
    updateInterval: 5 * 60 * 1000,
    retryDelay: 60 * 1000,
    animationSpeed: 500,
    mode: "full",
    showHeader: true,
    showConnection: false,
    showUpdated: false,
    showFirmware: false,
    showModel: false,
    showCategory: false,
    showRawStatus: false,
    debug: false,
    labels: {}
  },

  start() {
    this.dataReady = false;
    this.statusData = null;
    this.errorText = null;
    this.sendSocketNotification("MMM_ROBOROCK_CONFIG", {
      identifier: this.identifier,
      config: this.config
    });
    this.updateDom(this.config.animationSpeed);
  },

  getStyles() {
    return ["MMM-Roborock.css"];
  },

  getTranslations() {
    return {
      en: "translations/en.json",
      da: "translations/da.json",
      de: "translations/de.json",
      nl: "translations/nl.json",
      fr: "translations/fr.json"
    };
  },

  getDom() {
    const wrapper = document.createElement("div");
    wrapper.className = "mmm-roborock";

    if (this.errorText) {
      wrapper.innerHTML = `
        ${this.config.showHeader ? '<div class="small bright">Roborock</div>' : ""}
        <div class="small light">${this.escapeHtml(this.errorText)}</div>
      `;
      return wrapper;
    }

    if (!this.dataReady || !this.statusData) {
      wrapper.innerHTML = `
        ${this.config.showHeader ? '<div class="small bright">Roborock</div>' : ""}
        <div class="small light">${this.translateText("LOADING")}</div>
      `;
      return wrapper;
    }

    const devices = Array.isArray(this.statusData) ? this.statusData : [this.statusData];
    const visibleDevices = devices.filter((deviceData) => this.shouldShowDevice(deviceData));

    if (visibleDevices.length === 0) {
      return wrapper;
    }

    if (visibleDevices.length > 1) {
      wrapper.classList.add("mmm-roborock--multi");
      if (this.config.showHeader) {
        const header = document.createElement("div");
        header.className = "small bright mmm-roborock__header";
        header.textContent = "Roborock";
        wrapper.appendChild(header);
      }

      visibleDevices.forEach((deviceData) => {
        const deviceWrapper = document.createElement("div");
        deviceWrapper.className = "mmm-roborock__device";
        this.renderDevice(deviceWrapper, deviceData, false);
        wrapper.appendChild(deviceWrapper);
      });

      return wrapper;
    }

    return this.renderDevice(wrapper, visibleDevices[0], this.config.showHeader);
  },

  renderDevice(wrapper, data, showHeader) {
    const statusInfo = this.getDeviceStatusInfo(data);
    const isMower = statusInfo.isMower;
    const isVacuum = statusInfo.isVacuum;

    const state = data.status?.stateName || this.fallbackStateName(data.status?.state) || this.translateText("UNKNOWN");
    const battery = statusInfo.battery;
    const progress = statusInfo.progress;
    const mowHeight = data.status?.mowHeight !== undefined && data.status?.mowHeight !== null
      ? this.formatMowHeight(data.status.mowHeight)
      : null;
    const chargeStateCode = data.status?.chargeState;
    const chargeState = chargeStateCode !== undefined && chargeStateCode !== null
      ? this.getChargeStateText(chargeStateCode)
      : null;
    const isCharging = this.isChargingState(chargeStateCode);

    const updated = data.fetchedAt ? this.formatTime(data.fetchedAt) : null;
    const connection = this.getConnectionText(data);
    const model = data.product?.model || null;
    const category = data.product?.category || null;
    const firmware = data.device?.firmware || null;
    const error = data.status?.errorCodeName || (!data.status ? data.statusError : null);

    if (this.isCompactMode()) {
      return this.getCompactDom(wrapper, {
        data,
        battery,
        isCharging,
        isMower,
        isVacuum,
        isMowing: statusInfo.isMowing,
        isCleaning: statusInfo.isCleaning,
        isReturning: statusInfo.isReturning,
        progress,
        cleanArea: data.status?.squareMeterCleanArea,
        state,
        rawState: data.status?.state,
        isStuck: statusInfo.isStuck,
        alertText: this.getDeviceAlertText(data)
      }, showHeader);
    }

    if (showHeader) {
      const header = document.createElement("div");
      header.className = "small bright";
      header.textContent = "Roborock";
      wrapper.appendChild(header);
    }

    if (battery !== null) {
      const batteryBlock = document.createElement("div");
      batteryBlock.className = "mmm-roborock__battery-block";
      batteryBlock.innerHTML = `
        <div class="small light mmm-roborock__battery-heading">
          <span>${this.escapeHtml(this.label("battery", "BATTERY"))}</span>
          ${isCharging ? `<span class="mmm-roborock__charge-icon fa fa-bolt" title="${this.translateText("CHARGING")}"></span>` : ""}
        </div>
        <div class="mmm-roborock__battery-row">
          <div class="mmm-roborock__battery-bar">
            <div class="mmm-roborock__battery-fill" style="width:${Math.max(0, Math.min(100, battery))}%"></div>
          </div>
          <div class="small bright mmm-roborock__battery-text">${battery}%</div>
        </div>
      `;
      wrapper.appendChild(batteryBlock);
    }

    const details = document.createElement("div");
    details.className = "mmm-roborock__details";

    if (!isMower) this.addDetailRow(details, this.label("state", "STATE"), state);
    if (isMower && progress) this.addDetailRow(details, this.label("progress", "PROGRESS"), progress, "fa fa-leaf");
    if (isMower && mowHeight) this.addDetailRow(details, this.label("mowHeight", "MOWING_HEIGHT"), mowHeight);
    if (isMower && chargeState && !isCharging) this.addDetailRow(details, this.label("chargeState", "CHARGING"), chargeState);
    const alertText = this.getDeviceAlertText(data);
    if (alertText) this.addAlertLine(wrapper, alertText);
    if (this.config.showConnection && connection) this.addDetailRow(details, this.label("connection", "CONNECTION"), connection);
    if (this.config.showModel && model) this.addDetailRow(details, this.label("model", "MODEL"), model);
    if (this.config.showCategory && category) this.addDetailRow(details, this.label("category", "CATEGORY"), category);
    if (this.config.showFirmware && firmware) this.addDetailRow(details, this.label("firmware", "FIRMWARE"), firmware);
    if (error) this.addDetailRow(details, this.label("error", "ERROR"), error);
    if (this.config.showUpdated && updated) this.addDetailRow(details, this.label("updated", "UPDATED"), updated);

    wrapper.appendChild(details);

    if (this.config.showRawStatus && data.status) {
      const raw = document.createElement("pre");
      raw.className = "xsmall dimmed mmm-roborock__raw";
      raw.textContent = JSON.stringify(data.status, null, 2);
      wrapper.appendChild(raw);
    }

    return wrapper;
  },

  getCompactDom(wrapper, status, showHeader = false) {
    wrapper.classList.add("mmm-roborock--compact");

    const battery = status.battery !== null ? `${status.battery}%` : "--%";
    const batteryIcon = status.isCharging ? "fa fa-bolt" : "fa fa-battery-half";
    const batteryTitle = status.isCharging ? this.translateText("CHARGING") : this.translateText("BATTERY");

    const activity = this.getCompactActivity(status);
    const label = this.getDeviceLabel(status);

    wrapper.innerHTML = `
      ${showHeader ? '<div class="small bright mmm-roborock__header">Roborock</div>' : ""}
      ${label ? `<div class="xsmall dimmed mmm-roborock__device-label">${this.escapeHtml(label)}</div>` : ""}
      <div class="mmm-roborock__compact-row">
        <div class="mmm-roborock__compact-item mmm-roborock__compact-item--battery" title="${batteryTitle}">
          <span class="mmm-roborock__compact-icon ${batteryIcon}"></span>
          <span class="mmm-roborock__compact-value">${this.escapeHtml(battery)}</span>
        </div>
        <div class="mmm-roborock__compact-item mmm-roborock__compact-item--activity" title="${this.escapeHtml(activity.title)}">
          <span class="mmm-roborock__compact-icon ${activity.icon}"></span>
          <span class="mmm-roborock__compact-value">${this.escapeHtml(activity.text)}</span>
        </div>
      </div>
      ${status.alertText ? `<div class="small mmm-roborock__alert">${this.escapeHtml(status.alertText)}</div>` : ""}
    `;

    return wrapper;
  },

  getCompactActivity(status) {
    if (status.isMower) {
      return {
        icon: status.isMowing ? "fa fa-leaf" : "fa fa-home",
        text: status.progress || "--%",
        title: status.isMowing ? this.translateText("MOWING") : this.translateText("DOCKED")
      };
    }

    if (status.isVacuum) {
      if (status.isCleaning) {
        return {
          icon: "fa fa-refresh",
          text: Number.isFinite(Number(status.cleanArea)) ? `${Number(status.cleanArea).toFixed(1)} m²` : this.translateText("CLEANING"),
          title: this.translateText("VACUUM")
        };
      }

      if (status.isReturning) {
        return { icon: "fa fa-reply", text: this.translateText("RETURN_SHORT"), title: this.translateText("RETURNING") };
      }

      return { icon: "fa fa-home", text: status.state || this.fallbackStateName(status.rawState) || "--", title: this.translateText("DOCKED") };
    }

    return { icon: "fa fa-info-circle", text: status.state || "--", title: this.translateText("STATUS") };
  },

  getDeviceLabel(status) {
    return status.data?.device?.name
      || status.data?.moduleDevice?.label
      || status.data?.moduleDevice?.id
      || this.translateText("DEVICE");
  },

  isCompactMode() {
    return ["compact", "onlyActive", "onlyErrors"].includes(this.config.mode);
  },

  shouldShowDevice(data) {
    const hasAlert = this.hasDeviceAlert(data);
    if (hasAlert) {
      return true;
    }

    if (this.config.mode === "onlyErrors") {
      return false;
    }

    if (this.config.mode === "onlyActive") {
      return this.isActiveTaskDevice(data);
    }

    return true;
  },

  isActiveTaskDevice(data) {
    const statusInfo = this.getDeviceStatusInfo(data);
    if (statusInfo.isMower) {
      return statusInfo.isMowing;
    }

    if (statusInfo.isVacuum) {
      return statusInfo.isCleaning || statusInfo.isReturning;
    }

    return false;
  },

  getDeviceStatusInfo(data) {
    const category = String(data.product?.category || "");
    const isMower = category.includes("mower");
    const isVacuum = category.includes("vacuum");
    const battery = this.toFiniteNumber(data.status?.battery);
    const mowProgress = this.toFiniteNumber(data.status?.mowProgress);
    const progress = Number.isFinite(mowProgress) ? `${mowProgress}%` : null;
    return {
      isMower,
      isVacuum,
      battery,
      progress,
      isMowing: this.hasActiveMowTask(data.status),
      isCleaning: this.isVacuumCleaning(data.status),
      isReturning: this.isVacuumReturning(data.status),
      isStuck: this.isStuck(data.status)
    };
  },

  hasDeviceAlert(data) {
    return Boolean(this.getDeviceAlertText(data));
  },

  getDeviceAlertText(data) {
    if (this.isMowerLidarDirty(data.status)) {
      return this.translateText("MOWER_LIDAR_DIRTY");
    }

    if (this.isStuck(data.status)) {
      return this.translateText("MOWER_STUCK");
    }

    if (this.isVacuumPausedWhileCleaning(data.status)) {
      return this.translateText("VACUUM_PAUSED_WHILE_CLEANING");
    }

    const maintenanceAlert = this.getMaintenanceAlertText(data);
    if (maintenanceAlert) {
      return maintenanceAlert;
    }

    const errorCode = Number(data.status?.errorCode);
    const errorName = String(data.status?.errorCodeName || "").toLowerCase();
    if (Number.isFinite(errorCode) && errorCode !== 0) {
      return data.status?.errorCodeName || this.translateText("CODE", { code: errorCode });
    }

    if (errorName && errorName !== "none") {
      return data.status.errorCodeName;
    }

    const statusError = data.statusError;
    if (statusError && !data.status) {
      return statusError;
    }

    return null;
  },

  getMaintenanceAlertText(data) {
    const mowerMaintenance = data.status?.maintenance?.needsMaintenance || [];
    if (mowerMaintenance.length > 0) {
      const items = mowerMaintenance.map((item) => this.getMowerMaintenanceLabel(item.typeName || item.type)).join(", ");
      return this.translateText("MAINTENANCE_CLEAN", { items });
    }

    const rawStatus = data.device?.raw?.device_status || {};
    const schema = data.product?.raw?.schema || [];
    const expired = [];

    schema.forEach((item) => {
      const code = item?.code;
      if (!["main_brush_life", "side_brush_life", "filter_life"].includes(code)) {
        return;
      }

      const value = rawStatus[String(item.id)] ?? rawStatus[item.id];
      if (Number(value) === 0) {
        expired.push(this.getMaintenanceLabel(code));
      }
    });

    if (expired.length === 0) {
      return null;
    }

    return this.translateText("MAINTENANCE_REPLACE", { items: expired.join(", ") });
  },

  getMaintenanceLabel(code) {
    const mapping = {
      main_brush_life: "MAIN_BRUSH",
      side_brush_life: "SIDE_BRUSH",
      filter_life: "FILTER"
    };

    return this.translateText(mapping[code] || code);
  },

  getMowerMaintenanceLabel(typeName) {
    const mapping = {
      CAMERA_CLEANING: "CAMERA_CLEANING",
      CHASSIS_CLEANING: "CHASSIS_CLEANING",
      CUTTING: "CUTTING_DISC",
      EDGING: "EDGING_CUTTER",
      BATTERY: "BATTERY"
    };

    return this.translateText(mapping[typeName] || String(typeName || "UNKNOWN"));
  },

  addAlertLine(container, message) {
    const alert = document.createElement("div");
    alert.className = "small mmm-roborock__alert";
    alert.textContent = message;
    container.appendChild(alert);
  },

  addDetailRow(container, label, value, iconClass = null) {
    const row = document.createElement("div");
    row.className = "mmm-roborock__row small";
    row.innerHTML = `
      <span class="light mmm-roborock__label">
        ${iconClass ? `<span class="mmm-roborock__row-icon ${this.escapeHtml(iconClass)}"></span>` : ""}
        <span>${this.escapeHtml(label)}</span>
      </span>
      <span class="bright mmm-roborock__value">${this.escapeHtml(String(value))}</span>
    `;
    container.appendChild(row);
  },

  socketNotificationReceived(notification, payload) {
    if (!payload || payload.identifier !== this.identifier) {
      return;
    }

    if (notification === "MMM_ROBOROCK_STATUS") {
      this.dataReady = true;
      this.errorText = null;
      this.statusData = payload.data;
      this.updateDom(this.config.animationSpeed);
    }

    if (notification === "MMM_ROBOROCK_ERROR") {
      this.dataReady = true;
      this.errorText = payload.error || "Ukendt fejl";
      this.updateDom(this.config.animationSpeed);
    }
  },

  getConnectionText(data) {
    if (data.localConnected) return this.translateText("LOCAL");
    if (data.connected) return "Cloud";
    if (typeof data.device?.online === "boolean") return data.device.online ? this.translateText("ONLINE") : this.translateText("OFFLINE");
    return null;
  },

  isChargingState(chargeState) {
    return Number(chargeState) > 0;
  },

  hasActiveMowTask(status) {
    const mowState = Number(status?.mowState);
    const mowType = Number(status?.mowType);
    const mowProgress = Number(status?.mowProgress);

    if (Number.isFinite(mowState) && mowState > 0) {
      return true;
    }

    if (Number.isFinite(mowType) && mowType > 0 && Number.isFinite(mowProgress) && mowProgress > 0 && mowProgress < 100) {
      return true;
    }

    return false;
  },

  isStuck(status) {
    return Number(status?.offDockNoTaskStatus) === 108 || Number(status?.mowState) === 59;
  },

  isMowerLidarDirty(status) {
    return Number(status?.offlineStatus) === 3;
  },

  isVacuumCleaning(status) {
    const state = Number(status?.state);
    const cleaningStates = [5, 11, 17, 18];
    return Number(status?.inCleaning) > 0 || cleaningStates.includes(state);
  },

  isVacuumPausedWhileCleaning(status) {
    return this.isVacuumCleaning(status) && String(status?.stateName || "").toLowerCase() === "paused";
  },

  isVacuumReturning(status) {
    const state = Number(status?.state);
    const returningStates = [6, 15];
    return Number(status?.inReturning) > 0 || returningStates.includes(state);
  },

  getVacuumStateText(state) {
    const normalized = String(state || "").toLowerCase();
    const mapping = {
      charging: this.translateText("CHARGING"),
      charger_error: this.translateText("DOCK_ERROR"),
      cleaning: this.translateText("CLEANING"),
      returning_home: this.translateText("RETURN_SHORT"),
      idle: this.translateText("READY"),
      sleeping: this.translateText("SLEEPING"),
      paused: this.translateText("PAUSED")
    };

    if (normalized === "charging") return this.translateText("CHARGING");
    return mapping[normalized] || state || "--";
  },

  toFiniteNumber(value) {
    if (value === null || value === undefined || value === "") {
      return null;
    }

    const numericValue = Number(value);
    return Number.isFinite(numericValue) ? numericValue : null;
  },

  getChargeStateText(chargeState) {
    const mapping = {
      0: this.translateText("INACTIVE"),
      1: this.translateText("CHARGING"),
      2: this.translateText("FULLY_CHARGED")
    };
    return mapping[chargeState] || this.translateText("CODE", { code: chargeState });
  },

  formatMowHeight(value) {
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) {
      return String(value);
    }

    return `${numericValue} mm`;
  },

  fallbackStateName(state) {
    if (state === null || state === undefined) return null;
    const mapping = {
      1: this.translateText("INITIALIZING"),
      2: this.translateText("SLEEPING"),
      3: this.translateText("IDLE"),
      5: this.translateText("CLEANING"),
      6: this.translateText("RETURNING"),
      8: this.translateText("DOCKED"),
      9: this.translateText("ERROR"),
      10: this.translateText("PAUSED"),
      12: this.translateText("ERROR"),
      17: this.translateText("ZONES"),
      18: this.translateText("ROOMS"),
      22: this.translateText("EMPTYING"),
      23: this.translateText("WASHING_MOP"),
      26: this.translateText("DOCKED"),
      28: this.translateText("MAPPING")
    };
    return mapping[state] || this.translateText("CODE", { code: state });
  },

  label(configKey, translationKey) {
    return this.config.labels?.[configKey] || this.translateText(translationKey);
  },

  translateText(key, variables = {}) {
    const translated = this.translate(key, variables);
    return translated === key ? key : translated;
  },

  formatTime(isoString) {
    try {
      return new Date(isoString).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit"
      });
    } catch (error) {
      return isoString;
    }
  },

  escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
});
