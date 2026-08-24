import { describe, expect, it } from "vitest";

import type { RuntimeConfig } from "../src/config";
import type {
  GithubWatchdogOperations,
  GithubWatchdogStatusSnapshot,
  WatchdogStorage,
} from "../src/models";
import {
  POSTCHECK_DELAY_MS,
  WATCHDOG_STATE_KEY,
  WatchdogStateMachine,
} from "../src/watchdog-state";

const config = {
  owner: "H234598",
  repo: "desinfect",
  workflowFile: "rki-dispatcher.yml",
  watchdogIntervalDays: 45,
  graceHours: 12,
  dispatchCooldownHours: 24,
} satisfies RuntimeConfig;

class MemoryStorage implements WatchdogStorage {
  private readonly values = new Map<string, unknown>();
  alarmAt: number | null = null;

  async get<T>(key: string): Promise<T | undefined> {
    const value = this.values.get(key);
    return value === undefined ? undefined : structuredClone(value as T);
  }

  async put<T>(key: string, value: T): Promise<void> {
    this.values.set(key, structuredClone(value));
  }

  async setAlarm(scheduledTimeMs: number): Promise<void> {
    this.alarmAt = scheduledTimeMs;
  }

  async deleteAlarm(): Promise<void> {
    this.alarmAt = null;
  }
}

class FakeGithub implements GithubWatchdogOperations {
  statusReads = 0;
  recoveries = 0;
  postchecks = 0;
  postcheckError: Error | null = null;
  runs: Awaited<ReturnType<GithubWatchdogOperations["listRecentWorkflowRuns"]>> = [];

  constructor(public snapshot: GithubWatchdogStatusSnapshot) {}

  async readStatus(): Promise<GithubWatchdogStatusSnapshot> {
    this.statusReads += 1;
    return structuredClone(this.snapshot);
  }

  async recoverWorkflow() {
    this.recoveries += 1;
    return { action: "enabled_and_dispatched", workflowState: "disabled_inactivity" } as const;
  }

  async listRecentWorkflowRuns() {
    this.postchecks += 1;
    if (this.postcheckError !== null) {
      throw this.postcheckError;
    }
    return structuredClone(this.runs);
  }
}

function statusSnapshot(status: Record<string, unknown>): GithubWatchdogStatusSnapshot {
  return { statusSha: "a".repeat(40), status };
}

function dueStatus(nextBarkAt = "2026-08-15T00:00:00Z"): Record<string, unknown> {
  return {
    repository: "H234598/desinfect",
    watchdog: {
      interval_days: 45,
      last_reset_at: "2026-07-01T00:00:00Z",
      next_bark_at: nextBarkAt,
    },
  };
}

