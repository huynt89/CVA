import { HttpError } from "./vercel-api";

export type GoogleServiceAccountCredentials = {
  client_email?: string;
  private_key?: string;
};

export function googleServiceAccountCredentials(): GoogleServiceAccountCredentials {
  const encoded = process.env.GOOGLE_SERVICE_ACCOUNT_JSON_B64?.trim();
  const raw = process.env.GOOGLE_SERVICE_ACCOUNT_JSON?.trim();
  if (!encoded && !raw) {
    throw new HttpError(
      503,
      "GOOGLE_SERVICE_ACCOUNT_JSON_B64 or GOOGLE_SERVICE_ACCOUNT_JSON is not configured.",
    );
  }

  let json = raw ?? "";
  if (encoded) {
    const validBase64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
    if (!validBase64.test(encoded) || Buffer.from(encoded, "base64").toString("base64") !== encoded) {
      throw new HttpError(503, "GOOGLE_SERVICE_ACCOUNT_JSON_B64 is not valid base64.");
    }
    json = Buffer.from(encoded, "base64").toString("utf8");
  }

  let credentials: unknown;
  try {
    credentials = JSON.parse(json);
  } catch {
    throw new HttpError(
      503,
      "The Google service account environment variable must contain valid JSON.",
    );
  }
  if (
    typeof credentials !== "object"
    || credentials === null
    || !("client_email" in credentials)
    || typeof credentials.client_email !== "string"
    || !credentials.client_email
    || !("private_key" in credentials)
    || typeof credentials.private_key !== "string"
    || !credentials.private_key
  ) {
    throw new HttpError(
      503,
      "The Google service account JSON must include client_email and private_key.",
    );
  }
  return {
    client_email: credentials.client_email,
    private_key: credentials.private_key,
  };
}
