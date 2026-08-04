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
});
