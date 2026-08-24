import type { RuntimeConfig } from "./config";
import type { RecoveryResult, WorkflowRun } from "./github-api";

export interface GithubWatchdogStatusSnapshot {
  statusSha: string;
  status: Record<string, unknown>;
}

export interface GithubWatchdogOperations {
  readStatus(): Promise<GithubWatchdogStatusSnapshot>;
  recoverWorkflow(): Promise<RecoveryResult>;
  listRecentWorkflowRuns(): Promise<WorkflowRun[]>;
}

export interface WatchdogStorage {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
  setAlarm(scheduledTimeMs: number): Promise<void>;
  deleteAlarm(): Promise<void>;
}

export type WatchdogOperationPhase = "pending" | "dispatched" | "verified" | "failed";

export interface WatchdogOperationState {
  key: string;
  phase: WatchdogOperationPhase;
  statusSha: string;
  dueAt: string;
  startedAtMs: number;
  dispatchAtMs: number | null;
  postcheckAttempts: number;
}

export interface PersistentWatchdogState {
  schemaVersion: 1;
  operation: WatchdogOperationState | null;
  cooldownUntilMs: number | null;
  alarmAtMs: number | null;
}

export interface WatchdogStateMachineOptions {
  storage: WatchdogStorage;
  github: GithubWatchdogOperations;
  config: RuntimeConfig;
}

export interface WatchdogActionResult {
  action: "unarmed" | "not_due" | "cooldown" | "duplicate" | "noop" | "dispatched" | "retry_scheduled" | "verified" | "failed";
  key?: string;
}
