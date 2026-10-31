import { createHash } from "node:crypto";
import { google as googleApis } from "googleapis";
import type { VercelRequest, VercelResponse } from "../lib/vercel-types";
import {
  authenticate,
  enforceUsageLimit,
  HttpError,
  requirePost,
  sendError,
} from "../lib/vercel-api";
import { googleServiceAccountCredentials } from "../lib/google-credentials";

const allowedMimeTypes = new Set([
  "audio/webm",
  "audio/ogg",
  "audio/wav",
  "audio/mpeg",
  "audio/mp4",
  "video/webm",
  "video/mp4",
]);

function googleAuth() {
  const credentials = googleServiceAccountCredentials();
  return new googleApis.auth.JWT({
    email: credentials.client_email,
    key: credentials.private_key,
    scopes: ["https://www.googleapis.com/auth/drive"],
  });
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (!requirePost(req, res)) return;
  try {
    const user = await authenticate(req);
    await enforceUsageLimit(user.id);
    const body = req.body as Record<string, unknown> | undefined;
    const fileName = typeof body?.file_name === "string" ? body.file_name.trim() : "";
    const mimeType = typeof body?.mime_type === "string" ? body.mime_type.trim().toLowerCase() : "";
    const fileSize = Number(body?.file_size);
    const folderId = process.env.GOOGLE_DRIVE_FOLDER_ID?.trim();
    if (!fileName || fileName.length > 180 || /[\\/\r\n]/.test(fileName)) {
      throw new HttpError(400, "The media file name is invalid.");
    }
    if (!allowedMimeTypes.has(mimeType)) throw new HttpError(400, "This media type is not supported.");
    if (!Number.isSafeInteger(fileSize) || fileSize < 1 || fileSize > 18 * 1024 * 1024) {
      throw new HttpError(413, "Media files must be smaller than 18 MB.");
    }
    if (!folderId) throw new HttpError(503, "Google Drive folder is not configured.");

    const auth = googleAuth();
    const { token } = await auth.getAccessToken();
    if (!token) throw new HttpError(503, "Could not authenticate with Google Drive.");
    const metadata = {
      name: fileName,
      parents: [folderId],
      appProperties: {
        cvaOwner: createHash("sha256").update(user.id).digest("hex"),
      },
    };
    const response = await fetch(
      "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true&fields=id,name,webViewLink,appProperties",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json; charset=UTF-8",
          "X-Upload-Content-Type": mimeType,
          "X-Upload-Content-Length": String(fileSize),
        },
        body: JSON.stringify(metadata),
        signal: AbortSignal.timeout(20_000),
      },
    );
    const uploadUrl = response.headers.get("location");
    if (!response.ok || !uploadUrl) {
      console.error("Could not create a Google Drive resumable upload session. HTTP", response.status);
      throw new HttpError(502, "Could not prepare the Google Drive upload.");
    }
    res.status(200).json({ upload_url: uploadUrl, file_size: fileSize });
  } catch (error) {
    sendError(res, error, "Could not prepare the Google Drive upload.");
  }
}
