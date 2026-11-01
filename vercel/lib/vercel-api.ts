import { createHmac, timingSafeEqual } from "node:crypto";
import type { VercelRequest, VercelResponse } from "./vercel-types";

export type AppRole = "student" | "teacher";
export type AuthenticatedUser = {
  id: string;
  email: string;
  role: AppRole;
  is_headteacher?: boolean;
};

export class HttpError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const usage = new Map<string, { minute: number; minuteCount: number; day: string; dayCount: number }>();
const maxRequestsPerMinute = 20;
const maxRequestsPerDay = 100;

export async function enforceUsageLimit(userId: string) {
  const now = Date.now();
  const minute = Math.floor(now / 60_000);
  const day = new Date(now).toISOString().slice(0, 10);
  const previous = usage.get(userId) ?? { minute, minuteCount: 0, day, dayCount: 0 };
  const minuteCount = previous.minute === minute ? previous.minuteCount : 0;
  const dayCount = previous.day === day ? previous.dayCount : 0;
  if (minuteCount >= maxRequestsPerMinute || dayCount >= maxRequestsPerDay) {
    throw new HttpError(429, "Request limit reached. Try again later.");
  }
  usage.set(userId, {
    minute,
    minuteCount: minuteCount + 1,
    day,
    dayCount: dayCount + 1,
  });
}

export async function authenticate(req: VercelRequest): Promise<AuthenticatedUser> {
  const rawAuthorization = req.headers.authorization;
  const authorization = Array.isArray(rawAuthorization) ? rawAuthorization[0] ?? "" : rawAuthorization ?? "";
  const token = /^Bearer\s+(.+)$/i.exec(authorization)?.[1];
  if (!token) throw new HttpError(401, "A Vercel session token is required.");
  return verifySessionToken(token);
}

function signingSecret() {
  const secret = process.env.APP_SESSION_SECRET?.trim();
  if (!secret || secret.length < 32) {
    throw new HttpError(503, "The application session signing secret is not configured.");
  }
  return secret;
}

function base64Url(value: string | Buffer) {
  return Buffer.from(value).toString("base64url");
}

export function createSessionToken(user: AuthenticatedUser) {
  const now = Math.floor(Date.now() / 1000);
  const header = base64Url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const payload = base64Url(JSON.stringify({
    sub: user.id,
    email: user.email,
    role: user.role,
    is_headteacher: user.is_headteacher === true,
    iat: now,
    exp: now + 4 * 60 * 60,
  }));
  const unsigned = `${header}.${payload}`;
  const signature = createHmac("sha256", signingSecret()).update(unsigned).digest("base64url");
  return `${unsigned}.${signature}`;
}

function verifySessionToken(token: string): AuthenticatedUser {
  const parts = token.split(".");
  if (parts.length !== 3) throw new HttpError(401, "The Vercel session token is invalid.");
  const unsigned = `${parts[0]}.${parts[1]}`;
  const expected = createHmac("sha256", signingSecret()).update(unsigned).digest();
  let actual: Buffer;
  try {
    actual = Buffer.from(parts[2], "base64url");
  } catch {
    throw new HttpError(401, "The Vercel session token is invalid.");
  }
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    throw new HttpError(401, "The Vercel session token is invalid.");
  }

  try {
    const header = JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8")) as { alg?: string };
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")) as {
      sub?: string;
      email?: string;
      role?: string;
      is_headteacher?: boolean;
      exp?: number;
    };
    if (
      header.alg !== "HS256"
      || !payload.sub
      || !payload.email
      || !Number.isFinite(payload.exp)
      || Number(payload.exp) <= Math.floor(Date.now() / 1000)
      || (payload.role !== "teacher" && payload.role !== "student")
      || (payload.is_headteacher === true && payload.role !== "teacher")
    ) {
      throw new HttpError(401, "The Vercel session token is invalid or expired.");
    }
    return {
      id: payload.sub,
      email: payload.email,
      role: payload.role,
      is_headteacher: payload.is_headteacher === true,
    };
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(401, "The Vercel session token is invalid.");
  }
}

export function requirePost(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST is required." });
    return false;
  }
  return true;
}

export function sendError(res: VercelResponse, error: unknown, fallback: string) {
  if (error instanceof HttpError) {
    res.status(error.status).json({ error: error.message });
    return;
  }
  console.error(fallback);
  res.status(502).json({ error: fallback });
}

export function requireTeacher(user: AuthenticatedUser) {
  if (user.role !== "teacher") throw new HttpError(403, "Teacher access is required.");
}
