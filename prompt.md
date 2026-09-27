# Build a Full-Stack Student Attendance System

Build a production-structured web application with a strict separation between frontend and backend.

## 1. Core Architecture

The project must contain exactly two main application folders:

```text
attendance-system/
│
├── frontend/
│   └── React.js + Vite
│
└── backend/
    └── Python + FastAPI
```

### Mandatory architecture

```text
React Frontend
      │
      │ HTTPS REST API
      ▼
Python FastAPI Backend
      │
      ├── Firebase Admin SDK → Firestore
      │
      └── Google Sheets API → Attendance Google Sheet
```

### VERY IMPORTANT

The React frontend must NEVER directly communicate with Firestore.

The React frontend must NEVER directly write attendance data to Google Sheets.

All database operations and business logic must go through FastAPI.

Correct:

```text
Frontend → FastAPI → Firestore
Frontend → FastAPI → Google Sheets
```

Incorrect:

```text
Frontend → Firestore
Frontend → Google Sheets
```

---

# 2. Technologies

## Frontend

Use:

* React.js
* Vite
* JavaScript
* React Router if routing is needed
* Firebase Authentication SDK only for Google sign-in
* Browser Geolocation API
* QR code display library
* Clean responsive CSS/UI

The frontend should be responsible for:

* UI
* Google authentication initiation
* requesting browser location
* displaying QR codes
* countdown timers
* displaying session status
* collecting student information
* calling FastAPI APIs
* displaying success/error messages

The frontend must NOT contain security-sensitive business logic.

---

## Backend

Use:

* Python 3.x
* FastAPI
* Uvicorn
* Firebase Admin SDK
* Google Sheets API
* JWT library such as PyJWT
* Pydantic
* python-dotenv
* appropriate CORS configuration
* standard Python logging

The backend is responsible for:

* Firebase ID token verification
* application authentication
* JWT creation and verification
* role determination
* authorization
* attendance session creation
* QR token generation
* QR token expiration
* session validation
* student validation
* location/distance calculation
* duplicate attendance prevention
* Google Sheets writing
* Firestore access
* API validation
* error handling

---

# 3. Authentication Architecture

Use Firebase Authentication with Google.

The authentication flow must be:

```text
User opens frontend
        ↓
Frontend requests browser location
        ↓
Frontend displays Google Login
        ↓
User signs in with Google
        ↓
Firebase Authentication authenticates user
        ↓
Frontend obtains Firebase ID Token
        ↓
Frontend calls:
POST /authorize-user
        ↓
FastAPI verifies Firebase ID Token
        ↓
FastAPI determines user's role
        ↓
FastAPI creates application JWT
        ↓
JWT is stored in HttpOnly cookie
        ↓
Frontend receives authorized-user response
```

Do NOT implement a separate Google OAuth system in FastAPI.

Firebase Authentication handles Google authentication.

FastAPI verifies the Firebase ID token using Firebase Admin SDK.

---

# 4. Application JWT

After successful Firebase authentication, FastAPI must create its own JWT.

The JWT must:

* be signed using a secret stored in environment variables
* expire after exactly 3 minutes
* contain the authenticated user's relevant identity information
* contain the user's role
* contain Firebase UID
* use appropriate JWT claims such as `sub`, `role`, `iat`, and `exp`

Example conceptual payload:

```json
{
    "sub": "firebase-user-uid",
    "role": "cr",
    "iat": 1234567890,
    "exp": 1234568070
}
```

Do not hardcode the JWT secret.

Use:

```text
JWT_SECRET_KEY
```

from environment variables.

---

# 5. JWT Storage

The application JWT MUST be stored in an:

```text
HttpOnly
Secure
SameSite
```

cookie.

Do NOT store the application JWT in:

```text
localStorage
sessionStorage
React state
URL parameters
```

The frontend should not need to read the JWT directly.

The browser automatically sends the HttpOnly cookie with API requests.

Use HTTPS in production.

For local development, configure the cookie appropriately so the application works on localhost while still being production-safe.

---

# 6. /authorize-user API

Create:

