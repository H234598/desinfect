import { createSignedGithubAppJwt } from "./github-app";
import type { RuntimeConfig } from "./config";

const API_VERSION = "2026-03-10";
const API_BASE = "https://api.github.com";
const USER_AGENT = "desinfect-watchdog";
const ACCEPT_HEADER = "application/vnd.github+json";
const MAX_RESPONSE_BYTES = 262_144;
const ERROR_BODY_PREVIEW_BYTES = 2_048;
const GITHUB_REQUEST_HEADERS = {
  "User-Agent": USER_AGENT,
  "Accept": ACCEPT_HEADER,
  "X-GitHub-Api-Version": API_VERSION,
};

export interface GithubSecrets {
  appId: string;
  installationId: string;
  appPrivateKey: string;
}

export interface GithubApiErrorContext {
  operation: string;
  status: number;
  retryable: boolean;
}

export class GithubApiError extends Error {
  readonly operation: string;
  readonly status: number;
  readonly retryable: boolean;

  constructor(context: GithubApiErrorContext, statusText: string, details: string) {
    super(
      `${context.operation} failed (${context.status} ${statusText}): ${redactSecrets(details || "no response body")}`,
    );
    this.operation = context.operation;
    this.status = context.status;
    this.retryable = context.retryable;
  }
}

export interface ContentsFile {
  type: "file";
  encoding: "base64";
  content: string;
}

export interface CommitRef {
  sha: string;
}

export interface WorkflowMeta {
  state: string;
}

export interface WorkflowRun {
  id: number;
  status: string;
  conclusion: string | null;
  created_at: string;
}

export interface WorkflowRuns {
  total_count: number;
  workflow_runs: WorkflowRun[];
}

export interface InstallationAccessToken {
  token: string;
  expires_at: string;
}

export interface RecoveryResult {
  action: "noop" | "enabled_and_dispatched";
  workflowState: string;
}

export interface GithubRequestOptions {
  nowMs?: number;
  fetch?: typeof globalThis.fetch;
}

export class GithubApiClient {
  private readonly config: RuntimeConfig;
  private readonly secrets: GithubSecrets;
  private readonly nowMs: number;
  private readonly fetchFn: typeof globalThis.fetch;

  constructor(
    config: RuntimeConfig,
    secrets: GithubSecrets,
    options: GithubRequestOptions = {},
  ) {
    this.config = config;
    this.secrets = validateSecrets(secrets);
    this.nowMs = options.nowMs ?? Date.now();
    this.fetchFn = options.fetch ?? globalThis.fetch;
  }

