export interface RuntimeConfigEnv {
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  GITHUB_WORKFLOW_FILE: string;
  WATCHDOG_INTERVAL_DAYS: string;
  WATCHDOG_GRACE_HOURS: string;
  DISPATCH_COOLDOWN_HOURS: string;
}

export interface RuntimeConfig {
  owner: "H234598";
  repo: "desinfect";
  workflowFile: "rki-dispatcher.yml";
  watchdogIntervalDays: number;
  graceHours: number;
  dispatchCooldownHours: number;
}

function requireFixed(name: string, actual: string, expected: string): void {
  if (actual !== expected) {
    throw new Error(`${name} must match the fixed deployment target`);
  }
}

function readBoundedInteger(name: string, raw: string, maximum: number): number {
  if (!/^[1-9][0-9]*$/.test(raw)) {
    throw new Error(`${name} must be a base-10 integer within the safe range`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > maximum) {
    throw new Error(`${name} must be a base-10 integer within the safe range`);
  }
  return value;
}

export function readRuntimeConfig(env: RuntimeConfigEnv): RuntimeConfig {
  requireFixed("GITHUB_OWNER", env.GITHUB_OWNER, "H234598");
  requireFixed("GITHUB_REPO", env.GITHUB_REPO, "desinfect");
  requireFixed("GITHUB_WORKFLOW_FILE", env.GITHUB_WORKFLOW_FILE, "rki-dispatcher.yml");

  return {
    owner: "H234598",
    repo: "desinfect",
    workflowFile: "rki-dispatcher.yml",
    watchdogIntervalDays: readBoundedInteger(
      "WATCHDOG_INTERVAL_DAYS",
      env.WATCHDOG_INTERVAL_DAYS,
      365,
    ),
    graceHours: readBoundedInteger("WATCHDOG_GRACE_HOURS", env.WATCHDOG_GRACE_HOURS, 168),
    dispatchCooldownHours: readBoundedInteger(
      "DISPATCH_COOLDOWN_HOURS",
      env.DISPATCH_COOLDOWN_HOURS,
      168,
    ),
  };
}