```text
POST /authorize-user
```

This endpoint requires the Firebase ID token from the frontend.

Use:

```text
Authorization: Bearer <firebase_id_token>
```

FastAPI must:

1. Extract the Firebase ID token.
2. Verify it using Firebase Admin SDK.
3. Get the Firebase UID and email.
4. Check the Firebase Firestore `admin list` collection/document structure.
5. Determine whether the email belongs to a CR.
6. If email exists in the admin list → role = `cr`.
7. Otherwise → role = `student`.
8. Create the 3-minute application JWT.
9. Store JWT in an HttpOnly cookie.
10. Return a JSON response describing the authenticated user and role.

Example response:

```json
{
    "success": true,
    "user": {
        "uid": "...",
        "name": "...",
        "email": "...",
        "role": "cr"
    }
}
```

Do not return the JWT itself in the JSON response because it is stored in the HttpOnly cookie.

---

# 7. CR Identification

Create a Firestore collection called:

```text
admin_list
```

This collection is the source of truth for CR authorization.

The user's Google email must be matched against the email stored in this collection.

Example:

```text
admin_list/
    document1
        email: "cr@example.com"

    document2
        email: "anothercr@example.com"
```

Email matching should be normalized appropriately, such as trimming whitespace and using lowercase comparison.

If the authenticated user's email exists in `admin_list`, assign:

```text
role = "cr"
```

Otherwise:

```text
role = "student"
```

The frontend must NEVER decide whether a user is a CR.

The backend makes this decision.

---

# 8. First Website Load

When the website is opened:

### Step 1

Request browser location permission.

Use the Browser Geolocation API.

The browser should show the standard:

```text
Allow location access?
```

prompt.

### Step 2

Prompt the user to sign in with Google.

### Step 3

After Google login succeeds:

Get Firebase ID token.

### Step 4

Call:

```text
POST /authorize-user
```

with:

```text
Authorization: Bearer <Firebase ID Token>
```

### Step 5

FastAPI verifies the Firebase token and issues the application JWT.

### Step 6

Frontend receives the role.

If:

```text
role = cr
```

show CR dashboard.

If:

```text
role = student
```

show student interface.

---

# 9. CR Dashboard

Create a clean, modern, responsive CR dashboard.

Initially show:

```text
Student Attendance System

Welcome, <CR Name>

[ Start Session Attendance ]
```

When the CR clicks:

```text
Start Session Attendance
```

the frontend must:

1. Obtain the current browser location.
2. Get latitude and longitude.
3. Send them to:

```text
POST /start-attendance
```

The application JWT is automatically sent through the HttpOnly cookie.

Request body:

```json
{
    "latitude": 27.000000,
    "longitude": 78.000000
}
```

Do NOT trust a role sent by the frontend.

FastAPI must determine the authenticated user from the JWT.

---

# 10. /start-attendance

Create:

```text
POST /start-attendance
```

This is a protected CR-only endpoint.

FastAPI must:

1. Verify application JWT.
2. Verify the authenticated user's role is `cr`.
3. Validate latitude and longitude.
4. Create a unique attendance session ID.
5. Store the CR's latitude and longitude.
6. Store the configured attendance radius.
7. Store session creation timestamp.
8. Mark session as active.
9. Generate the initial short-lived QR token.
10. Return the session information.

Example conceptual Firestore session:

```text
sessions/{session_id}

{
    cr_uid: "...",
    latitude: 27.000000,
    longitude: 78.000000,
    radius_meters: 30,
    status: "active",
    started_at: "...",
    ended_at: null
}
```

The attendance radius must be configurable on the backend.

Use an environment variable such as:

```text
ATTENDANCE_RADIUS_METERS=30
```

Do not hardcode the radius in React.

---

# 11. Attendance Session

An attendance session remains active until the CR explicitly clicks:

```text
End Session
```

The session must not end merely because the QR changes.

Under the QR code, show:

```text
Session Status: Active

QR refreshes in: 7 seconds

[ End Session ]
```

---

# 12. QR Code System

The QR code must contain a short-lived random token.

