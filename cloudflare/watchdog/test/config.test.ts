import { describe, expect, it } from "vitest";

import { readRuntimeConfig } from "../src/config";

const validEnv = {
  GITHUB_OWNER: "H234598",
  GITHUB_REPO: "desinfect",
  GITHUB_WORKFLOW_FILE: "rki-dispatcher.yml",
  WATCHDOG_INTERVAL_DAYS: "45",
  WATCHDOG_GRACE_HOURS: "12",
  DISPATCH_COOLDOWN_HOURS: "24",
} as const;

describe("readRuntimeConfig", () => {
  it("parses the fixed repository and bounded integer variables", () => {
    expect(readRuntimeConfig(validEnv)).toEqual({
      owner: "H234598",
      repo: "desinfect",
      workflowFile: "rki-dispatcher.yml",
      watchdogIntervalDays: 45,
      graceHours: 12,
      dispatchCooldownHours: 24,
    });
  });

  it.each([
    ["GITHUB_OWNER", { ...validEnv, GITHUB_OWNER: "other" }],
    ["GITHUB_REPO", { ...validEnv, GITHUB_REPO: "other" }],
    ["GITHUB_WORKFLOW_FILE", { ...validEnv, GITHUB_WORKFLOW_FILE: "other.yml" }],
  ])("rejects a non-fixed %s", (name, env) => {
    expect(() => readRuntimeConfig(env)).toThrow(`${name} must match the fixed deployment target`);
  });

  it.each([
    ["WATCHDOG_INTERVAL_DAYS", "0"],
    ["WATCHDOG_INTERVAL_DAYS", "366"],
    ["WATCHDOG_GRACE_HOURS", "1.5"],
    ["WATCHDOG_GRACE_HOURS", "-1"],
    ["DISPATCH_COOLDOWN_HOURS", "169"],
  ])("rejects an unsafe %s value", (name, value) => {
    expect(() => readRuntimeConfig({ ...validEnv, [name]: value })).toThrow(
      `${name} must be a base-10 integer within the safe range`,
    );
  });
});
