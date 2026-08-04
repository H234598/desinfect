import { createSign } from "node:crypto";

const DEFAULT_JWT_TTL_SECONDS = 540;
const DEFAULT_CLOCK_SKEW_SECONDS = 60;
const APP_ID_RE = /^\d+$/;

interface SignedJwtOptions {
  appId: string;
  privateKey: string;
  nowMs?: number;
  ttlSeconds?: number;
  clockSkewSeconds?: number;
}

export interface SignedJwtResult {
  token: string;
  issuedAt: number;
  expiresAt: number;
}

function base64Url(value: string): string {
  return Buffer.from(value, "utf8")
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function requirePositiveDecimal(name: string, value: string): string {
  const normalized = value.trim();
  if (!APP_ID_RE.test(normalized) || normalized === "0") {
    throw new Error(`${name} must be a positive decimal string`);
  }
  return normalized;
}

function requireText(name: string, value: string): string {
  const normalized = value.trim();
  if (!normalized) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return normalized;
}

export function createSignedGithubAppJwt({
  appId,
  privateKey,
  nowMs = Date.now(),
  ttlSeconds = DEFAULT_JWT_TTL_SECONDS,
  clockSkewSeconds = DEFAULT_CLOCK_SKEW_SECONDS,
}: SignedJwtOptions): SignedJwtResult {
  const now = Math.floor(Math.max(0, nowMs) / 1000);
  const issuedAt = now - clockSkewSeconds;
  const expiresAt = issuedAt + ttlSeconds;

  if (!Number.isInteger(ttlSeconds) || ttlSeconds <= 0 || ttlSeconds > 600) {
    throw new Error("ttlSeconds must be 1..600");
  }
  if (!Number.isInteger(clockSkewSeconds) || clockSkewSeconds < 0) {
    throw new Error("clockSkewSeconds must be a non-negative integer");
  }
  if (issuedAt < 0 || expiresAt <= issuedAt) {
    throw new Error("clock values would be invalid");
  }

  const normalizedAppId = requirePositiveDecimal("appId", appId);
  const normalizedPrivateKey = requireText("privateKey", privateKey);

  const encodedHeader = base64Url('{"alg":"RS256","typ":"JWT"}');
  const encodedPayload = base64Url(
    JSON.stringify({
      iat: issuedAt,
      exp: expiresAt,
      iss: normalizedAppId,
    }),
  );
  const signingInput = `${encodedHeader}.${encodedPayload}`;
  const signature = createSign("RSA-SHA256")
    .update(signingInput)
    .end()
    .sign(normalizedPrivateKey, "base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");

  return {
    token: `${signingInput}.${signature}`,
    issuedAt,
    expiresAt,
  };
}