Do NOT put the following inside the QR:

* JWT
* Firebase ID token
* student name
* enrollment number
* CR email
* sensitive information

The QR should contain a URL such as:

```text
https://your-domain.com/attendance?token=<random_token>
```

The token must be cryptographically random and unpredictable.

---

# 13. QR Token Lifetime

Each QR token must remain valid for approximately:

```text
10 seconds
```

After 10 seconds:

```text
old QR token → invalid
new QR token → generated
new QR displayed
```

The backend must be the authority for QR token validity.

Do NOT rely only on the frontend countdown.

The backend must reject expired QR tokens even if someone manually submits one.

Recommended structure:

```text
qr_tokens/{token_id}

{
    session_id: "...",
    token_hash: "...",
    created_at: "...",
    expires_at: "...",
    active: true
}
```

Prefer storing a secure hash of the token rather than the raw token where practical.

---

# 14. QR Refresh

While the CR session is active:

```text
Generate QR
    ↓
10-second countdown
    ↓
Request/generate new QR
    ↓
Display new QR
    ↓
Repeat
```

The QR must stop refreshing when:

```text
End Session
```

is clicked.

The backend must also reject tokens belonging to ended sessions.

---

# 15. End Session

Create:

```text
POST /end-attendance
```

This is a CR-only protected API.

Request:

```json
{
    "session_id": "..."
}
```

FastAPI must:

1. Verify JWT.
2. Verify role = CR.
3. Verify that the CR owns the session.
4. Verify session is currently active.
5. Mark session as ended.
6. Store `ended_at`.
7. Invalidate remaining QR tokens for that session.
8. Return success.

Example:

```json
{
    "success": true,
    "message": "Attendance session ended successfully."
}
```

The frontend should then replace the QR interface with:

```text
Attendance Session Ended
```

---

# 16. Student QR Flow

When a student scans the QR:

```text
QR
 ↓
https://domain.com/attendance?token=XYZ
```

React must read the token from the URL.

Do NOT immediately mark attendance.

The student page should display something like:

```text
Student Attendance

Attendance Session Found

This session is valid for 1 minute.

[ Continue ]
```

The student has a maximum completion window of:

```text
1 minute
```

The backend must enforce the time limit.

Do not rely only on a frontend countdown.

---

# 17. Student Authentication

The student must:

1. Allow location access.
2. Sign in with Google.
3. Obtain Firebase ID token.
4. Call:

```text
POST /authorize-user
```

5. Backend verifies Firebase identity.
6. Backend determines role.
7. Backend issues the 3-minute application JWT in HttpOnly cookie.

The student then sees a form:

```text
Name
[________________]

Enrollment No.
[________________]

[ Mark Attendance ]
```

---

# 18. Student Information

The student will enter:

```text
Name
Enrollment No
```

The authoritative student information is stored in a Google Sheet.

The backend must use the Google Sheets API to verify the submitted details.

The frontend must never have access to Google service-account credentials.

---

# 19. /attendance/check

Create:

```text
POST /attendance/check
```

Use the application JWT from the HttpOnly cookie for authentication.

Request body:

```json
{
    "session_token": "...",
    "name": "Student Name",
    "enrollment_no": "123456",
    "latitude": 27.000000,
    "longitude": 78.000000
}
```

The JWT must NOT be included in the body.

FastAPI obtains it from the HttpOnly cookie.

---

# 20. Backend Attendance Validation

When `/attendance/check` is called, the backend must perform ALL of these checks.

### Check 1 — JWT

Verify:

* JWT exists
* signature is valid
* JWT is not expired
* JWT belongs to an authenticated user

If invalid:

```text
401 Unauthorized
```

---

### Check 2 — QR token

Verify:

* token exists
* token is valid
* token is not expired
* token belongs to an active attendance session

If invalid:

```text
400 Invalid or expired attendance QR
```

---

### Check 3 — Session

Verify:

* session exists
* session status is `active`
* session has not ended
* session is within the allowed attendance window

If invalid:

```text
400 Attendance session is no longer active
```

---

### Check 4 — Student details

