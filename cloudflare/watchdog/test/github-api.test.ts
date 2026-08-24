import { afterEach, describe, expect, it, vi } from "vitest";
import { generateKeyPairSync } from "node:crypto";

import { createSignedGithubAppJwt } from "../src/github-app";
import { GithubApiClient, recoverInactiveWorkflow } from "../src/github-api";
import type { RuntimeConfig } from "../src/config";

function createKeyPair(): { privateKey: string } {
  const keyPair = generateKeyPairSync("rsa", {
    modulusLength: 2048,
  });
  const privateKey = keyPair.privateKey.export({ type: "pkcs1", format: "pem" }).toString();
  return { privateKey };
}

function buildResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/vnd.github+json",
    },
  });
}

const validRuntimeConfig = {
  owner: "H234598",
  repo: "desinfect",
  workflowFile: "rki-dispatcher.yml",
  watchdogIntervalDays: 45,
  graceHours: 12,
  dispatchCooldownHours: 24,
} satisfies RuntimeConfig;

describe("createSignedGithubAppJwt", () => {
  it("emits RS256 token with short fixed lifetime and skew-safe iat/exp", () => {
    const keys = createKeyPair();
    const nowMs = Date.parse("2026-08-04T00:00:00Z");
    const token = createSignedGithubAppJwt({
      appId: "123456",
      privateKey: keys.privateKey,
      nowMs,
      ttlSeconds: 540,
      clockSkewSeconds: 60,
    });

    const [, rawClaims] = token.token.split(".");
    expect(token.token.split(".")).toHaveLength(3);

    if (!rawClaims) {
      throw new Error("jwt claim segment missing");
    }
    const claims = JSON.parse(Buffer.from(rawClaims, "base64url").toString("utf8")) as {
      iat: number;
      exp: number;
      iss: string;
    };
    expect(claims.iss).toBe("123456");
    expect(token.issuedAt).toBe(claims.iat);
    expect(token.expiresAt).toBe(claims.exp);
    expect(token.expiresAt - token.issuedAt).toBe(540);
    expect(claims.iat).toBe(Math.floor(nowMs / 1000) - 60);
  });

  it("rejects a non-finite signing clock", () => {
    const keys = createKeyPair();

    expect(() =>
      createSignedGithubAppJwt({
        appId: "123456",
        privateKey: keys.privateKey,
        nowMs: Number.NaN,
      }),
    ).toThrow("nowMs must be finite");
  });
});

