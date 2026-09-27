You are working on my existing Attendance System project.

PROJECT STRUCTURE:

attendance-system/
├── frontend/        # React.js + Vite
└── Backend/         # Python + FastAPI

CURRENT ARCHITECTURE:

React Frontend
│ HTTPS REST APIs
▼
Python FastAPI Backend
│
├── Firebase Admin SDK → Firestore
└── Google Sheets API → Google Sheets

IMPORTANT:

* React must NOT directly access Firestore or Google Sheets.
* All authentication, authorization, attendance validation, session logic, roster lookup, duplicate prevention, and attendance status decisions must remain backend-controlled.
* Do NOT rewrite unrelated existing functionality.
* First inspect the current codebase and understand the existing implementation before making changes.
* Preserve the existing Firebase Authentication + application JWT architecture.
* Preserve the existing session ID, QR token, location validation, duplicate attendance prevention, and Google Sheet roster mechanisms unless a change below explicitly modifies their behavior.
* Make changes incrementally and keep the existing functionality working.

==================================================
REQUIRED CHANGES
================

1. RECORD DEVICE IP AND BROWSER INFORMATION

---

Add attendance audit information so that the system can identify potentially suspicious proxy attendance from the same device during the same session.

For every successfully recorded attendance, the backend should record:

* Firebase UID
* Enrollment number
* Student name
* Session ID
* Session name
* Timestamp
* Student latitude/longitude if already being stored
* Distance from CR if already being calculated
* Client IP address
* Browser/user-agent information
* A privacy-conscious server/browser-generated device identifier if practical and reliable

IMPORTANT:

* Obtain IP information on the BACKEND, not from a frontend field.
* Obtain browser/user-agent information from the HTTP request headers on the backend.
* Do NOT attempt to collect IMEI, MAC address, phone serial number, SIM number, or other hardware identifiers.
* Do NOT rely on IP/device information as the primary attendance authentication mechanism.
* Existing Google authentication, verified student identity, QR/session validation, and location validation remain the primary mechanisms.

Add backend logic to detect/flag suspicious cases such as:

* Multiple different students marking attendance from the same device identifier during the same session.
* Multiple different students using the same IP during the same session.

Do NOT automatically reject attendance merely because students share an IP/device. Instead, make this an audit/flagging mechanism unless the existing architecture already has a clearly defined rejection rule.

The CR UI should be able to see a simple warning such as:

"⚠ Multiple students marked from the same device"

or

"⚠ Multiple students detected from the same IP"

Do NOT expose raw IP addresses to students.

Preferably keep detailed technical audit information backend-side and only expose the minimum information required to the CR/Admin.

==================================================
2. CREATE A NEW GOOGLE SHEET FOR EVERY ATTENDANCE SESSION
---------------------------------------------------------

Whenever a NEW attendance session is successfully created, automatically create a new Google Sheets worksheet/tab for that session.

IMPORTANT:

* Do NOT create a new spreadsheet file unless the current Google Sheets architecture requires that.
* Prefer creating a new worksheet/tab inside the existing attendance spreadsheet.
* Each session must have its own attendance sheet/tab.
* Existing Sheet1 remains the authoritative master student roster.
* Existing Sheet2/current attendance mechanism must not be unnecessarily broken.

When a new session starts:

1. Read the complete student roster from Sheet1.
2. Create a new worksheet/tab for the newly created session.
3. Copy the following information from Sheet1:

   * Enrollment ID
   * Student Name
4. Add a Status column.

Example:

## Enrollment ID | Student Name | Status

2026001       | Student A   | ABSENT
2026002       | Student B   | ABSENT
2026003       | Student C   | ABSENT

Initially every student must be ABSENT.

When a student successfully marks attendance:

* Update that student's Status to PRESENT in the session-specific worksheet.
* Do not trust the frontend to decide the status.
* The backend must determine that attendance is valid and then update the sheet.

Formatting:

* PRESENT should be displayed in GREEN.
* ABSENT should be displayed in RED.
* Use readable formatting and preserve the enrollment number/name.
* Avoid creating duplicate session sheets if the same request is accidentally submitted twice.
* The worksheet must be associated with the backend session ID so the correct sheet is always updated.

IMPORTANT:

The existing Sheet1 must remain the master roster.

Do not allow students or the frontend to modify the roster directly.

Use the existing backend Google Sheets service and extend it rather than creating an unrelated second implementation.