Validate submitted:

```text
Name
Enrollment No
```

against the authoritative Google Sheet.

Name matching must NOT be case-sensitive.

For example:

```text
Rahul Kumar
rahul kumar
RAHUL KUMAR
RaHuL KuMaR
```

must be treated as the same name after normalization.

Trim leading/trailing whitespace.

Enrollment number should also be normalized for whitespace, but should otherwise match the authoritative value.

If details do not match:

```text
400 Student information does not match our records
```

Do not mark attendance.

---

# 21. Location Validation

The CR's location is stored when:

```text
/start-attendance
```

is called.

The student's location comes from the browser when:

```text
/attendance/check
```

is called.

FastAPI calculates the geographical distance between:

```text
CR latitude/longitude
```

and:

```text
Student latitude/longitude
```

Use an appropriate geographic distance calculation such as the Haversine formula.

Example:

```text
CR:
lat = ...
lon = ...

Student:
lat = ...
lon = ...

distance = calculate_distance(...)
```

Then compare:

```text
distance <= ATTENDANCE_RADIUS_METERS
```

The radius must be configurable through backend configuration.

For example:

```text
ATTENDANCE_RADIUS_METERS=30
```

If outside the radius:

```text
400
You are outside the attendance location.
```

Do NOT trust a distance value calculated by the frontend.

The backend must calculate the final distance.

---

# 22. Location Permission

Location is mandatory.

If the browser denies location:

```text
Location access is required to mark attendance.
```

Do not allow attendance submission.

The frontend should clearly explain that location permission is required.

---

# 23. Duplicate Attendance

A student can only be marked present once per attendance session.

Before writing attendance:

```text
Check whether this student already has attendance for this session.
```

If already marked:

```text
Attendance already marked for this session.
```

Do not create another attendance record.

---

# 24. Google Sheets Attendance

Successful attendance must be written to a Google Spreadsheet.

Columns MUST be:

```text
Timestamp | Enrollment No | Student Name | Status | Session-ID
```

Example:

```text
27/09/2026 14:02:31 | MCA123 | Rahul Kumar | PRESENT | SESSION-ABC123
```

The backend must write:

```text
Status = PRESENT
```

only after ALL validation checks succeed.

Do not allow the frontend to submit:

```text
status: "PRESENT"
```

The backend decides the status.

---

# 25. Google Sheets Authentication

Use a Google service account for backend access to Google Sheets.

The spreadsheet must be shared with the service-account email.

Service-account credentials must:

* never be placed in React
* never be committed to Git
* never be returned by an API
* be loaded securely by the backend

Prefer environment variables or a secure credentials mechanism appropriate for deployment.

---

# 26. Firestore

Firestore should be used for application/session data.

Suggested collections:

```text
admin_list
users
sessions
qr_tokens
attendance
```

Suggested `users` document:

```json
{
    "uid": "...",
    "name": "...",
    "email": "...",
    "role": "student"
}
```

Suggested session document:

```json
{
    "session_id": "...",
    "cr_uid": "...",
    "latitude": 27.000000,
    "longitude": 78.000000,
    "radius_meters": 30,
    "status": "active",
    "started_at": "...",
    "ended_at": null
}
```

Suggested attendance document:

```json
{
    "session_id": "...",
    "student_uid": "...",
    "name": "...",
    "enrollment_no": "...",
    "status": "PRESENT",
    "marked_at": "...",
    "distance_meters": 12.4
}
```

Use a deterministic document ID or another reliable mechanism to prevent duplicate attendance for the same student/session.

---

# 27. Firestore Security

The React frontend must not directly access Firestore.

The final architecture should treat FastAPI as the application's backend access layer.

Use Firebase Admin SDK from FastAPI.

Do not put Firebase Admin credentials in the frontend.

During development, it is acceptable to use permissive Firestore rules temporarily, but the final application must not depend on insecure rules.

---

# 28. API Structure

Create clean FastAPI routers.

Suggested structure:

