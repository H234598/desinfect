import { env } from "cloudflare:workers";
import {
  createExecutionContext,
  createScheduledController,
  runInDurableObject,
  waitOnExecutionContext,
} from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";

import worker, { WatchdogCoordinator } from "../src/index";
import { coordinatorFor } from "../src/routing";
import { POSTCHECK_DELAY_MS, WATCHDOG_STATE_KEY } from "../src/watchdog-state";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("watchdog Worker foundation", () => {
  it("routes only to the fixed desinfect-watchdog singleton", () => {
    const actual = coordinatorFor(env).id;
    const expected = env.WATCHDOG_COORDINATOR.idFromName("desinfect-watchdog");

    expect(actual.equals(expected)).toBe(true);
  });

  it("exposes a ready RPC on the configured Durable Object", async () => {
    const stub = coordinatorFor(env);

    await expect(stub.foundationStatus()).resolves.toEqual({ ready: true });
  });

  it("uses the SQLite Durable Object backend", async () => {
    const stub = coordinatorFor(env);

    await runInDurableObject(stub, (instance, state) => {
      expect(instance).toBeInstanceOf(WatchdogCoordinator);
      expect(state.storage.sql.exec<{ value: number }>("SELECT 1 AS value").one()).toEqual({
        value: 1,
      });
    });
  });

  it("initializes versioned persistent watchdog state in blockConcurrencyWhile", async () => {
    const stub = coordinatorFor(env);
    await stub.foundationStatus();

    await runInDurableObject(stub, async (_instance, state) => {
      await expect(state.storage.get(WATCHDOG_STATE_KEY)).resolves.toMatchObject({
        schemaVersion: 1,
        operation: null,
      });
    });
  });

  it("defers an alarm without consuming retries when GitHub credentials are absent", async () => {
    const nowMs = Date.parse("2026-08-16T06:00:00Z");
    vi.spyOn(Date, "now").mockReturnValue(nowMs);
    const stub = coordinatorFor(env);

    await runInDurableObject(stub, async (instance, state) => {
      await state.storage.put(WATCHDOG_STATE_KEY, {
        schemaVersion: 1,
        operation: {
          key: "pending-key",
          phase: "pending",
          statusSha: "a".repeat(40),
          dueAt: "2026-08-15T00:00:00.000Z",
          startedAtMs: Date.parse("2026-08-16T00:00:00Z"),
          dispatchAtMs: null,
          postcheckAttempts: 0,
        },
        cooldownUntilMs: Date.parse("2026-08-17T00:00:00Z"),
        alarmAtMs: nowMs,
      });

      await instance.alarm();

      await expect(state.storage.get(WATCHDOG_STATE_KEY)).resolves.toMatchObject({
        operation: { phase: "pending", postcheckAttempts: 0 },
        alarmAtMs: nowMs + POSTCHECK_DELAY_MS,
      });
    });
  });

  it("runs the UTC reconciliation hook without external I/O", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("unexpected fetch"));
    const controller = createScheduledController({
      cron: "0 2 * * *",
      scheduledTime: Date.parse("2026-08-04T02:00:00Z"),
    });
    const ctx = createExecutionContext();

    await worker.scheduled(controller, env, ctx);
    await waitOnExecutionContext(ctx);

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("has no public HTTP endpoint", async () => {
    const response = await worker.fetch(
      new Request("https://watchdog.invalid/"),
      env,
      createExecutionContext(),
    );

    expect(response.status).toBe(404);
  });

  it("exposes only a read-only health endpoint with deployment version", async () => {
    const response = await worker.fetch(
      new Request("https://watchdog.invalid/healthz"),
      env,
      createExecutionContext(),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({
      service: "desinfect-watchdog",
      status: "ok",
      version: "local",
    });

    const writeAttempt = await worker.fetch(
      new Request("https://watchdog.invalid/healthz", { method: "POST" }),
      env,
      createExecutionContext(),
    );
    expect(writeAttempt.status).toBe(404);
  });
});
