export interface RuntimeConfigEnv {
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  GITHUB_WORKFLOW_FILE: string;
  WATCHDOG_INTERVAL_DAYS: string;
  WATCHDOG_GRACE_HOURS: string;
  DISPATCH_COOLDOWN_HOURS: string;
}

const FIXED_OWNER = "H234598";
const FIXED_REPO = "desinfect";
const FIXED_WORKFLOW_FILE = "rki-dispatcher.yml";

export interface RuntimeConfig {
  owner: typeof FIXED_OWNER;
  repo: typeof FIXED_REPO;
  workflowFile: typeof FIXED_WORKFLOW_FILE;
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
  requireFixed("GITHUB_OWNER", env.GITHUB_OWNER, FIXED_OWNER);
  requireFixed("GITHUB_REPO", env.GITHUB_REPO, FIXED_REPO);
  requireFixed("GITHUB_WORKFLOW_FILE", env.GITHUB_WORKFLOW_FILE, FIXED_WORKFLOW_FILE);

  return {
    owner: FIXED_OWNER,
    repo: FIXED_REPO,
    workflowFile: FIXED_WORKFLOW_FILE,
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
