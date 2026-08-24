import type {
  GithubWatchdogStatusSnapshot,
  PersistentWatchdogState,
  WatchdogActionResult,
  WatchdogOperationState,
  WatchdogStateMachineOptions,
} from "./models";

export const WATCHDOG_STATE_KEY = "watchdog-state-v1";
export const POSTCHECK_DELAY_MS = 6 * 60 * 60 * 1_000;

const STATE_SCHEMA_VERSION = 1;
const MAX_POSTCHECK_ATTEMPTS = 3;
const MAX_POSTCHECK_DELAY_MS = 24 * 60 * 60 * 1_000;
const FIXED_TASKSET = "enable-disabled-workflow,dispatch-main";

function initialState(): PersistentWatchdogState {
  return {
    schemaVersion: STATE_SCHEMA_VERSION,
    operation: null,
    cooldownUntilMs: null,
    alarmAtMs: null,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableFiniteNumber(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function isOperation(value: unknown): value is WatchdogOperationState {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.key === "string" &&
    ["pending", "dispatched", "verified", "failed"].includes(String(value.phase)) &&
    typeof value.statusSha === "string" &&
    typeof value.dueAt === "string" &&
    typeof value.startedAtMs === "number" &&
    Number.isFinite(value.startedAtMs) &&
    isNullableFiniteNumber(value.dispatchAtMs) &&
    Number.isSafeInteger(value.postcheckAttempts) &&
    Number(value.postcheckAttempts) >= 0
  );
}

function validateState(value: unknown): PersistentWatchdogState {
  if (
    !isRecord(value) ||
    value.schemaVersion !== STATE_SCHEMA_VERSION ||
    (value.operation !== null && !isOperation(value.operation)) ||
    !isNullableFiniteNumber(value.cooldownUntilMs) ||
    !isNullableFiniteNumber(value.alarmAtMs)
  ) {
    throw new Error("persisted watchdog state is malformed or has an unsupported schema");
  }
  return value as unknown as PersistentWatchdogState;
}

function parseUtcTimestamp(value: unknown, field: string): { iso: string; ms: number } {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)
  ) {
    throw new Error(`${field} must be an RFC3339 UTC timestamp`);
  }
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) {
    throw new Error(`${field} must be an RFC3339 UTC timestamp`);
  }
  const iso = new Date(ms).toISOString();
  const canonicalInput = value.includes(".")
    ? value.replace(/\.(\d{1,3})Z$/, (_match, fraction: string) => `.${fraction.padEnd(3, "0")}Z`)
    : value.replace(/Z$/, ".000Z");
  if (iso !== canonicalInput) {
    throw new Error(`${field} must be an RFC3339 UTC timestamp`);
  }
  return { iso, ms };
}

function parseDueAt(
  snapshot: GithubWatchdogStatusSnapshot,
  expectedRepository: string,
  expectedIntervalDays: number,
): { iso: string; ms: number } | null {
  if (!/^[0-9a-f]{40}$/.test(snapshot.statusSha)) {
    throw new Error("status SHA must be a 40-character lowercase hexadecimal Git blob SHA");
  }
  if (snapshot.status.repository !== expectedRepository) {
    throw new Error("status repository does not match the fixed deployment target");
  }
  const watchdog = snapshot.status.watchdog;
  if (!isRecord(watchdog)) {
    throw new Error("status.watchdog must be an object");
  }
  if (watchdog.interval_days !== expectedIntervalDays) {
    throw new Error("status.watchdog.interval_days does not match runtime configuration");
  }
  if (watchdog.next_bark_at === null) {
    return null;
  }
  return parseUtcTimestamp(watchdog.next_bark_at, "status.watchdog.next_bark_at");
}

function operationKey(
  statusSha: string,
  dueAt: string,
  workflowFile: string,
): string {
  return `${statusSha}:${dueAt}:${workflowFile}:${FIXED_TASKSET}`;
}

export class WatchdogStateMachine {
  private readonly storage: WatchdogStateMachineOptions["storage"];
  private readonly github: WatchdogStateMachineOptions["github"];
  private readonly config: WatchdogStateMachineOptions["config"];
  private lock: Promise<void> = Promise.resolve();

  constructor({ storage, github, config }: WatchdogStateMachineOptions) {
    this.storage = storage;
    this.github = github;
    this.config = config;
  }

  async initialize(): Promise<void> {
    await this.runExclusive(async () => {
      await this.loadState();
    });
  }

  async reconcile(nowMs: number): Promise<WatchdogActionResult> {
    return this.runExclusive(async () => this.reconcileExclusive(nowMs));
  }

  async handleAlarm(nowMs: number): Promise<WatchdogActionResult> {
    return this.runExclusive(async () => this.handleAlarmExclusive(nowMs));
  }

  async deferAlarm(nowMs: number): Promise<void> {
    await this.runExclusive(async () => {
      this.requireFiniteClock(nowMs);
      const state = await this.loadState();
      if (
        state.operation === null ||
        state.operation.phase === "pending" ||
        state.operation.phase === "dispatched"
      ) {
        await this.setAlarm(state, nowMs + POSTCHECK_DELAY_MS);
      }
    });
  }

