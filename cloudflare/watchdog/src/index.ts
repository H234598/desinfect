import { DurableObject } from "cloudflare:workers";

import { readRuntimeConfig } from "./config";
import type { RuntimeConfig } from "./config";
import {
  GithubApiClient,
  recoverInactiveWorkflow,
  type GithubSecrets,
} from "./github-api";
import type { GithubWatchdogOperations, WatchdogStorage } from "./models";
import { coordinatorFor } from "./routing";
import { WatchdogStateMachine } from "./watchdog-state";

interface GithubCredentialsEnv {
  GITHUB_APP_ID?: string;
  GITHUB_INSTALLATION_ID?: string;
  GITHUB_APP_PRIVATE_KEY?: string;
}

type WatchdogEnv = Env & GithubCredentialsEnv;

class DurableWatchdogStorage implements WatchdogStorage {
  constructor(private readonly storage: DurableObjectStorage) {}

  get<T>(key: string): Promise<T | undefined> {
    return this.storage.get<T>(key);
  }

  async put<T>(key: string, value: T): Promise<void> {
    await this.storage.put(key, value);
  }

  async setAlarm(scheduledTimeMs: number): Promise<void> {
    await this.storage.setAlarm(scheduledTimeMs);
  }

  async deleteAlarm(): Promise<void> {
    await this.storage.deleteAlarm();
  }
}

function hasGithubCredentials(env: GithubCredentialsEnv): boolean {
  return [env.GITHUB_APP_ID, env.GITHUB_INSTALLATION_ID, env.GITHUB_APP_PRIVATE_KEY].every(
    (value) => typeof value === "string" && value.length > 0,
  );
}

function readGithubSecrets(env: GithubCredentialsEnv): GithubSecrets {
  if (!hasGithubCredentials(env)) {
    throw new Error("GitHub App credentials are not configured");
  }
  return {
    appId: env.GITHUB_APP_ID!,
    installationId: env.GITHUB_INSTALLATION_ID!,
    appPrivateKey: env.GITHUB_APP_PRIVATE_KEY!,
  };
}

function githubOperations(env: WatchdogEnv, config: RuntimeConfig): GithubWatchdogOperations {
  const client = (): GithubApiClient => new GithubApiClient(config, readGithubSecrets(env));
  return {
    readStatus: async () => client().getStatusSnapshot(),
    recoverWorkflow: async () => recoverInactiveWorkflow(client()),
    listRecentWorkflowRuns: async () => client().listRecentWorkflowRuns(20),
  };
}

export class WatchdogCoordinator extends DurableObject<WatchdogEnv> {
  private readonly machine: WatchdogStateMachine;

  constructor(ctx: DurableObjectState, env: WatchdogEnv) {
    super(ctx, env);
    const config = readRuntimeConfig(env);
    this.machine = new WatchdogStateMachine({
      storage: new DurableWatchdogStorage(ctx.storage),
      github: githubOperations(env, config),
      config,
    });
    ctx.blockConcurrencyWhile(async () => this.machine.initialize());
  }

  async foundationStatus(): Promise<{ ready: true }> {
    return { ready: true };
  }

  async reconcile(nowMs: number): Promise<Awaited<ReturnType<WatchdogStateMachine["reconcile"]>>> {
    return this.machine.reconcile(nowMs);
  }

  override async alarm(): Promise<void> {
    const nowMs = Date.now();
    if (!hasGithubCredentials(this.env)) {
      await this.machine.deferAlarm(nowMs);
      return;
    }
    await this.machine.handleAlarm(nowMs);
  }
}

const worker = {
  async fetch(_request: Request, _env: Env, _ctx: ExecutionContext): Promise<Response> {
    return new Response(null, { status: 404 });
  },

  async scheduled(
    controller: ScheduledController,
    env: Env,
    _ctx: ExecutionContext,
  ): Promise<void> {
    readRuntimeConfig(env);
    const coordinator = coordinatorFor(env);
    if (!hasGithubCredentials(env as WatchdogEnv)) {
      await coordinator.foundationStatus();
      return;
    }
    await coordinator.reconcile(controller.scheduledTime);
  },
} satisfies ExportedHandler<Env>;

export default worker;
