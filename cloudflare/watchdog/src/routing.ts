import type { WatchdogCoordinator } from "./index";

const SINGLETON_NAME = "desinfect-watchdog";

export function coordinatorFor(env: Env): DurableObjectStub<WatchdogCoordinator> {
  return env.WATCHDOG_COORDINATOR.getByName(SINGLETON_NAME);
}