  private async reconcileExclusive(nowMs: number): Promise<WatchdogActionResult> {
    this.requireFiniteClock(nowMs);
    const state = await this.loadState();
    if (state.cooldownUntilMs !== null && nowMs < state.cooldownUntilMs) {
      const alarmAtMs = Math.min(state.alarmAtMs ?? state.cooldownUntilMs, state.cooldownUntilMs);
      await this.setAlarm(state, alarmAtMs);
      return state.operation === null
        ? { action: "cooldown" }
        : { action: "cooldown", key: state.operation.key };
    }
    if (
      state.operation !== null &&
      (state.operation.phase === "pending" || state.operation.phase === "dispatched")
    ) {
      if (state.alarmAtMs !== null) {
        await this.storage.setAlarm(state.alarmAtMs);
      }
      return { action: "duplicate", key: state.operation.key };
    }

    const snapshot = await this.github.readStatus();
    const dueAt = parseDueAt(
      snapshot,
      `${this.config.owner}/${this.config.repo}`,
      this.config.watchdogIntervalDays,
    );
    if (dueAt === null) {
      await this.clearAlarm(state);
      return { action: "unarmed" };
    }
    if (nowMs < dueAt.ms) {
      await this.setAlarm(state, dueAt.ms);
      return { action: "not_due" };
    }

    const key = operationKey(snapshot.statusSha, dueAt.iso, this.config.workflowFile);
    if (state.operation !== null && (state.operation.key === key || state.operation.dueAt === dueAt.iso)) {
      if (state.alarmAtMs !== null) {
        await this.storage.setAlarm(state.alarmAtMs);
      }
      return { action: "duplicate", key: state.operation.key };
    }

    state.operation = {
      key,
      phase: "pending",
      statusSha: snapshot.statusSha,
      dueAt: dueAt.iso,
      startedAtMs: nowMs,
      dispatchAtMs: null,
      postcheckAttempts: 0,
    };
    state.cooldownUntilMs = nowMs + this.config.dispatchCooldownHours * 60 * 60 * 1_000;
    await this.setAlarm(state, nowMs + POSTCHECK_DELAY_MS);

    try {
      const recovery = await this.github.recoverWorkflow();
      if (recovery.action === "noop") {
        state.operation.phase = "verified";
        await this.clearAlarm(state);
        return { action: "noop", key };
      }
      state.operation.phase = "dispatched";
      state.operation.dispatchAtMs = nowMs;
      await this.setAlarm(state, nowMs + POSTCHECK_DELAY_MS);
      return { action: "dispatched", key };
    } catch (error) {
      await this.setAlarm(state, nowMs + POSTCHECK_DELAY_MS);
      throw error;
    }
  }

  private async handleAlarmExclusive(nowMs: number): Promise<WatchdogActionResult> {
    this.requireFiniteClock(nowMs);
    const state = await this.loadState();
    const operation = state.operation;
    if (operation === null) {
      return this.reconcileExclusive(nowMs);
    }
    if (operation.phase === "verified" || operation.phase === "failed") {
      await this.clearAlarm(state);
      return { action: operation.phase === "failed" ? "failed" : "verified", key: operation.key };
    }

    let runs: Awaited<ReturnType<typeof this.github.listRecentWorkflowRuns>>;
    try {
      runs = await this.github.listRecentWorkflowRuns();
    } catch {
      return this.schedulePostcheckRetry(state, operation, nowMs);
    }
    const matching = runs
      .filter((run) => {
        const createdAtMs = Date.parse(run.created_at);
        return Number.isFinite(createdAtMs) && createdAtMs >= operation.startedAtMs;
      })
      .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at))[0];

    if (matching?.status === "completed") {
      operation.phase = matching.conclusion === "success" ? "verified" : "failed";
      await this.clearAlarm(state);
      return { action: operation.phase, key: operation.key };
    }

    return this.schedulePostcheckRetry(state, operation, nowMs);
  }

  private async schedulePostcheckRetry(
    state: PersistentWatchdogState,
    operation: WatchdogOperationState,
    nowMs: number,
  ): Promise<WatchdogActionResult> {
    operation.postcheckAttempts += 1;
    if (operation.postcheckAttempts >= MAX_POSTCHECK_ATTEMPTS) {
      operation.phase = "failed";
      await this.clearAlarm(state);
      return { action: "failed", key: operation.key };
    }

    const delayMs = Math.min(
      POSTCHECK_DELAY_MS * 2 ** operation.postcheckAttempts,
      MAX_POSTCHECK_DELAY_MS,
    );
    await this.setAlarm(state, nowMs + delayMs);
    return { action: "retry_scheduled", key: operation.key };
  }

  private async loadState(): Promise<PersistentWatchdogState> {
    const current = await this.storage.get<unknown>(WATCHDOG_STATE_KEY);
    if (current !== undefined) {
      return validateState(current);
    }
    const created = initialState();
    await this.storage.put(WATCHDOG_STATE_KEY, created);
    return created;
  }

  private async setAlarm(state: PersistentWatchdogState, alarmAtMs: number): Promise<void> {
    state.alarmAtMs = alarmAtMs;
    await this.storage.put(WATCHDOG_STATE_KEY, state);
    await this.storage.setAlarm(alarmAtMs);
  }

  private async clearAlarm(state: PersistentWatchdogState): Promise<void> {
    state.alarmAtMs = null;
    await this.storage.put(WATCHDOG_STATE_KEY, state);
    await this.storage.deleteAlarm();
  }

  private requireFiniteClock(nowMs: number): void {
    if (!Number.isFinite(nowMs)) {
      throw new Error("nowMs must be finite");
    }
  }

  private async runExclusive<T>(callback: () => Promise<T>): Promise<T> {
    const previous = this.lock;
    let release!: () => void;
    this.lock = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await callback();
    } finally {
      release();
    }
  }
}