```text
backend/
│
├── app/
│   ├── main.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── security.py
│   │   └── firebase.py
│   │
│   ├── middleware/
│   │   └── auth.py
│   │
│   ├── routers/
│   │   ├── auth.py
│   │   ├── attendance.py
│   │   └── users.py
│   │
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── session_service.py
│   │   ├── qr_service.py
│   │   ├── student_service.py
│   │   └── sheets_service.py
│   │
│   ├── models/
│   │   ├── auth.py
│   │   ├── attendance.py
│   │   └── session.py
│   │
│   └── utils/
│       └── location.py
│
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

Keep business logic out of route handlers wherever practical.

---

# 29. Required API Endpoints

Implement at minimum:

```text
POST /authorize-user

POST /start-attendance

POST /end-attendance

POST /attendance/check

GET /health
```

Optionally create:

```text
GET /me
GET /sessions/{session_id}
POST /qr/refresh
```

only if actually useful.

Do not unnecessarily create APIs.

---

# 30. Authentication Dependencies

Create reusable FastAPI dependencies such as:

```text
get_current_user()
require_cr()
require_student()
```

For example:

```text
get_current_user()
        ↓
verify application JWT
        ↓
return authenticated user
```

Then:

```text
require_cr()
        ↓
get_current_user()
        ↓
check role == "cr"
```

Do not repeat JWT verification code in every endpoint.

---

# 31. Frontend API Layer

Do not put raw `fetch()` calls everywhere.

Create a centralized API service such as:

```text
frontend/src/services/api.js
```

All communication with FastAPI should go through this layer.

Configure:

```javascript
credentials: "include"
```

so the browser sends the HttpOnly authentication cookie.

The Firebase ID token is only needed during the authorization process.

---

# 32. Frontend Pages

Create clean pages/components such as:

```text
Login
CRDashboard
StudentAttendance
Loading
Error
```

Possible routes:

```text
/
 /attendance
 /cr
```

The frontend should determine what UI to show based on the backend authorization response, but security must always be enforced by FastAPI.

---

# 33. CR UI

Create a polished but simple interface.

Initial state:

```text
Student Attendance System

Welcome, CR Name

[ Start Session Attendance ]
```

Loading state:

```text
Starting Attendance Session...

████████░░░░░░░░░░ 60%
```

Active state:

```text
Attendance Session Active

┌─────────────────┐
│                 │
│       QR        │
│                 │
└─────────────────┘

QR refreshes in: 7 seconds

[ End Session ]
```

Ended state:

```text
Attendance Session Ended

[ Start New Session ]
```

---

# 34. Student UI

Create a clean student page:

```text
Student Attendance

Session detected.

Time remaining: 00:47

Name
[________________________]

Enrollment No.
[________________________]

[ Mark Attendance ]
```

During verification:

```text
Verifying attendance...

✓ Identity
✓ Session
✓ Location
✓ Student record
```

On success:

```text
✓ Attendance Marked Successfully

Status: PRESENT
```

On failure, show a clear reason.

Examples:

```text
QR code expired.
```

```text
Attendance session has ended.
```

```text
Student details do not match our records.
```

```text
You are outside the attendance location.
```

```text
Location permission is required.
```

```text
Attendance has already been marked.
```

---

# 35. One-Minute Student Session

The student attendance page should have a one-minute completion window.

The backend must enforce this.

The frontend should display a countdown.

For example:

```text
Time remaining: 00:59
```

When the one-minute window expires:

```text
Attendance window expired.
Please scan the current QR code again.
```

Do not rely only on JavaScript for expiration.

The backend must verify the timestamp/session validity.

---

# 36. Error Handling

Use appropriate HTTP status codes.

Examples:

```text
200 OK
201 Created
400 Bad Request
401 Unauthorized
403 Forbidden
404 Not Found
409 Conflict
422 Validation Error
500 Internal Server Error
```

Return consistent JSON structures.

Example:

```json
{
    "success": false,
    "error": {
        "code": "QR_EXPIRED",
        "message": "The QR code has expired."
    }
}
```

Do not expose internal exceptions, stack traces, Firebase credentials, or service-account information to the frontend.

Log detailed errors only on the backend.

---

# 37. Security Requirements

Follow these strictly:

1. Never trust frontend role information.
2. Never trust frontend attendance status.
3. Never trust frontend distance calculations.
4. Never trust frontend session validity.
5. Never put JWT in URL.
6. Never put Firebase Admin credentials in frontend.
7. Never put Google service-account credentials in frontend.
8. Never store application JWT in localStorage.
9. Use HttpOnly cookies for application JWT.
10. Validate all request bodies using Pydantic.
11. Verify Firebase ID tokens on the backend.
12. Verify application JWT on protected APIs.
13. Enforce CR permissions on CR endpoints.
14. Enforce student permissions where necessary.
15. Prevent duplicate attendance.
16. QR tokens must expire server-side.
17. Ended sessions must reject attendance.
18. One-minute attendance windows must be enforced server-side.
19. Attendance radius must be enforced server-side.
20. Never allow the frontend to directly modify Firestore.
21. Never allow the frontend to directly modify Google Sheets.
22. Keep secrets in environment variables.
23. Add `.env` and credential files to `.gitignore`.

---

# 38. Environment Configuration

Create:

```text
backend/.env.example
```

with variables such as:

```text
APP_ENV=development

