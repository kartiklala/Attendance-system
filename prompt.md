I want you to modify the existing attendance system according to the requirements below.

IMPORTANT:

* First inspect the existing frontend and backend code before making changes.
* Do NOT rewrite the entire application.
* Preserve the current architecture and working functionality.
* Make the smallest clean changes necessary.
* Follow the existing React + Vite frontend and Python + FastAPI backend architecture.
* React must NOT directly access Firestore or Google Sheets.
* All authentication, authorization, session state, student verification, attendance validation, duplicate prevention, and database/Google Sheets operations must remain backend-controlled.
* Do not move security-sensitive logic to the frontend.
* Do not store the application JWT in localStorage/sessionStorage.
* Continue using the existing HttpOnly JWT cookie mechanism.
* Do not break the existing Firebase Google authentication, Firebase Admin SDK, Firestore, Google Sheets, QR, location verification, or attendance functionality.

==================================================

1. MULTIPLE CRs MUST SHARE ONE ACTIVE ATTENDANCE SESSION
   ==================================================

Current behavior:
After a CR successfully logs in and passes the `/authorize-user` API, the CR can start an attendance session.

Change this behavior:

* After `/authorize-user` confirms that the logged-in user has role `cr`, the backend must check Firestore for an existing ACTIVE attendance session.
* If an active session already exists:

  * Do NOT create another session.
  * Return the existing active session information.
  * The CR should automatically continue/rejoin that existing session.
* If no active session exists:

  * The CR should see the normal "Start Session Attendance" option.
  * Clicking it creates a new active session.

Example:

CR 1:
Login → authorize-user → no active session → Start Attendance → Session 1 created.

CR 2:
Login → authorize-user → Session 1 already active → automatically connect to Session 1.

Both CR 1 and CR 2 must see the SAME:

* session_id
* active attendance state
* attendance progress
* QR token/session
* present count
* total student count

There must never be two simultaneous active attendance sessions.

Backend requirements:

* The backend must be the source of truth for active session detection.
* Do not rely only on React state.
* Make the active-session lookup safe against race conditions so two CRs clicking at approximately the same time cannot accidentally create two active sessions.
* Ending the session should end the shared session for ALL CRs connected to it.
* If CR 1 ends the session, CR 2 must also see that the session has ended.

==================================================
2. INVALID JWT / EXPIRED SESSION MUST REMOVE "CONTINUE"
=======================================================

If the application JWT becomes invalid, expires, or is rejected by the backend:

* The frontend must immediately treat the user as unauthenticated.
* Do NOT continue showing a "Continue" button that allows the user to proceed with the old session.
* This must also work when the user:

  * clicks Back
  * returns to a previous screen
  * refreshes
  * navigates between screens
  * clicks Continue again
  * opens an old attendance URL

For any protected API that returns 401/UNAUTHORIZED because the application JWT is invalid or expired:

1. Clear/reset the frontend authenticated-user state.
2. Remove any stale attendance/session UI state.
3. Do not show the Continue button.
4. Redirect/show the login/Google authentication screen as appropriate.
5. Require fresh authorization through `/authorize-user`.
6. Do not attempt to reuse an expired JWT.

Important:

* The frontend must NOT try to inspect or decode the HttpOnly JWT itself.
* The backend remains responsible for validating the JWT.
* Use the existing `/me` or appropriate backend authentication mechanism if needed to restore/check authentication state.

Also prevent UI race conditions where a user can click Continue while an authentication check is still in progress.

==================================================
3. STUDENT: ASK ONLY FOR LAST 3 DIGITS OF ENROLLMENT
====================================================

Change the student attendance flow.

Current behavior:
The student is asked to enter:

* Name
* Full enrollment number

I no longer want this.

New flow:

Step 1:
Student scans QR and authenticates with Google as currently implemented.

Step 2:
Ask ONLY:

"Enter the last 3 digits of your enrollment number"

Example:
Student enters:

001

Step 3:
Backend must use the authoritative Google Sheet `Sheet1` to find the matching student.

The Google Sheet currently contains:

Sheet1:

Enrollment-ID | Name

Example:

2025001001 | Rahul Sharma
2025001002 | Priya Singh
2025001003 | Aman Kumar

If the student enters:

001

the backend should identify the appropriate enrollment number ending in `001`.

IMPORTANT:

* The backend must perform this lookup.
* React must NOT download the entire student roster and search locally.
* Do not trust the student's submitted name.
* Do not let the frontend decide which student is present.

After successful lookup:

Display on the student's screen:

"Student Found"

Name:
Rahul Sharma

Enrollment:
********001

Then show a confirmation/submit button.

The student clicks Submit/Confirm.

Only after confirmation should the backend perform the final attendance validation and mark attendance.

