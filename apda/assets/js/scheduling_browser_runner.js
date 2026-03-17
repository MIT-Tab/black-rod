function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let index = 0; index < cookies.length; index += 1) {
      const cookie = cookies[index].trim();
      if (cookie.substring(0, name.length + 1) === `${name}=`) {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatTimestamp(isoString) {
  try {
    return new Date(isoString).toLocaleString();
  } catch (error) {
    return isoString;
  }
}

class SchedulingBrowserRunner {
  constructor(root) {
    this.root = root;
    this.workspaceUrl = root.dataset.workspaceUrl;
    this.saveRunUrl = root.dataset.saveRunUrl;
    this.startButton = document.getElementById("scheduling-start-button");
    this.cancelButton = document.getElementById("scheduling-cancel-button");
    this.refreshButton = document.getElementById(
      "scheduling-refresh-workspace-button",
    );
    this.statusNode = document.getElementById("scheduling-run-status");
    this.workerCountNode = document.getElementById("scheduling-worker-count");
    this.progressCountNode = document.getElementById(
      "scheduling-progress-count",
    );
    this.bestPenaltyNode = document.getElementById("scheduling-best-penalty");
    this.progressBar = document.getElementById("scheduling-progress-bar");
    this.runDetailNode = document.getElementById("scheduling-run-detail");
    this.saveStatusNode = document.getElementById("scheduling-save-status");
    this.logNode = document.getElementById("scheduling-run-log");
    this.latestScheduleTitleNode = document.getElementById(
      "scheduling-latest-schedule-title",
    );
    this.latestScheduleBodyNode = document.getElementById(
      "scheduling-latest-schedule-body",
    );
    this.originalLatestScheduleTitle = this.latestScheduleTitleNode
      ? this.latestScheduleTitleNode.textContent
      : "";
    this.originalLatestScheduleBody = this.latestScheduleBodyNode
      ? this.latestScheduleBodyNode.innerHTML
      : "";
    this.workspace = null;
    this.runState = null;
  }

  init() {
    this.startButton.addEventListener("click", () => {
      this.startRun();
    });
    this.cancelButton.addEventListener("click", () => {
      this.cancelRun();
    });
    this.refreshButton.addEventListener("click", () => {
      this.loadWorkspaceSnapshot(true);
    });
  }

  appendLog(message) {
    const timestamp = new Date().toLocaleTimeString();
    const nextValue = `${this.logNode.value}\n[${timestamp}] ${message}`.trim();
    const logLines = nextValue.split("\n").slice(-200);
    this.logNode.value = logLines.join("\n");
    this.logNode.scrollTop = this.logNode.scrollHeight;
  }

  setStatus(statusText) {
    this.statusNode.textContent = statusText;
  }

  updateProgress(completed, total) {
    const safeTotal = total || 0;
    const percent =
      safeTotal > 0 ? Math.floor((completed / safeTotal) * 100) : 0;
    this.progressCountNode.textContent = `${completed} / ${safeTotal}`;
    this.progressBar.style.width = `${percent}%`;
    this.progressBar.textContent = `${percent}%`;
  }

  restoreLatestSchedule() {
    if (!this.latestScheduleTitleNode || !this.latestScheduleBodyNode) {
      return;
    }
    this.latestScheduleTitleNode.textContent = this.originalLatestScheduleTitle;
    this.latestScheduleBodyNode.innerHTML = this.originalLatestScheduleBody;
  }

  renderBestResult(result) {
    if (!this.latestScheduleTitleNode || !this.latestScheduleBodyNode) {
      return;
    }

    this.latestScheduleTitleNode.textContent = "Live Best Result";

    if (!result) {
      this.latestScheduleBodyNode.innerHTML =
        '<p class="text-muted mb-3">Waiting for the first completed batch...</p>';
      return;
    }

    const unmatchedHtml =
      result.unmatched_schools && result.unmatched_schools.length > 0
        ? `
          <div class="mb-3">
            <strong>Unmatched Schools:</strong>
            <div class="mt-2">
              ${result.unmatched_schools
                .map(
                  (school) =>
                    `<span class="badge badge-warning mr-1 mb-1">${escapeHtml(
                      school,
                    )}</span>`,
                )
                .join("")}
            </div>
          </div>
        `
        : "";

    const scheduleRows = result.schedule
      .flatMap((day) =>
        (day.assignments || []).map(
          (assignment) => `
            <tr>
              <td>${escapeHtml(day.date)}</td>
              <td>${escapeHtml(day.weekend_count)}</td>
              <td>${escapeHtml((day.tags || []).join(", "))}</td>
              <td>${escapeHtml(assignment.school)}</td>
              <td>${escapeHtml(assignment.region || "")}</td>
              <td>${escapeHtml(assignment.preference || "")}</td>
              <td>${escapeHtml(assignment.weight)}</td>
              <td>${escapeHtml(assignment.penalties_text || "")}</td>
            </tr>
          `,
        ),
      )
      .join("");

    this.latestScheduleBodyNode.innerHTML = `
      <div class="mb-3">
        <strong>Best Penalty:</strong> ${escapeHtml(result.best_penalty)}
        <span class="ml-3"><strong>Best Seed:</strong> ${escapeHtml(
          result.best_seed,
        )}</span>
      </div>
      ${unmatchedHtml}
      <div class="table-responsive mb-3">
        <table class="table table-sm table-striped mb-0">
          <thead>
            <tr>
              <th>Date</th>
              <th>Slots</th>
              <th>Tags</th>
              <th>School</th>
              <th>Region</th>
              <th>Preference</th>
              <th>Score</th>
              <th>Penalty Notes</th>
            </tr>
          </thead>
          <tbody>
            ${scheduleRows}
          </tbody>
        </table>
      </div>
      <div>
        <strong class="d-block mb-2">Copyable Text Output</strong>
        <textarea class="form-control" rows="12" readonly>${escapeHtml(
          result.output_text,
        )}</textarea>
      </div>
    `;
  }

  async loadWorkspaceSnapshot(logReload) {
    this.runDetailNode.textContent = "Loading the latest workspace snapshot...";
    const response = await fetch(this.workspaceUrl, {
      credentials: "same-origin",
    });
    const payload = await response.json();
    if (!response.ok || !payload.success) {
      throw new Error(
        payload.error || "Unable to load the workspace snapshot.",
      );
    }

    this.workspace = payload.workspace;
    this.runDetailNode.textContent = `Workspace version ${
      this.workspace.version
    } loaded. Last updated ${formatTimestamp(this.workspace.updated_at)}.`;

    if (logReload) {
      const { school_count: schoolCount } = this.workspace.summary;
      const { scenario_count: scenarioCount } = this.workspace.summary;
      this.appendLog(
        `Loaded workspace v${this.workspace.version} with ${schoolCount} schools and ${scenarioCount} scenarios.`,
      );
    }

    return this.workspace;
  }

  calculateWorkerCount() {
    const hardware = window.navigator.hardwareConcurrency || 4;
    return Math.max(
      1,
      Math.min(
        this.workspace.settings.max_workers,
        hardware,
        this.workspace.summary.scenario_count,
      ),
    );
  }

  calculateBatchSize(workerCount) {
    return Math.max(
      1,
      Math.min(
        64,
        Math.ceil(
          this.workspace.summary.scenario_count / Math.max(1, workerCount * 8),
        ),
      ),
    );
  }

  buildWorker(index) {
    const worker = new Worker(
      new URL("./scheduler_worker.js", import.meta.url),
    );
    const workerState = {
      index,
      worker,
      initialized: false,
      busy: false,
    };

    worker.onmessage = (event) => {
      this.handleWorkerMessage(workerState, event.data);
    };
    worker.onerror = (event) => {
      this.failRun(`Worker ${index + 1} failed: ${event.message}`);
    };
    worker.postMessage({
      type: "init",
      payload: {
        schoolsCsv: this.workspace.schools_csv,
        datesCsv: this.workspace.dates_csv,
        settings: this.workspace.settings,
      },
    });
    return workerState;
  }

  handleWorkerMessage(workerState, message) {
    const currentWorker = workerState;
    if (!this.runState || this.runState.cancelled) {
      return;
    }

    if (message.type === "initialized") {
      currentWorker.initialized = true;
      if (this.runState.workers.every((candidate) => candidate.initialized)) {
        this.appendLog(
          `All ${this.runState.workers.length} workers initialized. Starting batches.`,
        );
        this.runState.workers.forEach((candidate) => {
          this.dispatchNextBatch(candidate);
        });
      }
      return;
    }

    if (message.type === "batchComplete") {
      currentWorker.busy = false;
      this.runState.completed += message.payload.completedSeeds;
      if (
        message.payload.bestResult &&
        (!this.runState.bestResult ||
          message.payload.bestResult.best_penalty >
            this.runState.bestResult.best_penalty)
      ) {
        this.runState.bestResult = message.payload.bestResult;
        this.bestPenaltyNode.textContent =
          this.runState.bestResult.best_penalty;
        this.renderBestResult(this.runState.bestResult);
      }

      this.updateProgress(
        this.runState.completed,
        this.workspace.summary.scenario_count,
      );

      if (this.runState.nextSeed < this.workspace.summary.scenario_count) {
        this.dispatchNextBatch(currentWorker);
      } else if (this.runState.workers.every((candidate) => !candidate.busy)) {
        this.finishRun();
      }
      return;
    }

    if (message.type === "error") {
      this.failRun(message.error || "The browser worker reported an error.");
    }
  }

  dispatchNextBatch(workerState) {
    const currentWorker = workerState;
    if (!this.runState || this.runState.cancelled) {
      return;
    }
    if (this.runState.nextSeed >= this.workspace.summary.scenario_count) {
      return;
    }

    const startSeed = this.runState.nextSeed;
    const endSeed = Math.min(
      this.workspace.summary.scenario_count,
      startSeed + this.runState.batchSize,
    );
    this.runState.nextSeed = endSeed;
    currentWorker.busy = true;
    currentWorker.worker.postMessage({
      type: "runBatch",
      payload: {
        startSeed,
        endSeed,
      },
    });
  }

  async startRun() {
    if (this.runState && this.runState.running) {
      return;
    }

    try {
      await this.loadWorkspaceSnapshot(true);
    } catch (error) {
      this.failRun(error.message || String(error), false);
      return;
    }

    this.saveStatusNode.textContent = "";
    this.setStatus("Running in browser");
    this.startButton.disabled = true;
    this.cancelButton.disabled = false;
    this.refreshButton.disabled = true;
    this.progressBar.classList.add("progress-bar-animated");
    this.updateProgress(0, this.workspace.summary.scenario_count);
    this.bestPenaltyNode.textContent = "-";
    this.renderBestResult(null);

    const workerCount = this.calculateWorkerCount();
    const batchSize = this.calculateBatchSize(workerCount);
    this.workerCountNode.textContent = String(workerCount);
    this.appendLog(
      `Starting browser run with ${workerCount} workers and batch size ${batchSize}.`,
    );

    this.runState = {
      running: true,
      cancelled: false,
      completed: 0,
      nextSeed: 0,
      batchSize,
      bestResult: null,
      startedAt: Date.now(),
      workers: [],
    };
    this.runState.workers = Array.from({ length: workerCount }, (_, index) =>
      this.buildWorker(index),
    );
  }

  teardownWorkers() {
    if (!this.runState || !this.runState.workers) {
      return;
    }
    this.runState.workers.forEach((workerState) => {
      workerState.worker.terminate();
    });
  }

  cancelRun() {
    if (!this.runState || !this.runState.running) {
      return;
    }
    this.runState.cancelled = true;
    this.runState.running = false;
    this.teardownWorkers();
    this.progressBar.classList.remove("progress-bar-animated");
    this.setStatus("Cancelled");
    this.saveStatusNode.textContent =
      "Cancelled in browser. Nothing was saved to the server.";
    this.appendLog("Run cancelled by user.");
    this.startButton.disabled = false;
    this.cancelButton.disabled = true;
    this.refreshButton.disabled = false;
    this.restoreLatestSchedule();
  }

  async saveCompletedRun() {
    this.saveStatusNode.textContent = "Saving completed run to the server...";
    const response = await fetch(this.saveRunUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCookie("csrftoken"),
      },
      body: JSON.stringify({
        status: "completed",
        workspace_version: this.workspace.version,
        settings_snapshot: this.workspace.settings,
        result: this.runState.bestResult,
        output_text: this.runState.bestResult.output_text,
      }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.success) {
      throw new Error(payload.error || "Could not save the completed run.");
    }

    this.saveStatusNode.innerHTML = `Saved as run #${escapeHtml(
      payload.run_id,
    )}. Reload the page to refresh Recent Runs and Latest Completed Run.`;
    this.appendLog(`Saved completed browser run as #${payload.run_id}.`);
  }

  async finishRun() {
    if (!this.runState || this.runState.cancelled || !this.runState.running) {
      return;
    }

    this.runState.running = false;
    this.teardownWorkers();
    this.progressBar.classList.remove("progress-bar-animated");
    this.setStatus("Completed locally");
    this.startButton.disabled = false;
    this.cancelButton.disabled = true;
    this.refreshButton.disabled = false;

    if (!this.runState.bestResult) {
      this.failRun("The browser run finished without producing a result.");
      return;
    }

    const elapsedSeconds = Math.round(
      (Date.now() - this.runState.startedAt) / 1000,
    );
    const { best_penalty: bestPenalty, best_seed: bestSeed } =
      this.runState.bestResult;
    this.appendLog(
      `Browser run finished in ${elapsedSeconds}s. Best penalty ${bestPenalty} from seed ${bestSeed}.`,
    );

    try {
      await this.saveCompletedRun();
      this.setStatus("Completed and saved");
    } catch (error) {
      this.setStatus("Completed locally, save failed");
      this.saveStatusNode.textContent = error.message || String(error);
      this.appendLog(`Save failed: ${error.message || String(error)}`);
    }
    this.restoreLatestSchedule();
  }

  failRun(message, append = true) {
    if (append) {
      this.appendLog(message);
    }
    this.teardownWorkers();
    if (this.runState) {
      this.runState.running = false;
    }
    this.progressBar.classList.remove("progress-bar-animated");
    this.setStatus("Failed");
    this.saveStatusNode.textContent = message;
    this.startButton.disabled = false;
    this.cancelButton.disabled = true;
    this.refreshButton.disabled = false;
    this.restoreLatestSchedule();
  }
}

export default function initSchedulingBrowserRunner() {
  const root = document.getElementById("scheduling-browser-app");
  if (!root) {
    return;
  }

  const runner = new SchedulingBrowserRunner(root);
  runner.init();
}
