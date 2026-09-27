# Student Attendance System

A production-structured, full-stack QR attendance application.

```text
React Frontend
      │  HTTPS REST (HttpOnly JWT cookie)
      ▼
Python FastAPI Backend
      ├── Firebase Admin SDK → Firestore
      └── Google Sheets API  → Attendance Google Sheet
```

**Strict rule:** the frontend only initiates Google sign-in (Firebase Auth).
It never touches Firestore or Google Sheets directly. All roles, sessions,
QR validity, distance checks, duplicate prevention and attendance writes are
enforced inside FastAPI.

---

## 1. Project structure

```text
attendance_roaster/
├── frontend/attendance-system/     # React 19 + Vite
│   └── src/
│       ├── services/api.js         # centralized FastAPI client (credentials: "include")
│       ├── services/auth.js        # Firebase Google sign-in helpers
│       ├── utils/geolocation.js    # Browser Geolocation wrapper
│       ├── context/AuthContext.jsx # sign-in + backend role state
│       ├── pages/                  # Login / CRDashboard / StudentAttendance / ErrorPage
│       └── firebase.js             # Firebase Auth only (no Firestore SDK usage)
│
└── Backend/                        # Python FastAPI
    └── app/
        ├── main.py                 # app, CORS, logging, error shape, /health
        ├── core/                   # config.py, security.py (JWT), firebase.py
        ├── middleware/auth.py      # get_current_user / require_cr / require_student
        ├── routers/                # auth.py, attendance.py
        ├── services/               # auth/session/qr/student/sheets/attendance services
        ├── models/                 # Pydantic request/response models
        └── utils/location.py       # Haversine distance
```

---

## 2. Prerequisites

| Tool  | Version              |
| ----- | -------------------- |
| Node  | ≥ 20                 |
| Python| ≥ 3.10               |
| Google account | for Firebase project + Sheets |

---

## 3. Firebase setup