The backend must still perform all existing checks:

* authenticated student
* valid application JWT
* valid QR/session token
* active attendance session
* attendance time window
* enrollment/student match
* location within allowed radius
* duplicate attendance prevention
* Google Sheet attendance write

Do not weaken any existing validation.

---

## IMPORTANT: LAST 3 DIGITS MUST BE UNAMBIGUOUS

If multiple students have the same last 3 digits:

Example:

2025001001 → Rahul
2026001001 → Amit

Do NOT arbitrarily choose one.

Return an appropriate error such as:

"Multiple students found with these digits. Please contact your CR."

However, if the existing enrollment data guarantees uniqueness, handle the normal single-match case.

---

## SAVE STUDENT ↔ GOOGLE ACCOUNT ASSOCIATION

After the student successfully confirms attendance for the first time, save the student's verified enrollment information against their Firebase/Google account in Firestore.

For example, in the existing `users/{uid}` document:

{
"uid": "...",
"name": "Rahul Sharma",
"email": "...",
"role": "student",
"enrollment_no": "2025001001"
}

Use the existing user document structure where possible instead of creating unnecessary duplicate collections.

The enrollment number must ONLY be saved after the backend has successfully verified it against Google Sheets.

On future attendance attempts:

* After Google authentication, check whether the authenticated Firebase UID already has a verified enrollment number saved in Firestore.
* If it exists, do NOT ask the student for their enrollment digits again.
* Display the saved student information and allow the student to continue through the attendance flow.

Example:

Google Login
↓
Backend authorization
↓
Firestore users/{uid}
↓
Enrollment already verified?
↓
YES
↓
Show:
"Welcome Rahul Sharma"
"Enrollment: ********001"
↓
Continue to attendance validation

For a new Google account:

Google Login
↓
No enrollment saved
↓
Ask for last 3 digits
↓
Verify against Sheet1
↓
Show student name
↓
Student confirms
↓
Save enrollment against Firebase UID

Security requirements:

* Never allow the frontend to directly write the enrollment number to Firestore.
* Backend must verify the enrollment against Sheet1 before saving it.
* Do not allow a student to arbitrarily change their saved enrollment number from the frontend.
* If a saved enrollment exists, the backend should continue treating it as authoritative only if it was previously verified.
* Do not expose the entire student roster to the frontend.

==================================================
4. CR SCREEN: LIVE JAR-FILLING ATTENDANCE ANIMATION
===================================================

On the CR attendance screen, add a visually clean "jar filling" animation.

Purpose:
Show how many students have marked PRESENT out of the total number of students.

Example:

Total students: 52
Present: 26

Display:

26 / 52 Present
50%

And visually show a jar/container filling to approximately 50%.

Requirements:

* The fill percentage must come from backend attendance data.
* Do NOT calculate attendance based only on local frontend state.
* The frontend should periodically fetch/update the current session attendance statistics.
* Avoid excessive API polling; use a reasonable interval or existing mechanism if one already exists.
* The percentage should update automatically when students mark attendance.

The jar should clearly show:

* current percentage
* present count
* total student count

Example:

```
      ______
     /      \
    |        |
    |████████| 50%
    |████████|
    |        |
    |________|
```

Use a polished modern UI suitable for a college attendance application.

---

## STUDENT PRESENT POPUP ANIMATION

Whenever a student successfully marks attendance:

Show a small floating popup/notification on the CR screen.

Example:

"Rahul Sharma ✓"

The notification should:

* appear near the jar/dashboard
* float upward
* fade out automatically
* not block the CR's controls
* not require manual dismissal

Multiple students marking attendance should create separate notifications without breaking the UI.

IMPORTANT:
The backend should provide the data needed for this.
Do not trust a frontend-generated "student present" event.

Only display a student as PRESENT after the backend has successfully recorded the attendance.

==================================================
5. AFTER END SESSION: SHOW ABSENTEES + PRESENT COUNT
====================================================

When the CR clicks:

"End Session"

the backend should close the active attendance session as it currently does.

After the session is successfully ended, show a final attendance summary screen.

The summary should contain:

A. ABSENTEES

Show a list of all students who did NOT mark attendance.

Example:

Absent Students

1. Rahul Sharma — 2025001001
2. Priya Singh — 2025001002
3. Aman Kumar — 2025001003

The absentee list must be generated by the backend by comparing:

* authoritative student roster from Google Sheet
  AGAINST
* students recorded as PRESENT for this session.

Do NOT calculate the absentee list purely in React.

B. PRESENT COUNT

When scrolling down, show:

Present Students: 42 / 52

or:

Total Present
42

Also show the percentage if useful:

Attendance: 80.77%

The final summary should clearly distinguish:

* Total Students
* Present
* Absent
* Attendance Percentage

