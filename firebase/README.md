# Firebase Functions configuration

Create the Firebase project and Functions source here. Store provider keys and
the Google service-account JSON in Firebase Functions secrets/Secret Manager;
never copy them into this directory, the source repository, or an application
package.

Set secrets interactively after selecting the Firebase project:

```powershell
firebase use <FIREBASE_PROJECT_ID>
firebase functions:secrets:set GEMINI_API_KEY
firebase functions:secrets:set GROQ_API_KEY
firebase functions:secrets:set GOOGLE_SERVICE_ACCOUNT_JSON
```

Paste each value only into the Firebase CLI prompt. Do not put secret values in
PowerShell command arguments, shell history, source files, or GitHub variables.
Restrict each deployed Function to authenticated Firebase users and enforce
student/teacher roles before forwarding requests to a provider. Application
packages must not contain provider credentials or a reusable proxy secret.

The application still calls AI providers and Google APIs directly. This folder
is a deployment boundary and setup location, not yet an active proxy. Complete
the authenticated proxy and Firebase Authentication migration before deploying
the application with Firebase project configuration.