1. Open the [Firebase console](https://console.firebase.google.com) and use the
   existing project `attendance-roaster-7ce62` (the frontend `src/firebase.js`
   already points at it).
2. **Authentication → Sign-in method** → enable **Google**.
3. **Firestore Database → Create database** (Spark / free plan).
4. Download a **service account key**:
   Project settings → Service accounts → *Generate new private key*.
5. Save it as `Backend/credentials/serviceAccountKey.json`
   (already git-ignored). This one file serves both Firebase Admin and the
   Google Sheets API.

### admin_list (CR authorization)

Create a Firestore collection named `admin_list`. Each CR is one document:

```text
admin_list/
  doc-1 { email: "cr@example.com" }
  doc-2 { email: "another.cr@example.com" }
```

Emails are matched trimmed + lowercased. Any signed-in user whose Google
email is present becomes `role = "cr"`, everyone else `role = "student"`.
The frontend never decides roles.

> Recommended Firestore rules (backend uses Admin SDK which bypasses rules —
> deny client access entirely):
> `rules_version = '2'; service cloud.firestore { match /databases/**/documents { match /{document=**} { allow read, write: if false; } } }`

---

## 4. Google Sheets setup

In one spreadsheet (shared with the **service-account email**, e.g.
`firebase-admin-sdk-...@attendance-roaster-7ce62.iam.gserviceaccount.com`,
as Editor):

| Worksheet   | Purpose | Columns |
| ----------- | ------- | ------- |
| `Sheet1`    | authoritative roster | `Enrollment-ID` \| `Name` (header row + one row per student) |
| `Attendance`| attendance log | `Timestamp` \| `Enrollment No` \| `Student Name` \| `Status` \| `Session-ID` |

The backend writes `Status = PRESENT` only after **all** checks pass
(JWT, QR token, session, 1-minute window, roster match, distance, duplicates).

---

## 5. Backend setup

```bash
cd Backend
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on Linux/Mac)
pip install -r requirements.txt
copy .env.example .env            # then edit values
uvicorn app.main:app --reload --port 8000
```

Check: `GET http://localhost:8000/health` →
`{"status":"ok","service":"attendance-api","firebase":"connected"}`

### Key `.env` values

| Variable | Meaning | Default |
| -------- | ------- | ------- |
| `APP_ENV` | `development` / `production` (cookie flags, OpenAPI exposure) | development |
| `JWT_SECRET_KEY` | application JWT signing secret — **change it** | — |
| `JWT_EXPIRE_MINUTES` | app JWT lifetime | 3 |
| `ATTENDANCE_RADIUS_METERS` | allowed distance from CR | 30 |
| `STUDENT_SESSION_MINUTES` | student completion window | 1 |
| `QR_TOKEN_LIFETIME_SECONDS` | QR rotation interval | 10 |
| `FRONTEND_URL` | comma-separated CORS origins | http://localhost:5173 |
| `PUBLIC_APP_URL` | base URL encoded inside QR images | same as FRONTEND_URL |
| `FIREBASE_CREDENTIALS_FILE` | path to service-account JSON | ./firebasecreds.json |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Sheets credentials (can be the same file) | ./firebasecreds.json |
| `GOOGLE_SHEET_ID` | spreadsheet ID (from its URL) | — |
| `GOOGLE_SHEET_NAME` / `GOOGLE_STUDENTS_SHEET_NAME` | worksheet names | Attendance / Sheet1 |

The server **starts without credentials** (dev-friendly); Firestore/Sheets
dependent APIs return 503 until configured.

---

## 6. Frontend setup

```bash
cd frontend\attendance-system
npm install
npm run dev        # http://localhost:5173
```

`frontend/attendance-system/.env`:

```text
VITE_API_URL=http://localhost:8000
```

> Always use the host name `localhost` (not `127.0.0.1`) for both ports —
> the HttpOnly auth cookie relies on same-site cookies during development.

---

## 7. API endpoints

| Endpoint | Auth | Purpose |
| -------- | ---- | ------- |
| `GET /health` | — | liveness + config status |
| `POST /authorize-user` | `Authorization: Bearer <firebase id token>` | verifies ID token, resolves role from `admin_list`, upserts `users`, issues 3-min JWT in HttpOnly cookie |
| `GET /me` | cookie JWT | current user info |
| `POST /logout` | — | clears cookie |
| `POST /start-attendance` | CR | creates the one shared active session (or joins it) — race-safe |
| `GET /active-session` | CR | rejoin: the single active session + current QR + live stats |
| `POST /qr/refresh` | CR | returns the session's current QR (rotates only when expired) |
| `POST /end-attendance` | CR | ends the shared session for ALL CRs, returns server-computed summary |
| `GET /sessions/{id}/stats` | CR | live present/total counts + recent arrivals (jar animation) |
| `GET /sessions/{id}/summary` | CR | final summary: absentees, counts, percentage |
| `GET /sessions/{id}` | JWT | session details (location redacted for non-owners) |
| `POST /attendance/verify-token` | JWT | student scanned QR → starts server-side 1-min window |
| `GET /student/me` | student JWT | is a backend-verified enrollment already saved for this Google account? |
| `POST /student/lookup` | student JWT | resolves last-3 enrollment digits against Sheet1 (server-side) |
| `POST /student/confirm` | student JWT | re-verifies and links the enrollment to the Firebase UID |
| `POST /attendance/check` | student JWT | full validation + Firestore + Google Sheet write (identity from the saved UID link) |

Errors use a consistent shape:

```json
{ "success": false, "error": { "code": "QR_EXPIRED", "message": "The QR code has expired." } }
```

Codes include: `UNAUTHORIZED`, `CR_ONLY`, `STUDENT_ONLY`, `QR_INVALID`,
`QR_EXPIRED`, `SESSION_NOT_ACTIVE`, `SESSION_NOT_FOUND`,
`ATTENDANCE_WINDOW_EXPIRED`, `STUDENT_NOT_FOUND`, `MULTIPLE_STUDENTS_MATCH`,
`ENROLLMENT_NOT_VERIFIED`, `OUTSIDE_LOCATION`, `ALREADY_MARKED`,
`SERVICE_UNAVAILABLE`.

---

## 8. How the security model works

- **Application JWT** — HS256, exactly 3 minutes, claims `sub`/`role`/`iat`/`exp`,
  delivered only via an `HttpOnly` cookie (never localStorage/URL/state).
  The frontend silently re-authorizes via the Firebase SDK when it expires.
- **QR tokens** — `secrets.token_urlsafe(24)` random; Firestore stores only the
  **SHA-256 hash**. 10-second rotation lifetime + server-timestamped
  1-minute completion window; ended sessions reject all their tokens.
- **Roles** — backend-only, from `admin_list`. Frontend role data is display
  information, never authorization.
- **One active session** — a `session_state/current` pointer document plus a
  Firestore transaction make it impossible for two CRs to create two active
  sessions; all CRs see the same session, QR and statistics.
- **Student identity** — students never type their name or full enrollment.
  The last-3-digit input is resolved server-side against `Sheet1` (ambiguous
  matches are rejected); the verified `enrollment_no` is stored on
  `users/{uid}` and reused on later scans. `/attendance/check` accepts no
  identity fields at all.
- **Location** — Haversine distance computed by the backend between stored CR
  position and submitted student position, compared to `ATTENDANCE_RADIUS_METERS`.
- **Duplicates** — deterministic document id `{session_id}__{ENROLLMENT}` in the
  `attendance` collection; a second submit returns 409.
- **Secrets** — `.env`, `credentials/`, service-account keys are git-ignored;
  errors returned to clients never expose stack traces or credentials.

---

## 9. Local end-to-end test

1. Put the service-account JSON at `Backend/firebasecreds.json`, fill `.env`, restart uvicorn.
2. Add your own Google email to `admin_list` in Firestore.
3. Create the Google Sheet (`Sheet1` roster + `Attendance` log), share with the
   service-account email, set `GOOGLE_SHEET_ID`.
4. Start frontend + backend, open `http://localhost:5173` as the **CR** →
   allow location → sign in → *Start Session Attendance* → QR appears, refreshes every 10 s.
   A second CR signing in is dropped straight into the same live session.
5. On a phone, open the QR link → sign in with a student Google account →
   enter the **last 3 digits** of the enrollment → confirm the shown name →
   submit within 1 minute → a `PRESENT` row lands in the **Attendance** sheet.
   On the next scan the student is recognized automatically.
6. As CR press *End Session* — further submissions are rejected and the final
   absentee list + present percentage (computed by the backend) is shown.

---

## 10. Deployment (free tier)

| Component | Target |
| --------- | ------ |
| Frontend  | Firebase Hosting (`npm run build` → deploy `dist/`) |
| Backend   | Render free web service (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`) |
| Database  | Firestore (Spark) · Auth: Firebase Authentication · Attendance: Google Sheets |

Production checklist:

- `APP_ENV=production` (enables `Secure` + `SameSite=None` cookies — required
  because the frontend origin differs from the Render origin).
- Strong random `JWT_SECRET_KEY`.
- `FRONTEND_URL=https://<your-firebase-app>.web.app`,
  `PUBLIC_APP_URL` same.
- Add `https://<your-firebase-app>.web.app` to Firebase Auth **Authorized domains**.
- Render build: `pip install -r requirements.txt`; start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- Provide credentials on Render via Environment Variables
  (`FIREBASE_CREDENTIALS_FILE` pointing at a file path you deploy, or use
  Render's secret files feature).
- Firestore rules: deny all client access (Admin SDK bypasses them anyway).

---

## 11. Example commands (copy/paste)

```bash
# Backend
cd Backend
pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend
cd frontend/attendance-system
npm install
npm run dev
```
