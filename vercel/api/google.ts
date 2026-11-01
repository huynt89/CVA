import { randomBytes, scryptSync } from "node:crypto";
import { google as googleApis } from "googleapis";
import type { VercelRequest, VercelResponse } from "../lib/vercel-types";
import {
  authenticate,
  enforceUsageLimit,
  HttpError,
  requirePost,
  requireTeacher,
  sendError,
  type AuthenticatedUser,
} from "../lib/vercel-api";
import { googleServiceAccountCredentials } from "../lib/google-credentials";

const allowedSheets = new Set(["Users", "Lesson", "Practice"]);
const usersSheetName = "Users";
const practiceHeaders = [
  "submitted_at", "student_id", "student_email", "student_name", "username", "class_name",
  "lesson_id", "lesson_title", "task_1_score", "task_2_score", "task_3_score", "task_4_score",
  "overall_score", "feedback", "answer_1", "answer_2", "transcript_3", "audio_url", "video_url", "status",
];

function googleCredentials() {
  return googleServiceAccountCredentials();
}

function sheetsClient() {
  const credentials = googleCredentials();
  const auth = new googleApis.auth.JWT({
    email: credentials.client_email,
    key: credentials.private_key,
    scopes: ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
  });
  return googleApis.sheets({ version: "v4", auth });
}

function sheetId() {
  const value = process.env.GOOGLE_SHEET_ID?.trim();
  if (!value) throw new HttpError(503, "Google Sheets is not configured.");
  return value;
}

function validateSheet(value: unknown) {
  if (typeof value !== "string" || !allowedSheets.has(value)) {
    throw new HttpError(400, "The requested worksheet is not supported.");
  }
  return value;
}

function sanitizeRows(value: unknown, maxRows: number) {
  if (!Array.isArray(value) || value.length > maxRows) {
    throw new HttpError(400, "The sheet payload is invalid or too large.");
  }
  return value.map((row) => {
    if (!Array.isArray(row) || row.length > 40) throw new HttpError(400, "The sheet row is invalid.");
    return row.map((cell) => {
      if (cell === null || cell === undefined) return "";
      if (!["string", "number", "boolean"].includes(typeof cell)) {
        throw new HttpError(400, "The sheet contains an invalid cell.");
      }
      const text = String(cell);
      if (text.length > 10_000) throw new HttpError(400, "A sheet cell is too long.");
      return text;
    });
  });
}

async function readSheetRows(name: string) {
  const response = await sheetsClient().spreadsheets.values.get({
    spreadsheetId: sheetId(),
    range: `'${name}'!A:AZ`,
  });
  const values = response.data.values ?? [];
  if (!values.length) return { headers: [], rows: [] as Record<string, string>[] };
  const headers = values[0].map((cell) => String(cell ?? "").trim());
  const rows = values.slice(1)
    .filter((row) => row.some((cell) => String(cell ?? "").trim()))
    .map((row) => Object.fromEntries(headers
      .map((header, index) => [header, String(row[index] ?? "")])
      .filter(([header]) => Boolean(header))));
  return { headers, rows };
}

async function replaceSheetRows(name: string, value: unknown) {
  const values = sanitizeRows(value, 10_001);
  if (!values.length) throw new HttpError(400, "At least the header row is required.");
  const sheets = sheetsClient();
  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId(),
    range: `'${name}'!A1`,
    valueInputOption: "RAW",
    requestBody: { majorDimension: "ROWS", values },
  });
  await sheets.spreadsheets.values.clear({
    spreadsheetId: sheetId(),
    range: `'${name}'!A${values.length + 1}:AZ`,
  });
  return values.length - 1;
}

function normalizeHeader(value: unknown) {
  return String(value ?? "").normalize("NFKC").trim().toLowerCase().replace(/[^a-z0-9]/g, "");
}

function normalizeAccountValue(value: unknown) {
  return String(value ?? "").normalize("NFKC").trim().toLowerCase();
}

function createPasswordHash(password: string) {
  const salt = randomBytes(16);
  const digest = scryptSync(password, salt, 64, {
    N: 16_384,
    r: 8,
    p: 1,
    maxmem: 64 * 1024 * 1024,
  });
  return `scrypt$${salt.toString("base64")}$${digest.toString("base64")}`;
}

