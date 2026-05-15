const NodeHelper = require("node_helper");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

module.exports = NodeHelper.create({
  start() {
    this.configs = new Map();
    this.timers = new Map();
    this.running = new Map();
    this.debugLogPath = path.join(this.path, "data", "mmm-roborock-debug.log");
    this.logDebug("helper started");
  },

  stop() {
    for (const timer of this.timers.values()) {
      clearTimeout(timer);
    }
    this.timers.clear();
  },

  socketNotificationReceived(notification, payload) {
    this.logDebug(`socket notification received: ${notification}`);

    if (notification !== "MMM_ROBOROCK_CONFIG" || !payload?.identifier || !payload?.config) {
      this.logDebug("ignored socket notification");
      return;
    }

    this.configs.set(payload.identifier, payload.config);
    this.logDebug(`stored config for ${payload.identifier}: ${this.safeJson(payload.config)}`);
    this.fetchStatus(payload.identifier);
  },

  scheduleNext(identifier, delay) {
    if (this.timers.has(identifier)) {
      clearTimeout(this.timers.get(identifier));
    }

    const timer = setTimeout(() => {
      this.fetchStatus(identifier);
    }, delay);

    this.timers.set(identifier, timer);
    this.logDebug(`scheduled next fetch for ${identifier} in ${delay}ms`);
  },

  resolveModulePath(...parts) {
    return path.join(this.path, ...parts);
  },

  resolveConfiguredPath(configuredPath) {
    if (!configuredPath) {
      return configuredPath;
    }

    if (path.isAbsolute(configuredPath)) {
      return configuredPath;
    }

    return this.resolveModulePath(configuredPath);
  },

  async fetchStatus(identifier) {
    const config = this.configs.get(identifier);
    if (!config) {
      this.logDebug(`fetch skipped for ${identifier}: no config`);
      return;
    }

    if (this.running.get(identifier)) {
      this.logDebug(`fetch skipped for ${identifier}: already running`);
      return;
    }

    this.running.set(identifier, true);
    const deviceConfigs = this.getDeviceConfigs(config);

    try {
      const devices = [];
      for (const deviceConfig of deviceConfigs) {
        devices.push(await this.fetchDeviceStatus(identifier, config, deviceConfig));
      }

      const data = Array.isArray(config.devices) ? devices : devices[0];
      this.sendSocketNotification("MMM_ROBOROCK_STATUS", { identifier, data });
      this.scheduleNext(identifier, config.updateInterval || 300000);
    } catch (error) {
      this.logDebug(`fetch failed for ${identifier}: ${error.stack || error.message}`);
      this.sendSocketNotification("MMM_ROBOROCK_ERROR", {
        identifier,
        error: error.message || "Ukendt Roborock-fejl"
      });
      this.scheduleNext(identifier, config.retryDelay || 60000);
    } finally {
      this.running.set(identifier, false);
    }
  },

  getDeviceConfigs(config) {
    if (Array.isArray(config.devices) && config.devices.length > 0) {
      return config.devices.map((deviceConfig) => ({ ...config, ...deviceConfig }));
    }

    return [config];
  },

  fetchDeviceStatus(identifier, config, deviceConfig) {
    return new Promise((resolve, reject) => {
      const pythonPath = this.resolveConfiguredPath(config.pythonPath || "python3");
      const scriptPath = this.resolveModulePath("scripts", "fetch_roborock_status.py");
      const sessionDir = this.resolveConfiguredPath(config.sessionDir || "data");

      const args = [scriptPath, "--session-dir", sessionDir];

      if (deviceConfig.deviceName) {
        args.push("--device-name", deviceConfig.deviceName);
      }

      if (deviceConfig.deviceDuid) {
        args.push("--device-duid", deviceConfig.deviceDuid);
      }

      if (deviceConfig.preferCategory) {
        args.push("--prefer-category", deviceConfig.preferCategory);
      }

      const deviceLabel = deviceConfig.id || deviceConfig.type || deviceConfig.deviceDuid || deviceConfig.preferCategory || "device";
      this.logDebug(
        `spawning python for ${identifier}/${deviceLabel}: cwd=${this.path} command=${pythonPath} args=${this.safeJson(args)}`
      );

      const child = spawn(pythonPath, args, {
        cwd: this.path,
        env: process.env
      });

      let stdout = "";
      let stderr = "";
      let timedOut = false;
      const fetchTimeout = config.fetchTimeout || 60000;
      const timeoutTimer = setTimeout(() => {
        timedOut = true;
        this.logDebug(`python timeout for ${identifier}/${deviceLabel} after ${fetchTimeout}ms`);
        child.kill("SIGTERM");
      }, fetchTimeout);

      child.stdout.on("data", (data) => {
        const chunk = data.toString();
        stdout += chunk;
        this.logDebug(`stdout chunk for ${identifier}/${deviceLabel}: ${chunk.length} bytes`);
      });

      child.stderr.on("data", (data) => {
        const chunk = data.toString();
        stderr += chunk;
        this.logDebug(`stderr chunk for ${identifier}/${deviceLabel}: ${chunk}`);
      });

      child.on("error", (error) => {
        clearTimeout(timeoutTimer);
        this.logDebug(`python spawn error for ${identifier}/${deviceLabel}: ${error.stack || error.message}`);
        reject(new Error(`Kunne ikke starte Python: ${error.message}`));
      });

      child.on("close", (code) => {
        clearTimeout(timeoutTimer);
        this.logDebug(`python closed for ${identifier}/${deviceLabel}: code=${code}`);

        if (timedOut) {
          reject(new Error(`Python-kaldet tog for lang tid (${Math.round(fetchTimeout / 1000)} sekunder)`));
          return;
        }

        if (code !== 0) {
          const message = stderr.trim() || stdout.trim() || `Python sluttede med kode ${code}`;
          this.logDebug(`python failed for ${identifier}/${deviceLabel}: ${message}`);
          reject(new Error(message));
          return;
        }

        try {
          const data = JSON.parse(stdout);
          data.moduleDevice = {
            id: deviceConfig.id || null,
            type: deviceConfig.type || null,
            label: deviceConfig.label || null
          };
          this.logDebug(
            `json parse success for ${identifier}/${deviceLabel}: status=${this.safeJson({
              battery: data.status?.battery,
              state: data.status?.state,
              stateName: data.status?.stateName,
              errorCode: data.status?.errorCode,
              statusError: data.statusError,
              rawByCode: data.status?.rawByCode
            })}`
          );
          resolve(data);
        } catch (error) {
          this.logDebug(
            `json parse failed for ${identifier}/${deviceLabel}: ${error.stack || error.message}; stdout=${stdout}; stderr=${stderr}`
          );
          reject(new Error(`Kunne ikke læse JSON fra Python: ${error.message}`));
        }
      });
    });
  },

  logDebug(message) {
    const debugEnabled = process.env.MMM_ROBOROCK_DEBUG === "1"
      || Array.from(this.configs?.values() || []).some((config) => config.debug);

    if (!debugEnabled) {
      return;
    }

    const line = `[${new Date().toISOString()}] ${message}\n`;

    try {
      fs.mkdirSync(path.dirname(this.debugLogPath), { recursive: true });
      fs.appendFileSync(this.debugLogPath, line, "utf8");
    } catch (error) {
      Log.error(`[MMM-Roborock] Could not write debug log: ${error.message}`);
    }
  },

  safeJson(value) {
    try {
      return JSON.stringify(value);
    } catch (error) {
      return `[unserializable: ${error.message}]`;
    }
  }
});
