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

function driveClient() {
  const credentials = googleServiceAccountCredentials();
  const auth = new googleApis.auth.JWT({
    email: credentials.client_email,
    key: credentials.private_key,
    scopes: ["https://www.googleapis.com/auth/drive"],
  });
  return googleApis.drive({ version: "v3", auth });
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (!requirePost(req, res)) return;
  try {
    const user = await authenticate(req);
    await enforceUsageLimit(user.id);
    const body = req.body as Record<string, unknown> | undefined;
    const fileId = typeof body?.file_id === "string" ? body.file_id.trim() : "";
    if (!fileId || fileId.length > 256) throw new HttpError(400, "The media file reference is invalid.");

    const drive = driveClient();
    const file = await drive.files.get({
      fileId,
      fields: "id,name,webViewLink,appProperties",
      supportsAllDrives: true,
    });
    const ownerKey = createHash("sha256").update(user.id).digest("hex");
    if (file.data.appProperties?.cvaOwner !== ownerKey) {
      throw new HttpError(403, "This media file does not belong to the signed-in account.");
    }
    await drive.permissions.create({
      fileId,
      requestBody: { type: "anyone", role: "reader" },
      supportsAllDrives: true,
    });
    res.status(200).json({
      file_id: fileId,
      drive_url: file.data.webViewLink ?? `https://drive.google.com/file/d/${fileId}/view`,
    });
  } catch (error) {
    sendError(res, error, "Could not finish saving the media to Google Drive.");
  }
}
