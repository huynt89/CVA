# Cloud Functions source

This is the source directory for authenticated Firebase Functions. Keep API
credentials in Firebase Functions secrets/Secret Manager; this directory must
contain source and non-secret configuration only.

Functions are not deployed from this workspace yet. Do not add a public proxy
until Firebase Authentication, role checks, rate limits, and the app's client
integration have been implemented and verified.
