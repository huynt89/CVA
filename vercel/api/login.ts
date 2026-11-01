import { createHash, scryptSync, timingSafeEqual } from "node:crypto";
import { google as googleApis } from "googleapis";
import type { VercelRequest, VercelResponse } from "../lib/vercel-types";
import { createSessionToken, HttpError, requirePost, sendError } from "../lib/vercel-api";
import { googleServiceAccountCredentials } from "../lib/google-credentials";

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
const loginApiVersion = "2026-11-01.2";

function rejectLogin(reason: string): never {
  console.warn(`Sign-in rejected (${reason}).`);
  throw new HttpError(401, "Username, password, or account role is incorrect.");
}

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

function verifyPassword(password: string, encoded: string) {
  const parts = encoded.split("$");
  if (parts.length !== 3 || parts[0] !== "scrypt" || !parts[1] || !parts[2]) {
    return { valid: false, formatValid: false };
  }
  const [, salt, hash] = parts;
  try {
    const saltBytes = Buffer.from(salt, "base64");
    const expected = Buffer.from(hash, "base64");
    if (
      saltBytes.length !== 16
      || expected.length !== 64
      || saltBytes.toString("base64") !== salt
      || expected.toString("base64") !== hash
    ) {
      return { valid: false, formatValid: false };
    }
    const actual = scryptSync(password, saltBytes, expected.length, {
      N: 16_384,
      r: 8,
      p: 1,
      maxmem: 64 * 1024 * 1024,
    });
    return {
      valid: expected.length === actual.length && timingSafeEqual(expected, actual),
      formatValid: true,
    };
  } catch {
    return { valid: false, formatValid: true };
  }
}

function normalizeHeader(value: unknown) {
  return String(value ?? "")
    .normalize("NFKC")
    .replace(/[\u200B-\u200D\uFEFF]/g, "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

function normalizeAccountValue(value: unknown) {
  return String(value ?? "")
    .normalize("NFKC")
    .replace(/[\u200B-\u200D\uFEFF]/g, "")
    .trim()
    .toLowerCase();
}

async function findUser(identifier: string, requestedRole: string): Promise<UserRecord | undefined> {
  const spreadsheetId = process.env.GOOGLE_SHEET_ID?.trim();
  if (!spreadsheetId) throw new HttpError(503, "GOOGLE_SHEET_ID is not configured.");
  const credentials = googleServiceAccountCredentials();
  const auth = new googleApis.auth.JWT({
    email: credentials.client_email,
    key: credentials.private_key,
    scopes: ["https://www.googleapis.com/auth/spreadsheets.readonly"],
  });
  const sheets = googleApis.sheets({ version: "v4", auth });
  const response = await sheets.spreadsheets.values.get({
    spreadsheetId,
    range: "'Users'!A:AZ",
  });
  const values = response.data.values ?? [];
  if (values.length < 2) throw new HttpError(403, "This account is not authorized.");
  const headers = values[0].map(normalizeHeader);
  const columnIndex = (...names: string[]) =>
    headers.findIndex((header) => names.includes(header));
  const emailIndex = columnIndex("email", "emailaddress");
  const usernameIndex = columnIndex("username", "userid");
  const fullNameIndex = columnIndex("fullname", "name");
  const classNameIndex = columnIndex("classname", "class");
  const passwordHashIndex = columnIndex("passwordhash", "hashedpassword");
  const passwordIndex = columnIndex("password");
  const roleIndex = columnIndex("role", "accountrole");
  const activeIndex = columnIndex("isactive", "active");
  const statusIndex = columnIndex("status", "accountstatus");
  if ([emailIndex, roleIndex].some((index) => index < 0) || usernameIndex < 0) {
    throw new HttpError(503, "The Users worksheet must contain username, email, and role columns.");
  }
  if (passwordHashIndex < 0 && passwordIndex < 0) {
    throw new HttpError(503, "The Users worksheet must contain username, email, password, and role columns.");
  }

  const matchingRows = values.slice(1).filter((cells) => {
    const rowUsername = normalizeAccountValue(cells[usernameIndex]);
    const rowEmail = normalizeAccountValue(cells[emailIndex]);
    return rowUsername === identifier || rowEmail === identifier;
  });
  if (matchingRows.length > 1) {
    throw new HttpError(409, "More than one account matches this username. Contact your teacher.");
  }
  const row = matchingRows[0];
  if (!row) return undefined;
  const passwordHash = passwordHashIndex >= 0 ? String(row[passwordHashIndex] ?? "").trim() : "";
  const password = passwordIndex >= 0 ? String(row[passwordIndex] ?? "").trim() : "";
  const active = activeIndex >= 0 ? String(row[activeIndex] ?? "").trim() : "";
  const status = statusIndex >= 0 ? String(row[statusIndex] ?? "").trim() : "";
  return {
    email: String(row[emailIndex] ?? ""),
    username: String(row[usernameIndex] ?? ""),
    full_name: fullNameIndex >= 0 ? String(row[fullNameIndex] ?? "") : "",
    class_name: classNameIndex >= 0 ? String(row[classNameIndex] ?? "") : "",
    password_hash: passwordHash,
    password,
    role: normalizeAccountValue(row[roleIndex]),
    is_active: normalizeAccountValue(active || status || "true"),
  };
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method === "GET") {
    res.status(200).json({ service: "login", version: loginApiVersion });
    return;
  }
  if (!requirePost(req, res)) return;
  try {
    const body = req.body as Record<string, unknown> | undefined;
    const rawIdentifier = typeof body?.username === "string"
      ? body.username
      : typeof body?.email === "string"
        ? body.email
        : "";
    const identifier = normalizeAccountValue(rawIdentifier);
    const password = typeof body?.password === "string" ? body.password : "";
    const requestedRole = typeof body?.role === "string"
      ? normalizeAccountValue(body.role)
      : "";
    if (
      !/^[\p{L}\p{N}._@+-]{1,120}$/u.test(identifier)
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
    if (!user) rejectLogin("account_not_found");
    const role: "teacher" | "student" | null =
      user?.role === "teacher" || user?.role === "student" ? user.role : null;
    if (!role || role !== requestedRole) rejectLogin("account_role_mismatch");
    const email = normalizeAccountValue(user?.email);
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) rejectLogin("account_email_invalid");
    if (!["true", "1", "yes", "active"].includes(String(user.is_active ?? "").toLowerCase())) {
      rejectLogin("account_inactive");
    }
    const passwordChecks = [user.password_hash, user.password]
      .filter((candidate): candidate is string => Boolean(candidate))
      .map((candidate) => verifyPassword(password, candidate));
    if (!passwordChecks.some((check) => check.valid)) {
      rejectLogin(
        passwordChecks.some((check) => check.formatValid)
          ? "password_does_not_match_scrypt_hash"
          : "password_hash_invalid_format",
      );
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