If the Google Sheets API requires additional permissions or configuration, keep the implementation compatible with the existing service account setup.

==================================================
3. SESSION NAME ENTERED BY CR
-----------------------------

When the CR clicks "Start Session", show a small UI asking for a Session Name.

Example:

Session Name:
[ MCA Gen-AI - Morning Attendance ]

[Start Session]

The entered session name should be stored with the attendance session.

IMPORTANT:

This must NOT replace or modify the existing Session ID mechanism.

The system must continue generating and using its existing unique Session ID internally.

The session should contain both:

session_id = existing unique backend-generated ID

session_name = name entered by CR

Example:

{
"session_id": "existing-generated-id",
"session_name": "MCA Gen-AI - Morning Attendance",
...
}

The session name is for human readability/UI/Google Sheet identification only.

Session ID remains the authoritative technical identifier.

Validate the session name:

* Trim whitespace.
* Do not allow an empty session name.
* Keep a reasonable maximum length.
* Do not allow the session name to be used as an identifier/security token.

Show the session name on the CR attendance screen and in the session-specific Google Sheet/tab name where practical.

==================================================
4. ABSENTEE LIST SORTING + CSV/COPY
-----------------------------------

After the CR ends an attendance session, show the absentee list.

Currently absentees may be displayed alphabetically by name.

CHANGE THIS:

Sort absentees in ASCENDING ORDER by Enrollment Number.

Example:

## Enrollment No | Name

2026001       | Rahul
2026004       | Amit
2026010       | Priya

Do NOT sort by student name.

Use the authoritative enrollment number from the backend roster.

Handle enrollment numbers correctly as strings where necessary so that leading zeros are not accidentally lost.

Add two buttons:

[Download CSV]

[Copy Names]

DOWNLOAD CSV:

* Generate/download a CSV containing at minimum:
  Enrollment Number
  Student Name

Example:

Enrollment Number,Student Name
2026001,Rahul
2026004,Amit
2026010,Priya

The CSV should be generated from backend-verified absentee data.

COPY BUTTON:

Copy the absentee names to the clipboard in a clean format.

Example:

Rahul
Amit
Priya

Preferably provide a small confirmation such as:

"Copied!"

Do not require the CR to manually select text.

==================================================
5. CR UI TO INCREASE/DECREASE QR TOKEN LIFE
-------------------------------------------

Add a UI control on the CR attendance screen that allows the CR to increase or decrease how long the currently displayed QR token remains valid.

Example UI:

QR Valid For: 10 seconds

[-]    10 sec    [+]

or a dropdown:

QR Lifetime:
[ 10 seconds ▼ ]

Allow reasonable predefined values, for example:

5 seconds
10 seconds
15 seconds
20 seconds
30 seconds
60 seconds

The exact allowed values can be chosen based on the current implementation.

IMPORTANT SECURITY REQUIREMENTS:

* The frontend must NOT decide whether a QR token is valid.
* The backend must create the QR token and determine its expiration.
* The frontend only requests/changes the desired QR lifetime through a protected backend API.
* Never put Firebase tokens, application JWTs, student information, or sensitive information inside the QR code.
* QR tokens must remain random, short-lived, and server validated.

When the CR changes the QR lifetime:

* Apply it to newly generated/refreshed QR tokens.
* Do not invalidate the existing attendance session.
* Do not change the Session ID.
* Do not change the existing CR session architecture.
* Continue QR rotation according to the existing mechanism.

Do not allow unreasonable values that could weaken security.

The backend must validate the requested lifetime against an allowed minimum and maximum.

==================================================
6. ADD A NEW ADMIN ROLE
-----------------------

Add a new role:

"admin"

The existing roles are currently:

* cr
* student

New roles:

* admin
* cr
* student

Admin should have a dedicated UI.

ADMIN FUNCTION:

Allow an Admin to add a new CR email address.

Example:

Admin Dashboard

## Add New CR

CR Email:
[ [example@gmail.com](mailto:example@gmail.com) ]

[Add CR]

The backend must:

1. Verify that the currently authenticated user is an Admin.
2. Validate and normalize the email.
3. Add/update the email in the authoritative Firebase/Firestore admin list currently used by the application.
4. Ensure duplicate emails are handled safely.
5. Return a success/error response.

IMPORTANT:

Do NOT let the frontend directly write to Firestore.