  async createInstallationAccessToken(): Promise<string> {
    const signedJwt = createSignedGithubAppJwt({
      appId: this.secrets.appId,
      privateKey: this.secrets.appPrivateKey,
      nowMs: this.nowMs,
    });
    const body = JSON.stringify({
      permissions: { actions: "write", contents: "read" },
      repositories: [this.config.repo],
    });
    const payload = await this.request<unknown>(
      "create installation token",
      `/app/installations/${encodeURIComponent(this.secrets.installationId)}/access_tokens`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${signedJwt.token}`,
        },
        body,
      },
      true,
      signedJwt,
    );
    return validateToken(payload, this.nowMs);
  }

  async getStatusJson(): Promise<Record<string, unknown>> {
    const token = await this.createInstallationAccessToken();
    const payload = await this.request<ContentsFile>(
      "fetch status.json",
      `/repos/${this.config.owner}/${this.config.repo}/contents/status.json?ref=main`,
      { method: "GET", headers: {} },
      true,
      { token },
    );
    if (payload.type !== "file" || payload.encoding !== "base64") {
      throw new Error("status.json payload has unexpected format");
    }
    return decodeStatusJsonPayload(payload.content);
  }

  async getLatestCommitSha(): Promise<string> {
    const token = await this.createInstallationAccessToken();
    const commits = await this.request<CommitRef[]>(
      "read latest commit",
      `/repos/${this.config.owner}/${this.config.repo}/commits?sha=main&per_page=1`,
      { method: "GET", headers: {} },
      true,
      { token },
    );
    if (commits.length === 0 || typeof commits[0]?.sha !== "string" || commits[0].sha.length === 0) {
      throw new Error("empty latest commit payload");
    }
    return commits[0].sha;
  }

  async getWorkflowState(): Promise<string> {
    const token = await this.createInstallationAccessToken();
    const workflow = await this.request<WorkflowMeta>(
      "read workflow state",
      `/repos/${this.config.owner}/${this.config.repo}/actions/workflows/${encodeURIComponent(
        this.config.workflowFile,
      )}`,
      { method: "GET", headers: {} },
      true,
      { token },
    );
    if (typeof workflow.state !== "string" || workflow.state.length === 0) {
      throw new Error("workflow state missing");
    }
    return workflow.state;
  }

  async enableWorkflow(): Promise<void> {
    const token = await this.createInstallationAccessToken();
    await this.request<unknown>(
      "enable workflow",
      `/repos/${this.config.owner}/${this.config.repo}/actions/workflows/${encodeURIComponent(
        this.config.workflowFile,
      )}/enable`,
      { method: "PUT", headers: {} },
      false,
      { token },
    );
  }

  async dispatchWorkflow(): Promise<void> {
    const token = await this.createInstallationAccessToken();
    await this.request<unknown>(
      "dispatch workflow",
      `/repos/${this.config.owner}/${this.config.repo}/actions/workflows/${encodeURIComponent(
        this.config.workflowFile,
      )}/dispatches`,
      {
        method: "POST",
        headers: {},
        body: JSON.stringify({ ref: "main" }),
      },
      false,
      { token },
    );
  }

  async listRecentWorkflowRuns(maxCount: number): Promise<WorkflowRun[]> {
    const safeCount = maxCount < 1 ? 1 : Math.min(20, maxCount);
    const token = await this.createInstallationAccessToken();
    const runs = await this.request<WorkflowRuns>(
      "list workflow runs",
      `/repos/${this.config.owner}/${this.config.repo}/actions/workflows/${encodeURIComponent(
        this.config.workflowFile,
      )}/runs?branch=main&event=workflow_dispatch&per_page=${safeCount}`,
      { method: "GET", headers: {} },
      true,
      { token },
    );
    if (!Array.isArray(runs.workflow_runs)) {
      throw new Error("workflow runs payload is malformed");
    }
    return runs.workflow_runs;
  }

  private async request<T>(
    operation: string,
    path: string,
    init: RequestInit,
    expectJson: boolean,
    auth?: { token: string },
  ): Promise<T> {
    if (!auth) {
      throw new Error(`${operation} missing auth`);
    }

    const headers = {
      ...GITHUB_REQUEST_HEADERS,
      ...(init.headers as Record<string, string>),
      Authorization: `Bearer ${auth.token}`,
    };

    const response = await this.fetchFn(`${API_BASE}${path}`, {
      ...init,
      headers,
    });
    const bodyText = await readBoundedResponseText(response);
    if (!response.ok) {
      throw responseToError(operation, response, bodyText);
    }

    if (!expectJson) {
      return undefined as T;
    }

    if (!bodyText) {
      throw new Error(`${operation} returned empty response`);
    }

    return parseJsonResponse<T>(operation, bodyText);
  }
}

export async function recoverInactiveWorkflow(client: GithubApiClient): Promise<RecoveryResult> {
  const state = await client.getWorkflowState();
  if (state === "active") {
    return {
      action: "noop",
      workflowState: state,
    };
  }
  if (state === "disabled_inactivity") {
    await client.enableWorkflow();
    await client.dispatchWorkflow();
    return {
      action: "enabled_and_dispatched",
      workflowState: state,
    };
  }
  throw new Error(`unexpected workflow state ${state}`);
}

function decodeStatusJsonPayload(content: string): Record<string, unknown> {
  const normalized = content.replace(/\s+/g, "");
  let decoded: string;
  try {
    decoded = Buffer.from(normalized, "base64").toString("utf8");
  } catch (error) {
    throw new Error("status payload is not base64");
  }
  let value: unknown;
  try {
    value = JSON.parse(decoded);
  } catch (error) {
    throw new Error("status payload is not valid JSON");
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("status payload is not an object");
  }
  return value as Record<string, unknown>;
}

function validateSecrets(secrets: GithubSecrets): GithubSecrets {
  const appId = secrets.appId?.trim();
  if (!/^[1-9][0-9]*$/.test(appId ?? "")) {
    throw new Error("appId must be a fixed decimal id");
  }
  if (!/^[1-9][0-9]*$/.test(secrets.installationId?.trim() ?? "")) {
    throw new Error("installationId must be a fixed decimal id");
  }
  const appPrivateKey = secrets.appPrivateKey?.trim();
  if (!appPrivateKey) {
    throw new Error("appPrivateKey must be provided");
  }
  return {
    appId,
    installationId: secrets.installationId.trim(),
    appPrivateKey,
  };
}

function validateToken(tokenPayload: unknown, nowMs: number): string {
  if (typeof tokenPayload !== "object" || tokenPayload === null || Array.isArray(tokenPayload)) {
    throw new Error("installation token payload malformed");
  }
  const { token, expires_at: expiresAtRaw } = tokenPayload as Partial<InstallationAccessToken>;
  const expiresAt = Date.parse(expiresAtRaw ?? "");
  if (
    typeof token !== "string" ||
    token.length < 10 ||
    typeof expiresAtRaw !== "string" ||
    Number.isNaN(expiresAt) ||
    expiresAt <= nowMs
  ) {
    throw new Error("installation token payload malformed");
  }
  return token;
}

function isRetryable(status: number, headers: Headers): boolean {
  if (status === 429 || (status >= 500 && status <= 504)) {
    return true;
  }
  if (status === 403 && headers.get("x-ratelimit-remaining") === "0") {
    return true;
  }
  return false;
}

function parseJsonResponse<T>(operation: string, bodyText: string): T {
  try {
    return JSON.parse(bodyText) as T;
  } catch (error) {
    throw new Error(`${operation} returned invalid JSON`);
  }
}

async function readBoundedResponseText(response: Response): Promise<string> {
  const contentLength = response.headers.get("content-length");
  if (contentLength && Number(contentLength) > MAX_RESPONSE_BYTES) {
    throw new Error(`response body exceeds ${MAX_RESPONSE_BYTES} bytes`);
  }

  if (!response.body) {
    return "";
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const chunks: string[] = [];
  let total = 0;

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    total += value.byteLength;
    if (total > MAX_RESPONSE_BYTES) {
      throw new Error(`response body exceeds ${MAX_RESPONSE_BYTES} bytes`);
    }
    chunks.push(decoder.decode(value, { stream: true }));
  }
  chunks.push(decoder.decode());

  return chunks.join("");
}

function responseToError(operation: string, response: Response, bodyText: string): GithubApiError {
  const status = response.status;
  const details = bodyText.slice(0, ERROR_BODY_PREVIEW_BYTES);
  const retryable = isRetryable(response.status, response.headers);
  return new GithubApiError(
    { operation, status, retryable },
    response.statusText || "HTTP error",
    details,
  );
}

function redactSecrets(value: string): string {
  return value
    .replace(/gh[pousr]_[A-Za-z0-9_]+/g, "[redacted-secret]")
    .replace(
      /-----BEGIN (?:RSA |EC )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC )?PRIVATE KEY-----/g,
      "[redacted-key]",
    )
    .replace(/[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+/g, "[redacted-jwt]");
}
