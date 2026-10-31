import type { VercelRequest, VercelResponse } from "../lib/vercel-types";
import { authenticate, requirePost, sendError } from "../lib/vercel-api";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (!requirePost(req, res)) return;
  try {
    const user = await authenticate(req);
    res.status(200).json({
      authenticated: true,
      uid: user.id,
      email: user.email,
      role: user.role,
    });
  } catch (error) {
    sendError(res, error, "Could not verify the Vercel session.");
  }
}