The React frontend must call a protected FastAPI endpoint.

Example conceptual endpoint:

POST /admin/cr

The backend should use the existing admin_list collection/mechanism rather than creating a duplicate authorization system.

AUTHORIZATION:

The existing CR authorization mechanism checks the admin_list collection to determine whether an email is a CR.

Do NOT accidentally make every admin automatically become a CR unless explicitly intended.

Keep the role hierarchy clear:

ADMIN:

* Can access Admin UI.
* Can add/manage CR email addresses.
* Can have appropriate administrative access.

CR:

* Can start/end attendance sessions.
* Can view attendance statistics.
* Can manage QR lifetime.
* Can perform existing CR attendance functions.

STUDENT:

* Can mark their own attendance through the existing student flow.

Backend must enforce these permissions regardless of what the frontend displays.

IMPORTANT ADMIN SECURITY:

Do not trust a role value sent by the frontend.

The backend must derive/verify the authenticated user's role from the authenticated application identity and authoritative backend data.

Do not allow a student to call the Admin API by manually constructing an HTTP request.

==================================================
7. FIX FLOATING STUDENT ATTENDANCE POPUPS
-----------------------------------------

The CR attendance screen currently displays floating notifications when a student successfully marks attendance.

Example:

"Rahul ✓"

Current animation starts only a few pixels below the top.

CHANGE THE ANIMATION:

The student name notification should begin from the VERY BOTTOM of the visible screen and animate upward toward the TOP-RIGHT area.

Desired behavior:

BOTTOM OF SCREEN
↑
↑
↑
↑
↑
TOP-RIGHT

Example:

[Student Name ✓]
↑
↑
↑
↑
↑
bottom

The notification should:

* Start from below/at the bottom edge of the viewport.
* Move smoothly upward.
* End/disappear near the top-right area.
* Not start a few pixels below the top.
* Have a smooth entrance and exit.
* Not block the main CR controls.
* Support multiple students arriving close together without completely overlapping each other.

Use the existing frontend notification/event mechanism if one already exists.

Do not rewrite the entire CR UI just to change this animation.

==================================================
IMPORTANT EXISTING FUNCTIONALITY TO PRESERVE
============================================

Do NOT break or remove the following:

1. Firebase Google Authentication.

2. Backend Firebase ID token verification.

3. Backend-generated application JWT.

4. JWT stored in HttpOnly cookie.

5. Frontend must never read the HttpOnly JWT.

6. JWT expiration remains exactly 3 minutes unless there is an existing configuration that must be preserved.

7. Existing CR/student authorization.

8. Existing active-session/rejoin behavior.

9. Only one active attendance session at a time.

10. If multiple authorized CRs access the same active session, they must continue/rejoin the same session.

11. Ending the session by one CR ends the shared session for all CRs.

12. QR token validation.

13. QR token expiration.

14. CR location capture and attendance radius validation.

15. Student location validation.

16. Authoritative Google Sheet roster validation.

17. Verified student enrollment/name association with Firebase UID.

18. Duplicate attendance prevention.

19. Backend is the source of truth for PRESENT/ABSENT.

20. Frontend must never directly access Firestore.

21. Frontend must never directly access Google Sheets.

22. Existing Firestore security/architecture.

23. Existing Google Sheets service account architecture.

24. Existing CR attendance statistics/jar animation.

25. Existing absentee final summary.

26. Existing "End Session" behavior.

27. Existing authentication expiration handling:
    If the backend says the application JWT is invalid/expired, immediately treat the user as unauthenticated.
    Do NOT show a misleading "Continue" button.
    Clear stale frontend state and redirect/re-authenticate.

28. Existing button spam prevention:
    Start Session, End Session, Submit Attendance, Confirm Enrollment, and similar actions should remain disabled immediately after click while the request is being processed.

==================================================
IMPLEMENTATION PROCESS
======================

STEP 1 — INSPECT FIRST

Before changing anything:

* Inspect the complete current frontend structure.
* Inspect the complete current backend structure.
* Locate the current authentication implementation.
* Locate session creation/end logic.
* Locate QR generation/refresh logic.
* Locate attendance validation.
* Locate Firestore models/services.
* Locate Google Sheets service.
* Locate CR dashboard.
* Locate student attendance flow.
* Locate absentee summary.
* Locate live attendance statistics.
* Locate the existing floating attendance notification implementation.

