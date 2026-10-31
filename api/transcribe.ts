import { createHash } from "node:crypto";
import type { VercelRequest, VercelResponse } from "../lib/vercel-types";
import {
  authenticate,
  enforceUsageLimit,
  HttpError,
  requirePost,
  sendError,
} from "../lib/vercel-api";
import { google as googleApis } from "googleapis";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (!requirePost(req, res)) return;
  try {
    const user = await authenticate(req);
    await enforceUsageLimit(user.id);
    const body = req.body as Record<string, unknown> | undefined;
    const fileId = typeof body?.audio_file_id === "string" ? body.audio_file_id : "";
    const audioName = typeof body?.audio_name === "string" ? body.audio_name : "recording.webm";
    if (!fileId || fileId.length > 256 || /[\\/\r\n]/.test(audioName)) {
      throw new HttpError(400, "The audio file reference is invalid.");
    }
    const rawCredentials = process.env.GOOGLE_SERVICE_ACCOUNT_JSON?.trim();
    if (!rawCredentials) throw new HttpError(503, "Google Drive is not configured.");
    const credentials = JSON.parse(rawCredentials) as { client_email?: string; private_key?: string };
    if (!credentials.client_email || !credentials.private_key) {
      throw new HttpError(503, "Google Drive is not configured.");
    }
    const auth = new googleApis.auth.JWT({
      email: credentials.client_email,
      key: credentials.private_key,
      scopes: ["https://www.googleapis.com/auth/drive.readonly"],
    });
    const drive = googleApis.drive({ version: "v3", auth });
    const metadata = await drive.files.get({
      fileId,
      fields: "id,appProperties",
      supportsAllDrives: true,
    });
    const ownerKey = createHash("sha256").update(user.id).digest("hex");
    if (metadata.data.appProperties?.cvaOwner !== ownerKey) {
      throw new HttpError(403, "This audio file does not belong to the signed-in account.");
    }
    const fileResponse = await drive.files.get(
      { fileId, alt: "media", supportsAllDrives: true },
      { responseType: "arraybuffer" },
    );
    const audio = Buffer.from(fileResponse.data as ArrayBuffer);
    if (!audio.length || audio.length > 18 * 1024 * 1024) {
      throw new HttpError(413, "Audio files must be smaller than 18 MB.");
    }

    const apiKey = process.env.GROQ_API_KEY?.trim();
    if (!apiKey) throw new HttpError(503, "The transcription service is not configured.");
    const form = new FormData();
    form.append("file", new Blob([audio]), audioName.slice(0, 160));
    form.append("model", "whisper-large-v3-turbo");
    form.append("response_format", "json");
    const response = await fetch("https://api.groq.com/openai/v1/audio/transcriptions", {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}` },
      body: form,
      signal: AbortSignal.timeout(90_000),
    });
    if (!response.ok) {
      console.error("Groq transcription returned HTTP", response.status);
      throw new HttpError(502, "Audio transcription is temporarily unavailable.");
    }
    const result = await response.json() as { text?: unknown };
    if (typeof result.text !== "string") {
      throw new HttpError(502, "The transcription provider returned an invalid response.");
    }
    res.status(200).json({ transcript: result.text.slice(0, 5000) });
  } catch (error) {
    sendError(res, error, "Audio transcription is temporarily unavailable.");
  }
}