async function updateTeacherProfile(user: AuthenticatedUser, body: Record<string, unknown>) {
  requireTeacher(user);
  const fullName = typeof body.full_name === "string" ? body.full_name.trim() : "";
  const username = typeof body.username === "string" ? body.username.trim() : "";
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const password = typeof body.password === "string" ? body.password : "";
  if (
    !fullName || fullName.length > 200
    || !/^[\p{L}\p{N}._-]{1,120}$/u.test(username)
    || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
    || password.length > 256
  ) {
    throw new HttpError(400, "Enter a valid name, username, email, and password.");
  }

  const sheets = sheetsClient();
  const response = await sheets.spreadsheets.values.get({
    spreadsheetId: sheetId(),
    range: `'${usersSheetName}'!A:AZ`,
  });
  const values = response.data.values ?? [];
  if (values.length < 2) throw new HttpError(404, "The teacher account was not found in the Users worksheet.");
  const headers = values[0].map(normalizeHeader);
  const columnIndex = (...names: string[]) => headers.findIndex((header) => names.includes(header));
  const nameIndex = columnIndex("fullname", "name");
  const usernameIndex = columnIndex("username", "userid");
  const emailIndex = columnIndex("email", "emailaddress");
  const roleIndex = columnIndex("role", "accountrole");
  const passwordHashIndex = columnIndex("passwordhash", "hashedpassword");
  const passwordIndex = columnIndex("password");
  if ([nameIndex, usernameIndex, emailIndex, roleIndex].some((index) => index < 0)) {
    throw new HttpError(503, "The Users worksheet must contain full_name, username, email, and role columns.");
  }
  if (password && passwordHashIndex < 0 && passwordIndex < 0) {
    throw new HttpError(503, "The Users worksheet has no password column.");
  }

  const matchingRows = values.slice(1).map((row, index) => ({ row, rowNumber: index + 2 }))
    .filter(({ row }) => normalizeAccountValue(row[emailIndex]) === user.email.toLowerCase());
  if (matchingRows.length !== 1) {
    throw new HttpError(404, "The teacher account could not be uniquely found in the Users worksheet.");
  }
  const { row, rowNumber } = matchingRows[0];
  if (normalizeAccountValue(row[roleIndex]) !== "teacher" || user.role !== "teacher") {
    throw new HttpError(403, "Teacher access is required.");
  }
  const usernameKey = normalizeAccountValue(username);
  const emailKey = normalizeAccountValue(email);
  const duplicate = values.slice(1).some((candidate, index) => index + 2 !== rowNumber
    && (
      normalizeAccountValue(candidate[usernameIndex]) === usernameKey
      || normalizeAccountValue(candidate[emailIndex]) === emailKey
    ));
  if (duplicate) throw new HttpError(409, "That username or email address is already in use.");

  const updatedRow = [...row];
  while (updatedRow.length < headers.length) updatedRow.push("");
  updatedRow[nameIndex] = fullName;
  updatedRow[usernameIndex] = username;
  updatedRow[emailIndex] = email;
  if (password) updatedRow[passwordHashIndex >= 0 ? passwordHashIndex : passwordIndex] = createPasswordHash(password);
  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId(),
    range: `'${usersSheetName}'!A${rowNumber}`,
    valueInputOption: "RAW",
    requestBody: { majorDimension: "ROWS", values: [updatedRow] },
  });
  return { updated: true };
}

async function upsertStudentPractice(user: AuthenticatedUser, body: Record<string, unknown>) {
  if (user.role !== "student") throw new HttpError(403, "Student access is required.");
  const headers = Array.isArray(body.headers) ? body.headers.map(String) : [];
  if (headers.length !== practiceHeaders.length || headers.some((value, index) => value !== practiceHeaders[index])) {
    throw new HttpError(400, "The Practice headers are invalid.");
  }
  const incomingRows = sanitizeRows(body.rows, 1000);
  const studentData = await readSheetRows(usersSheetName);
  const student = studentData.rows.find((row) => String(row.email ?? "").trim().toLowerCase() === user.email.toLowerCase());
  if (!student) throw new HttpError(403, "This account is not listed on the Users worksheet.");

  const practice = await readSheetRows("Practice");
  const outputHeaders = practice.headers.length ? [...practice.headers] : [...headers];
  for (const header of headers) if (!outputHeaders.includes(header)) outputHeaders.push(header);
  if (!outputHeaders.includes("student_email")) outputHeaders.push("student_email");
  const lessonIndex = outputHeaders.indexOf("lesson_id");
  const emailIndex = outputHeaders.indexOf("student_email");
  if (lessonIndex < 0) throw new HttpError(400, "The Practice worksheet has no lesson_id column.");

  const rows = practice.rows.map((row) => outputHeaders.map((header) => String(row[header] ?? "")));
  let updatedCount = 0;
  for (const row of incomingRows) {
    const record = Object.fromEntries(headers.map((header, index) => [header, row[index] ?? ""]));
    const lesson = String(record.lesson_id ?? "").trim();
    if (!lesson) continue;
    record.student_email = user.email;
    record.student_name = student.full_name ?? "";
    record.username = student.username ?? "";
    record.class_name = student.class_name ?? "";
    const values = outputHeaders.map((header) => String(record[header] ?? ""));
    const index = rows.findIndex((existing) =>
      String(existing[emailIndex] ?? "").trim().toLowerCase() === user.email.toLowerCase()
      && String(existing[lessonIndex] ?? "").trim() === lesson);
    if (index >= 0) rows[index] = values;
    else rows.push(values);
    updatedCount += 1;
  }
  if (!updatedCount) throw new HttpError(400, "No valid Practice records were provided.");
  await sheetsClient().spreadsheets.values.update({
    spreadsheetId: sheetId(),
    range: "'Practice'!A1",
    valueInputOption: "RAW",
    requestBody: { majorDimension: "ROWS", values: [outputHeaders, ...rows] },
  });
  return { updatedCount, totalRows: rows.length };
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (!requirePost(req, res)) return;
  try {
    const user = await authenticate(req);
    await enforceUsageLimit(user.id);
    const body = req.body as Record<string, unknown> | undefined;
    const operation = typeof body?.operation === "string" ? body.operation : "";
    if (operation === "read_sheet") {
      requireTeacher(user);
      res.status(200).json({ ok: true, ...(await readSheetRows(validateSheet(body?.sheet))) });
      return;
    }
    if (operation === "replace_sheet") {
      requireTeacher(user);
      const rowsWritten = await replaceSheetRows(validateSheet(body?.sheet), body?.values);
      res.status(200).json({ ok: true, rows_written: rowsWritten });
      return;
    }
    if (operation === "update_user_profile") {
      const result = await updateTeacherProfile(user, body ?? {});
      res.status(200).json({ ok: true, ...result });
      return;
    }
    if (operation === "upsert_practice") {
      const result = await upsertStudentPractice(user, body ?? {});
      res.status(200).json({ ok: true, ...result });
      return;
    }
    throw new HttpError(400, "The Google operation is not supported.");
  } catch (error) {
    sendError(res, error, "Google Drive or Sheets is temporarily unavailable.");
  }
}