Identify exactly which files/functions need modification.

Do NOT immediately rewrite files.

STEP 2 — CREATE A CHANGE PLAN

Before implementation, provide a concise plan containing:

* Backend files to modify.
* Frontend files to modify.
* New API endpoints required.
* Firestore schema changes.
* Google Sheets changes.
* Any environment/configuration changes.
* Any migration/backward compatibility considerations.

STEP 3 — IMPLEMENT INCREMENTALLY

Implement the changes without unnecessarily restructuring the project.

Reuse existing services/components wherever possible.

Do not create duplicate authentication, Google Sheets, Firestore, session, or QR implementations.

STEP 4 — VALIDATE

After implementation, check:

BACKEND:

* Python syntax.
* Imports.
* Pydantic models.
* FastAPI routes.
* Authentication dependencies.
* Role authorization.
* Firestore operations.
* Google Sheets operations.
* Session lifecycle.
* QR expiration.
* Duplicate attendance.
* Device/IP audit logic.

FRONTEND:

* React compilation.
* API requests.
* Credentials included for cookie authentication.
* Loading/disabled states.
* CR UI.
* Admin UI.
* Student UI.
* CSV download.
* Clipboard copy.
* QR lifetime controls.
* Floating notifications.

STEP 5 — SECURITY REVIEW

Before considering the task complete, verify:

* No Firebase Admin credentials are exposed to frontend.
* No Google service-account credentials are exposed to frontend.
* No JWT is placed in localStorage/sessionStorage.
* No JWT is placed inside QR codes.
* No student can directly modify attendance status.
* No student can call Admin APIs successfully.
* No student can call CR-only APIs successfully.
* Admin-only APIs are backend protected.
* Google Sheet roster remains authoritative.
* Session ID remains backend generated.
* Session name cannot replace Session ID.
* Device/IP information is captured server-side where possible.
* Raw IP/device information is not unnecessarily exposed to students.
* QR lifetime is validated by backend.
* Existing duplicate attendance protection remains active.

==================================================
EXPECTED RESULT
===============

After these changes:

CR FLOW:

Google Login
↓
Authorize as CR
↓
Check for existing active session
↓
If active → Rejoin existing session
If none → Start Session
↓
Enter Session Name
↓
Backend creates existing Session ID + Session Name
↓
Create session-specific Google Sheet
↓
Copy Sheet1 roster
↓
All students initially ABSENT/red
↓
CR displays QR
↓
CR can adjust QR lifetime
↓
Students scan QR and complete attendance
↓
Backend validates identity + session + QR + location + roster
↓
Backend records attendance
↓
Session Google Sheet changes student to PRESENT/green
↓
CR live statistics update
↓
Student notification floats from bottom → top-right
↓
Potential same-device/IP proxy situations are flagged
↓
CR ends session
↓
Backend calculates final attendance from authoritative records
↓
Absentees sorted by Enrollment Number ascending
↓
CR sees absentee list
↓
[Download CSV] [Copy Names]

ADMIN FLOW:

Google Login
↓
Authorize as Admin
↓
Admin Dashboard
↓
Add New CR Email
↓
Backend validates Admin role
↓
Backend updates authoritative Firebase/Firestore admin list
↓
That email can subsequently authenticate as CR

STUDENT FLOW:

Google Login
↓
Existing verified enrollment association is checked
↓
If not associated → ask for last 3 enrollment digits
↓
Backend searches authoritative Sheet1
↓
If exactly one match → show verified student name
↓
Student confirms
↓
Backend associates verified enrollment/name with Firebase UID
↓
Future attendance does not repeatedly ask for the same information

If multiple students have the same last 3 enrollment digits:

DO NOT automatically select one.

Show an appropriate message and require an unambiguous verification process.

==================================================
FINAL REQUIREMENT
=================

Keep the implementation production-oriented but simple enough to maintain.

Do not introduce unnecessary libraries or architectural changes.

Do not remove existing functionality.

Do not silently change security behavior.

If any requested feature conflicts with the existing implementation, STOP and clearly explain the conflict before making a destructive change.

After implementation, provide:

1. Files changed.
2. New endpoints added.
3. Firestore changes.
4. Google Sheets changes.
5. Frontend changes.
6. Any required environment/configuration changes.
7. Any Firebase/Google Cloud console actions required.
8. Testing steps for CR, Admin, and Student flows.
9. Any known limitations.