The absentee list should be scrollable if there are many students.

Make sure the final result is based on server-side data after the session has actually ended.

==================================================
6. DISABLE "CONTINUE" AFTER FIRST CLICK
=======================================

Any "Continue" button in the authentication/attendance flow must become disabled immediately when clicked.

Purpose:
Prevent spam clicking and duplicate API requests.

Behavior:

Before click:
[ Continue ]

After click:
[ Processing... ]  ← disabled

The user must not be able to click it again until the operation finishes.

Requirements:

* Disable immediately on first click.
* Show a loading/processing state.
* Prevent duplicate API calls.
* Re-enable only if the operation fails and the user is allowed to retry.
* If the JWT/session becomes invalid during the operation, do not re-enable Continue as if the session were still valid. Return the user to authentication.

Apply the same protection to other important one-time actions where appropriate, especially:

* Start Session
* End Session
* Submit Attendance
* Confirm student enrollment

Do NOT rely only on frontend button disabling for duplicate prevention. The backend must continue to enforce:

* one active session
* duplicate attendance prevention
* valid session
* valid authentication

==================================================
7. API / BACKEND DESIGN
=======================

Before implementing, inspect the existing services and reuse them where possible.

Likely relevant files include:

Backend/app/routers/auth.py
Backend/app/routers/attendance.py
Backend/app/services/auth_service.py
Backend/app/services/attendance_service.py
Backend/app/services/session_service.py
Backend/app/services/qr_service.py
Backend/app/services/student_service.py
Backend/app/services/sheets_service.py
Backend/app/middleware/auth.py
Backend/app/core/security.py

Add or modify endpoints only where necessary.

Potential backend capabilities needed:

* Get/rejoin active session after CR authorization
* Get current session attendance statistics
* Look up student by last 3 enrollment digits
* Retrieve verified enrollment associated with Firebase UID
* Confirm/save verified enrollment
* Get final attendance summary after session ends

Use the existing error-response structure.

Do not expose sensitive information unnecessarily.

==================================================
8. DATA CONSISTENCY / SECURITY
==============================

The following rules are mandatory:

1. Backend is the source of truth for attendance.
2. Backend is the source of truth for CR/student roles.
3. Backend is the source of truth for active session state.
4. Backend is the source of truth for present/absent status.
5. React must never directly access Google Sheets.
6. React must never directly access Firestore.
7. React must never determine whether someone is PRESENT.
8. React must never determine whether someone is CR.
9. React must never store the application JWT.
10. Application JWT remains HttpOnly.
11. Firebase ID token is only sent to `/authorize-user`.
12. Student enrollment must be verified against Sheet1 before being associated with a Firebase UID.
13. Duplicate attendance must remain blocked server-side.
14. Multiple CRs must operate on the same active session.
15. Ending a session must invalidate its QR/session attendance ability.
16. Never trust frontend attendance counts as authoritative.
17. Never trust frontend student names/enrollment numbers without backend verification.

==================================================
9. UI/UX
========

Keep the existing application's design language.

Do not completely redesign the application.

Make the new screens responsive for:

* desktop
* mobile

Use clear states:

Loading
Success
Error
Expired session
Session ended
Attendance already marked
Student found
Student not found
Multiple enrollment matches
Location required
Outside attendance radius

Avoid confusing navigation.

When a session expires or is ended:

* do not leave stale Continue buttons visible
* do not allow old attendance submission
* clear stale session state where appropriate.

==================================================
10. IMPLEMENTATION PROCESS
==========================

Before modifying code:

1. Inspect the current frontend authentication flow.
2. Inspect `/authorize-user`.
3. Inspect current session creation/end logic.
4. Inspect Firestore session structure.
5. Inspect current student verification.
6. Inspect Google Sheets integration.
7. Inspect current attendance statistics, if any.
8. Inspect current CR UI.
9. Inspect current student UI.
10. Identify exactly which files need modification.

Then implement the changes.

After implementation:

* Check for TypeScript/JavaScript errors.
* Check for Python syntax/import errors.
* Check that all existing API routes still work.
* Check that no Firebase/Google credentials are exposed to the frontend.
* Check that the application JWT remains HttpOnly.
* Check that duplicate requests are protected.
* Check that two CRs cannot create two active sessions.
* Check that an expired JWT cannot access protected functionality.
* Check that a student can be associated with only a backend-verified enrollment.
* Check that the final absentee list is generated from server-side attendance data.

Do not make unrelated changes.

At the end, give me:

1. A list of files changed.
2. A short explanation of what changed in each file.
3. Any new API endpoints added.
4. Any Firestore schema changes.
5. Any environment variables added/changed.
6. Any frontend changes.
7. Any commands I need to run to test/deploy the changes.
8. Any assumptions you had to make.
