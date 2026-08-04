import { DurableObject } from "cloudflare:workers";

import { readRuntimeConfig } from "./config";
import { coordinatorFor } from "./routing";

export class WatchdogCoordinator extends DurableObject<Env> {
  async foundationStatus(): Promise<{ ready: true }> {
    return { ready: true };
  }
}

const worker = {
  async fetch(_request: Request, _env: Env, _ctx: ExecutionContext): Promise<Response> {
    return new Response(null, { status: 404 });
  },

  async scheduled(
    _controller: ScheduledController,
    env: Env,
    _ctx: ExecutionContext,
  ): Promise<void> {
    readRuntimeConfig(env);
    await coordinatorFor(env).foundationStatus();
  },
} satisfies ExportedHandler<Env>;

export default worker;