describe("WatchdogStateMachine", () => {
  it("dispatches exactly once after the 46-day deadline under concurrent cron delivery", async () => {
    const nowMs = Date.parse("2026-08-16T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();

    const results = await Promise.all([machine.reconcile(nowMs), machine.reconcile(nowMs)]);

    expect(results.map((result) => result.action).sort()).toEqual(["cooldown", "dispatched"]);
    expect(github.statusReads).toBe(1);
    expect(github.recoveries).toBe(1);
    expect(storage.alarmAt).toBe(nowMs + POSTCHECK_DELAY_MS);
    await expect(storage.get(WATCHDOG_STATE_KEY)).resolves.toMatchObject({
      schemaVersion: 1,
      operation: {
        phase: "dispatched",
        statusSha: "a".repeat(40),
        dueAt: "2026-08-15T00:00:00.000Z",
      },
    });
  });

  it("uses the next-bark alarm to reconcile a newly due status", async () => {
    const dueMs = Date.parse("2026-08-15T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();

    await expect(machine.reconcile(dueMs - 1)).resolves.toMatchObject({ action: "not_due" });
    expect(storage.alarmAt).toBe(dueMs);

    await expect(machine.handleAlarm(dueMs)).resolves.toMatchObject({ action: "dispatched" });
    expect(github.recoveries).toBe(1);
  });

  it("defers a credential-blocked next-bark alarm before an operation exists", async () => {
    const nowMs = Date.parse("2026-08-15T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();

    await machine.deferAlarm(nowMs);

    expect(storage.alarmAt).toBe(nowMs + POSTCHECK_DELAY_MS);
    await expect(storage.get(WATCHDOG_STATE_KEY)).resolves.toMatchObject({
      operation: null,
      alarmAtMs: nowMs + POSTCHECK_DELAY_MS,
    });
  });

  it("persists the dispatch key and never duplicates it during alarm retries", async () => {
    const nowMs = Date.parse("2026-08-16T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const firstInstance = new WatchdogStateMachine({ storage, github, config });
    await firstInstance.initialize();
    await firstInstance.reconcile(nowMs);

    const restarted = new WatchdogStateMachine({ storage, github, config });
    await restarted.initialize();
    await restarted.handleAlarm(nowMs + POSTCHECK_DELAY_MS);
    const retryAt = storage.alarmAt;
    expect(retryAt).not.toBeNull();
    await restarted.handleAlarm(retryAt!);

    expect(github.recoveries).toBe(1);
    expect(github.postchecks).toBe(2);
    await expect(storage.get(WATCHDOG_STATE_KEY)).resolves.toMatchObject({
      schemaVersion: 1,
      operation: {
        phase: "dispatched",
        postcheckAttempts: 2,
      },
    });
  });

  it("marks a matching successful workflow run verified without redispatch", async () => {
    const nowMs = Date.parse("2026-08-16T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();
    await machine.reconcile(nowMs);
    github.runs = [
      {
        id: 42,
        status: "completed",
        conclusion: "success",
        created_at: "2026-08-16T00:01:00Z",
      },
    ];

    const result = await machine.handleAlarm(nowMs + POSTCHECK_DELAY_MS);

    expect(result.action).toBe("verified");
    expect(github.recoveries).toBe(1);
    expect(storage.alarmAt).toBeNull();
  });

  it("blocks a new status SHA while an earlier dispatch remains unverified", async () => {
    const nowMs = Date.parse("2026-08-16T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();
    await machine.reconcile(nowMs);
    github.snapshot = { ...github.snapshot, statusSha: "b".repeat(40) };

    const result = await machine.reconcile(nowMs + 25 * 60 * 60 * 1_000);

    expect(result.action).toBe("duplicate");
    expect(github.recoveries).toBe(1);
  });

  it("persists bounded backoff when the postcheck API fails", async () => {
    const nowMs = Date.parse("2026-08-16T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();
    await machine.reconcile(nowMs);
    github.postcheckError = new Error("temporary API failure");

    const result = await machine.handleAlarm(nowMs + POSTCHECK_DELAY_MS);

    expect(result.action).toBe("retry_scheduled");
    expect(github.recoveries).toBe(1);
    await expect(storage.get(WATCHDOG_STATE_KEY)).resolves.toMatchObject({
      operation: { phase: "dispatched", postcheckAttempts: 1 },
    });
  });

  it("does not reopen an already verified due window when the status SHA changes", async () => {
    const nowMs = Date.parse("2026-08-16T00:00:00Z");
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus()));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();
    await machine.reconcile(nowMs);
    github.runs = [
      {
        id: 43,
        status: "completed",
        conclusion: "success",
        created_at: "2026-08-16T00:01:00Z",
      },
    ];
    await machine.handleAlarm(nowMs + POSTCHECK_DELAY_MS);
    github.snapshot = { ...github.snapshot, statusSha: "b".repeat(40) };

    const result = await machine.reconcile(nowMs + 25 * 60 * 60 * 1_000);

    expect(result.action).toBe("duplicate");
    expect(github.recoveries).toBe(1);
  });

  it("fails closed on malformed status before recovery or state transition", async () => {
    const storage = new MemoryStorage();
    const github = new FakeGithub(
      statusSnapshot({
        repository: "H234598/desinfect",
        watchdog: { interval_days: 45, next_bark_at: "tomorrow" },
      }),
    );
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();

    await expect(machine.reconcile(Date.parse("2026-08-16T00:00:00Z"))).rejects.toThrow(
      "status.watchdog.next_bark_at must be an RFC3339 UTC timestamp",
    );

    expect(github.recoveries).toBe(0);
    expect(storage.alarmAt).toBeNull();
    await expect(storage.get(WATCHDOG_STATE_KEY)).resolves.toMatchObject({
      schemaVersion: 1,
      operation: null,
    });
  });

  it("rejects calendar-normalized UTC timestamps", async () => {
    const storage = new MemoryStorage();
    const github = new FakeGithub(statusSnapshot(dueStatus("2026-02-31T00:00:00Z")));
    const machine = new WatchdogStateMachine({ storage, github, config });
    await machine.initialize();

    await expect(machine.reconcile(Date.parse("2026-08-16T00:00:00Z"))).rejects.toThrow(
      "status.watchdog.next_bark_at must be an RFC3339 UTC timestamp",
    );
    expect(github.recoveries).toBe(0);
  });
});
