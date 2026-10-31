import { createHash, scryptSync, timingSafeEqual } from "node:crypto";
import { google as googleApis } from "googleapis";
import type { VercelRequest, VercelResponse } from "../lib/vercel-types";
import { createSessionToken, HttpError, requirePost, sendError } from "../lib/vercel-api";

type UserRecord = {
  email?: string;
  username?: string;
  full_name?: string;
  class_name?: string;
  password?: string;
  password_hash?: string;
  role?: string;
  status?: string;
  is_active?: string;
};

const attempts = new Map<string, { windowStartedAt: number; count: number }>();

function checkLoginLimit(key: string) {
  const now = Date.now();
  const record = attempts.get(key);
  if (!record || now - record.windowStartedAt >= 15 * 60_000) {
    attempts.set(key, { windowStartedAt: now, count: 1 });
    return;
  }
  if (record.count >= 10) throw new HttpError(429, "Too many sign-in attempts. Try again later.");
  record.count += 1;
}

function validPassword(password: string, encoded: string) {
  const [algorithm, salt, hash] = encoded.split("$");
  if (algorithm !== "scrypt" || !salt || !hash) return false;
  try {
    const saltBytes = Buffer.from(salt, "base64");
    const expected = Buffer.from(hash, "base64");
    if (saltBytes.length !== 16 || expected.length !== 64) return false;
    const actual = scryptSync(password, saltBytes, expected.length, {
      N: 16_384,
      r: 8,
      p: 1,
      maxmem: 64 * 1024 * 1024,
    });
    return expected.length === actual.length && timingSafeEqual(expected, actual);
  } catch {
    return false;
  }
}

async function findUser(identifier: string, requestedRole: string): Promise<UserRecord | undefined> {
  const rawCredentials = process.env.GOOGLE_SERVICE_ACCOUNT_JSON?.trim();
  const spreadsheetId = process.env.GOOGLE_SHEET_ID?.trim();
  if (!rawCredentials || !spreadsheetId) {
    throw new HttpError(503, "The sign-in service is not configured.");
  }
  let credentials: { client_email?: string; private_key?: string };
  try {
    credentials = JSON.parse(rawCredentials) as typeof credentials;
  } catch {
    throw new HttpError(503, "The sign-in service is not configured.");
  }
  if (!credentials.client_email || !credentials.private_key) {
    throw new HttpError(503, "The sign-in service is not configured.");
  }
  const auth = new googleApis.auth.JWT({
    email: credentials.client_email,
    key: credentials.private_key,
    scopes: ["https://www.googleapis.com/auth/spreadsheets.readonly"],
  });
  const sheets = googleApis.sheets({ version: "v4", auth });
  const response = await sheets.spreadsheets.values.get({
    spreadsheetId,
    range: "'Student'!A:AZ",
  });
  const values = response.data.values ?? [];
  if (values.length < 2) throw new HttpError(403, "This account is not authorized.");
  const headers = values[0].map((value) => String(value ?? "").trim().toLowerCase());
  const emailIndex = headers.indexOf("email");
  const usernameIndex = headers.indexOf("username");
  const fullNameIndex = headers.indexOf("full_name");
  const classNameIndex = headers.indexOf("class_name");
  const passwordIndex = headers.indexOf("password_hash") >= 0
    ? headers.indexOf("password_hash")
    : headers.indexOf("password");
  const roleIndex = headers.indexOf("role");
  const activeIndex = headers.indexOf("is_active");
  const statusIndex = headers.indexOf("status");
  if ([emailIndex, passwordIndex, roleIndex].some((index) => index < 0) || usernameIndex < 0) {
    throw new HttpError(503, "The Student worksheet must contain username, email, password, and role columns.");
  }

  const matchingRows = values.slice(1).filter((cells) => {
    const rowRole = String(cells[roleIndex] ?? "").trim().toLowerCase();
    const rowUsername = String(cells[usernameIndex] ?? "").trim().toLowerCase();
    const rowEmail = String(cells[emailIndex] ?? "").trim().toLowerCase();
    return rowRole === requestedRole && (rowUsername === identifier || rowEmail === identifier);
  });
  if (matchingRows.length > 1) {
    throw new HttpError(409, "More than one account matches this username. Contact your teacher.");
  }
  const row = matchingRows[0];
  if (!row) return undefined;
  const status = statusIndex >= 0 ? String(row[statusIndex] ?? "") : "";
  return {
    email: String(row[emailIndex] ?? ""),
    username: String(row[usernameIndex] ?? ""),
    full_name: fullNameIndex >= 0 ? String(row[fullNameIndex] ?? "") : "",
    class_name: classNameIndex >= 0 ? String(row[classNameIndex] ?? "") : "",
    password_hash: String(row[passwordIndex] ?? ""),
    role: String(row[roleIndex] ?? "").trim().toLowerCase(),
    is_active: activeIndex >= 0
      ? String(row[activeIndex] ?? "").trim().toLowerCase()
      : statusIndex >= 0
        ? status.trim().toLowerCase()
        : "true",
  };
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (!requirePost(req, res)) return;
  try {
    const body = req.body as Record<string, unknown> | undefined;
    const identifier = typeof body?.username === "string"
      ? body.username.trim().toLowerCase()
      : typeof body?.email === "string"
        ? body.email.trim().toLowerCase()
        : "";
    const password = typeof body?.password === "string" ? body.password : "";
    const requestedRole = typeof body?.role === "string" ? body.role.trim().toLowerCase() : "";
    if (
      !/^[a-z0-9._@+-]{1,120}$/i.test(identifier)
      || !password
      || password.length > 256
      || !["student", "teacher"].includes(requestedRole)
    ) {
      throw new HttpError(400, "Enter a valid username, password, and account role.");
    }

    const forwarded = req.headers["x-forwarded-for"];
    const address = Array.isArray(forwarded) ? forwarded[0] : forwarded;
    checkLoginLimit(createHash("sha256").update(String(address ?? "unknown")).digest("hex"));
    const user = await findUser(identifier, requestedRole);
    const role: "teacher" | "student" | null =
      user?.role === "teacher" || user?.role === "student" ? user.role : null;
    const email = String(user?.email ?? "").trim().toLowerCase();
    if (
      !user
      || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
      || !["true", "1", "yes", "active"].includes(String(user.is_active ?? "").toLowerCase())
      || !role
      || role !== requestedRole
      || !validPassword(password, user.password_hash ?? "")
    ) {
      throw new HttpError(401, "Username, password, or account role is incorrect.");
    }

    const normalizedUser = { id: email, email, role };
    const accessToken = createSessionToken(normalizedUser);
    attempts.delete(createHash("sha256").update(String(address ?? "unknown")).digest("hex"));
    res.status(200).json({
      authenticated: true,
      uid: normalizedUser.id,
      email: normalizedUser.email,
      role: normalizedUser.role,
      username: user.username ?? "",
      full_name: user.full_name ?? "",
      class_name: user.class_name ?? "",
      access_token: accessToken,
      expires_in: 14_400,
    });
  } catch (error) {
    sendError(res, error, "Could not sign in at this time.");
  }
}