JWT_SECRET_KEY=CHANGE_ME
JWT_EXPIRE_MINUTES=3

ATTENDANCE_RADIUS_METERS=30
STUDENT_SESSION_MINUTES=1
QR_TOKEN_LIFETIME_SECONDS=10

FRONTEND_URL=http://localhost:5173

GOOGLE_SHEET_ID=CHANGE_ME
GOOGLE_SHEET_NAME=Attendance
```

Do not commit actual secrets.

Also configure Firebase Admin credentials appropriately.

---

# 39. CORS

Configure FastAPI CORS to allow the React frontend.

For local development:

```text
http://localhost:5173
```

Do not use:

```text
allow_origins=["*"]
```

in the final production configuration.

Use an environment variable for allowed frontend origins.

Because HttpOnly cookies are used, configure:

```text
allow_credentials=True
```

---

# 40. Logging

Add backend logging for important events such as:

```text
User authorized
CR started session
Session ended
QR generated
Attendance validation attempted
Attendance successful
Attendance rejected
```

Never log:

* passwords
* Firebase service-account private keys
* Google service-account private keys
* JWT secrets
* raw authentication tokens

---

# 41. Time Handling

Use UTC internally for timestamps.

Convert timestamps for display on the frontend if necessary.

Use timezone-aware datetime objects.

Do not rely on the client's local clock for security decisions.

The backend/server timestamp is authoritative.

---

# 42. Location Accuracy

When requesting browser location, use appropriate geolocation settings such as high accuracy where practical.

The frontend should handle:

* permission denied
* position unavailable
* timeout
* unsupported browser

The backend should validate that latitude/longitude values are valid geographic coordinates.

---

# 43. Do Not Overengineer

This is the first version.

Do NOT add unnecessary:

* Redis
* Celery
* WebSockets
* Docker unless necessary
* Kubernetes
* microservices
* paid cloud services
* Firebase Cloud Functions
* Node.js backend

The backend must remain:

```text
Python + FastAPI
```

The frontend must remain:

```text
React + Vite
```

Firestore and Google Sheets should be accessed only from FastAPI.

---

# 44. Free-Tier Deployment Goal

The application should be designed to run using free/no-cost resources where possible.

Target deployment:

```text
Frontend → Firebase Hosting
Backend → Render Free Web Service
Database → Firebase Firestore Spark plan
Authentication → Firebase Authentication
Attendance → Google Sheets
```

Do not introduce paid services without explaining why they are necessary.

The application should also work completely locally before deployment.

---

# 45. Development Order

Do NOT attempt to generate the entire application blindly in one step.

Build it incrementally in this order:

### Phase 1

Create project structure.

### Phase 2

Create FastAPI backend.

Implement:

```text
GET /health
```

and confirm the backend works.

### Phase 3

Configure Firebase Admin SDK.

### Phase 4

Connect React Google Authentication to FastAPI.

Implement:

```text
POST /authorize-user
```

and verify Firebase ID tokens.

### Phase 5

Implement application JWT:

```text
3 minutes
HttpOnly cookie
```

### Phase 6

Implement CR/student role detection using:

```text
admin_list
```

### Phase 7

Build CR dashboard.

### Phase 8

Implement:

```text
POST /start-attendance
POST /end-attendance
```

### Phase 9

Implement QR generation and 10-second rotation.

### Phase 10

Build student attendance page.

### Phase 11

Implement student authentication.

### Phase 12

Implement:

```text
POST /attendance/check
```

with:

* JWT validation
* QR validation
* session validation
* one-minute validation
* Google Sheet student verification
* location validation
* duplicate prevention

### Phase 13

Implement Google Sheets attendance writing.

### Phase 14

Implement final security hardening.

### Phase 15

Prepare deployment configuration.

---

# 46. Important Development Rule

After completing each phase:

1. Run the application.
2. Test the feature.
3. Fix errors.
4. Only then proceed to the next phase.

Do not leave broken placeholder code throughout the project.

If a dependency/API has changed, use the current supported API rather than deprecated methods.

---

# 47. README

Create a detailed README explaining:

* project architecture
* frontend setup
* backend setup
* Firebase setup
* Google Authentication setup
* Firestore setup
* admin_list setup
* Google Sheets API setup
* service-account setup
* environment variables
* local development
* API endpoints
* deployment
* security considerations

Also include example commands.

Frontend:

```bash
npm install
npm run dev
```

Backend:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

---

# 48. Final Expected Flow

The completed application must behave as follows.

## CR

```text
Open website
    ↓