describe("GithubApiClient", () => {
  const nowMs = Date.parse("2026-08-04T00:00:00Z");
  const keys = createKeyPair();

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the validated Git blob SHA with the decoded status document", async () => {
    const status = {
      repository: "H234598/desinfect",
      watchdog: { interval_days: 45, next_bark_at: "2026-09-18T00:00:00Z" },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        buildResponse({
          token: "ghs_test_token_1",
          expires_at: "2026-08-04T01:00:00Z",
        }),
      )
      .mockResolvedValueOnce(
        buildResponse({
          type: "file",
          encoding: "base64",
          sha: "a".repeat(40),
          content: Buffer.from(JSON.stringify(status)).toString("base64"),
        }),
      );
    const client = new GithubApiClient(
      validRuntimeConfig,
      {
        appId: "123456",
        installationId: "654321",
        appPrivateKey: keys.privateKey,
      },
      { fetch: fetchMock, nowMs },
    );

    await expect(client.getStatusSnapshot()).resolves.toEqual({
      statusSha: "a".repeat(40),
      status,
    });
  });

  it("reads workflow state from fixed repo and fixed workflow path", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        buildResponse({
          token: "ghs_test_token_1",
          expires_at: "2026-08-04T01:00:00Z",
        }),
      )
      .mockResolvedValueOnce(
        buildResponse({
          state: "active",
          path: ".github/workflows/rki-dispatcher.yml",
          name: "rki-dispatcher.yml",
          id: 1,
        }),
      );

    const client = new GithubApiClient(
      validRuntimeConfig,
      {
        appId: "123456",
        installationId: "654321",
        appPrivateKey: keys.privateKey,
      },
      { fetch: fetchMock, nowMs },
    );

    const state = await client.getWorkflowState();
    expect(state).toBe("active");
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const tokenRequest = fetchMock.mock.calls[0]!;
    const workflowRequest = fetchMock.mock.calls[1]!;
    expect(tokenRequest[0]).toBe("https://api.github.com/app/installations/654321/access_tokens");
    expect(workflowRequest[0]).toBe(
      "https://api.github.com/repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml",
    );

    const body = JSON.parse((tokenRequest[1] as RequestInit).body as string);
    expect(body).toEqual({
      permissions: { actions: "write", contents: "read" },
      repositories: ["desinfect"],
    });

    const headers = new Headers(tokenRequest[1]!.headers as HeadersInit);
    expect(headers.get("accept")).toBe("application/vnd.github+json");
    expect(headers.get("x-github-api-version")).toBe("2026-03-10");
    expect(headers.get("user-agent")).toBe("desinfect-watchdog");
    expect(headers.get("authorization")).toContain("Bearer");
  });

  it.each([
    ["missing", { token: "ghs_test_token_1" }],
    ["expired", { token: "ghs_test_token_1", expires_at: "2026-08-03T23:59:59Z" }],
  ])("rejects an installation token with %s expiry", async (_case, tokenPayload) => {
    const client = new GithubApiClient(
      validRuntimeConfig,
      {
        appId: "123456",
        installationId: "654321",
        appPrivateKey: keys.privateKey,
      },
      { fetch: vi.fn().mockResolvedValue(buildResponse(tokenPayload)), nowMs },
    );

    await expect(client.createInstallationAccessToken()).rejects.toThrow(
      "installation token payload malformed",
    );
  });

  it("rejects a non-object installation token payload", async () => {
    const client = new GithubApiClient(
      validRuntimeConfig,
      {
        appId: "123456",
        installationId: "654321",
        appPrivateKey: keys.privateKey,
      },
      { fetch: vi.fn().mockResolvedValue(buildResponse(null)), nowMs },
    );

    await expect(client.createInstallationAccessToken()).rejects.toThrow(
      "installation token payload malformed",
    );
  });

  it("cancels an oversized GitHub response stream", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(262_145));
      },
      cancel() {
        cancelled = true;
      },
    });
    const client = new GithubApiClient(
      validRuntimeConfig,
      {
        appId: "123456",
        installationId: "654321",
        appPrivateKey: keys.privateKey,
      },
      { fetch: vi.fn().mockResolvedValue(new Response(body)), nowMs },
    );

    await expect(client.createInstallationAccessToken()).rejects.toThrow(
      "response body exceeds 262144 bytes",
    );
    expect(cancelled).toBe(true);
  });

  it("no-ops active workflow and does not call enable/dispatch", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        buildResponse({
          token: "ghs_test_token_1",
          expires_at: "2026-08-04T01:00:00Z",
        }),
      )
      .mockResolvedValueOnce(
        buildResponse({
          state: "active",
        }),
      );

    const client = new GithubApiClient(
      validRuntimeConfig,
      {
        appId: "123456",
        installationId: "654321",
        appPrivateKey: keys.privateKey,
      },
      { fetch: fetchMock, nowMs },
    );

    const result = await recoverInactiveWorkflow(client);

    expect(result).toEqual({ action: "noop", workflowState: "active" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("enables and dispatches when workflow is disabled_inactivity", async () => {
    const tokenResponse = buildResponse({
      token: "ghs_test_token_1",
      expires_at: "2026-08-04T01:00:00Z",
    });
    const tokenResponse2 = buildResponse({
      token: "ghs_test_token_2",
      expires_at: "2026-08-04T01:00:00Z",
    });
    const tokenResponse3 = buildResponse({
      token: "ghs_test_token_3",
      expires_at: "2026-08-04T01:00:00Z",
    });

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(tokenResponse)
      .mockResolvedValueOnce(
        buildResponse({
          state: "disabled_inactivity",
        }),
      )
      .mockResolvedValueOnce(tokenResponse2)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(tokenResponse3)
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    const client = new GithubApiClient(
      validRuntimeConfig,
      {
        appId: "123456",
        installationId: "654321",
        appPrivateKey: keys.privateKey,
      },
      { fetch: fetchMock, nowMs },
    );

    const result = await recoverInactiveWorkflow(client);
    expect(result).toEqual({ action: "enabled_and_dispatched", workflowState: "disabled_inactivity" });
    expect(fetchMock).toHaveBeenCalledTimes(6);

    const enableCall = fetchMock.mock.calls[3]?.[0];
    const dispatchCall = fetchMock.mock.calls[5]?.[0];
    expect(enableCall).toBe(
      "https://api.github.com/repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/enable",
    );
    expect(dispatchCall).toBe(
      "https://api.github.com/repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/dispatches",
    );

    const dispatchBody = JSON.parse((fetchMock.mock.calls[5]![1] as RequestInit).body as string);
    expect(dispatchBody).toEqual({ ref: "main" });
  });
});