Allow location
    ↓
Google Login
    ↓
Firebase authentication
    ↓
POST /authorize-user
    ↓
FastAPI verifies Firebase token
    ↓
Email checked against admin_list
    ↓
role = CR
    ↓
3-minute JWT issued in HttpOnly cookie
    ↓
CR Dashboard
    ↓
Start Session Attendance
    ↓
Browser gets CR location
    ↓
POST /start-attendance
    ↓
FastAPI creates session
    ↓
QR appears
    ↓
QR changes every 10 seconds
    ↓
CR can End Session
```

## Student

```text
Scan QR
    ↓
Attendance page opens
    ↓
1-minute attendance window begins
    ↓
Allow location
    ↓
Google Login
    ↓
Firebase authentication
    ↓
POST /authorize-user
    ↓
FastAPI verifies Firebase token
    ↓
role = student
    ↓
3-minute JWT issued in HttpOnly cookie
    ↓
Enter Name + Enrollment No
    ↓
Browser obtains location
    ↓
POST /attendance/check
    ↓
FastAPI validates:
    ├── JWT
    ├── QR token
    ├── QR expiration
    ├── session
    ├── session expiration/window
    ├── student identity
    ├── name
    ├── enrollment number
    ├── CR location
    ├── student location
    ├── distance
    └── duplicate attendance
    ↓
All checks pass
    ↓
Google Sheet updated
    ↓
PRESENT
```

---

# 49. Final Instruction to Qoder

Before writing code, inspect the existing project and identify what has already been created.

We currently have an existing React/Vite frontend and Firebase project with Google Authentication already configured.

Do not throw away useful existing Firebase configuration unnecessarily.

However, refactor the application so that:

```text
React → FastAPI → Firebase/Firestore
```

is the final architecture.

Firebase Authentication may still be initiated from React because Google sign-in is a client authentication flow, but all authorization, roles, application JWT generation, Firestore access, attendance logic, Google Sheets access, session validation, QR validation, and security decisions must happen in FastAPI.

Do not put Firestore calls into React.

Do not put Google Sheets calls into React.

Do not put application secrets into React.

Do not generate a simplified demo. Build the foundation as a real full-stack application with clean separation of concerns, reusable services, validation, authentication, authorization, and secure API design.

Start with Phase 1 and Phase 2 first, verify that the FastAPI backend runs correctly, and then continue phase-by-phase.
