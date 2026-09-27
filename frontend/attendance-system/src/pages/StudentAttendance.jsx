// Student flow: /attendance?token=<qr_token>
// 1. Token found -> show info + Continue (disabled after first click)
// 2. Location + Google sign-in (POST /authorize-user)
// 3. POST /attendance/verify-token -> starts the SERVER-side 1-minute window
// 4. Returning student: GET /student/me shows the saved verified enrollment.
//    New student: POST /student/lookup (last 3 digits only, matched on the
//    backend against Sheet1) -> "Student Found" -> POST /student/confirm
//    links the verified enrollment to the Firebase UID.
// 5. POST /attendance/check (location re-read at submit; the request body
//    carries no name/enrollment — identity comes from the saved UID link).
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import * as api from "../services/api";
import { useAuth } from "../context/AuthContext";
import { ErrorBox } from "../components/ui";
import { getCurrentLocation } from "../utils/geolocation";

const formatMMSS = (seconds) =>
  `${String(Math.max(0, Math.floor(seconds / 60))).padStart(2, "0")}:${String(
    Math.max(0, seconds % 60)
  ).padStart(2, "0")}`;

const CHECKLIST = ["Identity", "Session", "Location", "Student record"];

// Fatal codes mean the QR/session is dead — no point offering a retry.
const FATAL_CODES = ["QR_INVALID", "QR_EXPIRED", "SESSION_NOT_ACTIVE", "SESSION_NOT_FOUND"];

export default function StudentAttendance() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") || "";
  const { firebaseUser, role, error: authError, requiresReauth, login, ensureSession } = useAuth();
  // found -> last3 -> lookup -> confirm -> verifying -> success | failed
  // returning students skip straight to "welcome" after verify-token
  const [stage, setStage] = useState(token ? "found" : "invalid");
  const [windowSeconds, setWindowSeconds] = useState(0); // server-enforced window
  const [timeLeft, setTimeLeft] = useState(0);
  const [last3, setLast3] = useState("");
  const [found, setFound] = useState(null); // { name, masked_enrollment }
  const [submitError, setSubmitError] = useState(null);
  const [checkingStep, setCheckingStep] = useState(-1);
  const [locationWarning, setLocationWarning] = useState("");
  const [busy, setBusy] = useState(false); // disables Continue/Submit on click
  const busyRef = useRef(false);

  // The backend rejected the application JWT and it could not be re-issued:
  // drop all local attendance state so no stale Continue/Submit can ever
  // fire against an old session (runs only on the transition, not per render).
  useEffect(() => {
    if (requiresReauth) {
      setStage("found");
      busyRef.current = false;
      setBusy(false);
      setSubmitError("Your session expired. Please sign in again and rescan the QR.");
    }
  }, [requiresReauth]);

  // One-minute countdown (display only — the backend enforces the real limit).
  useEffect(() => {
    if (windowSeconds <= 0) return undefined;
    const timer = setInterval(() => setTimeLeft((t) => t - 1), 1000);
    return () => clearInterval(timer);
  }, [windowSeconds]);

  // If the QR/session died while the student was mid-flow, surface it and
  // remove any actionable buttons (no stale Continue on an expired session).
  // The error object is stored unrendered, so this reads it without setting.
  useEffect(() => {
    if (
      submitError &&
      typeof submitError !== "string" &&
      FATAL_CODES.includes(submitError.code)
    ) {
      setStage("failed");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submitError?.code ?? null]);

  // ---- Step 1: Continue (sign in + verify QR token) ------------------------
  const handleContinue = useCallback(async () => {
    if (busyRef.current) return; // first click wins — no duplicate requests
    busyRef.current = true;
    setBusy(true);
    setSubmitError(null);
    try {
      // undefined = Firebase is STILL restoring the session right after the
      // redirect reload. Firing login() here re-opens the Google chooser and
      // loops forever. Only a confirmed null (genuinely signed out) may sign in.
      if (firebaseUser === undefined) {
        setSubmitError("Finishing sign-in\u2026 please tap Continue again in a moment.");
        return;
      }
      if (firebaseUser === null) {
        await login();
        return;
      }
      // Ensure the backend cookie exists BEFORE the protected call. The
      // redirect flow authorizes asynchronously at boot, so we no longer
      // assume it has already run (that race caused verify-token 401s).
      const authed = await ensureSession();
      if (!authed) {
        setSubmitError("Could not verify your sign-in. Please try again.");
        return;
      }
      // Location must be allowed before the 1-minute verification continues.
      await getCurrentLocation().catch((err) => {
        setLocationWarning(err.message);
      });
      // Validate the QR token server-side; this starts the enforced window.
      const data = await api.verifyToken(token);
      const seconds = data.window_seconds || 60;
      setWindowSeconds(seconds);
      setTimeLeft(seconds);
      // Returning account? Reuse the backend-verified enrollment.
      const me = await api.getStudentMe();
      if (me.verified) {
        setFound({ name: me.name, masked_enrollment: me.masked_enrollment });
        setStage("welcome");
      } else {
        setStage("last3");
      }
    } catch (err) {
      setSubmitError(err);
      if (FATAL_CODES.includes(err.code)) setStage("failed");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [firebaseUser, login, ensureSession, token]);

  // ---- Step 2: last-3 lookup (backend resolves against Sheet1) -------------
  const handleLookup = async (event) => {
    event.preventDefault();
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setSubmitError(null);
    try {
      const data = await api.studentLookup(last3);
      setFound({ name: data.name, masked_enrollment: data.masked_enrollment });
      setStage("confirm");
    } catch (err) {
      setSubmitError(err);
      if (FATAL_CODES.includes(err.code)) setStage("failed");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // ---- Step 3: confirm -> save UID↔enrollment -> mark attendance -----------
  const handleSubmit = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setSubmitError(null);
    setStage("verifying");
    setCheckingStep(0);

    // Advance the checklist visually while the backend does the real checks.
    const stepTimer = setInterval(
      () => setCheckingStep((s) => Math.min(s + 1, CHECKLIST.length - 1)),
      400
    );

    try {
      // Link the verified enrollment to this Google account (idempotent; the
      // backend re-verifies against Sheet1 before saving anything).
      if (stage === "confirm") await api.studentConfirm(last3);
      // Fresh, high-accuracy location at submission time.
      const location = await getCurrentLocation();
      setLocationWarning("");
      await api.checkAttendance({
        sessionToken: token,
        latitude: location.latitude,
        longitude: location.longitude,
      });
      clearInterval(stepTimer);
      setCheckingStep(CHECKLIST.length);
      setStage("success");
      setSubmitError(null);
    } catch (err) {
      clearInterval(stepTimer);
      setSubmitError(err);
      setStage(FATAL_CODES.includes(err.code) ? "failed" : "retry");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // ---- Render --------------------------------------------------------------
  const windowStarted = windowSeconds > 0;
  const windowExpired = windowStarted && timeLeft <= 0;
  const errorText = submitError
    ? typeof submitError === "string"
      ? submitError
      : mapError(submitError)
    : null;

  if (stage === "invalid") {
    return (
      <StudentCard
        title="Student Attendance"
        body="No attendance session was found in this link."
        hint="Please scan the current QR code shown by your Class Representative."
      />
    );
  }

  if (stage === "found") {
    // Session expired with no Firebase session left to re-authorize from:
    // offer a fresh sign-in only — never a Continue against the old session.
    const authLoading = firebaseUser === undefined; // restoring session after redirect
    const signedOut = requiresReauth && firebaseUser === null;
    return (
      <StudentCard>
        <h1 className="app-title">Student Attendance</h1>
        {signedOut ? (
          <p className="failed-reason">
            Your session expired. Please sign in again and rescan the QR.
          </p>
        ) : (
          <>
            <p className="session-found">✓ Attendance Session Found</p>
            <p className="muted">This session is valid for 1 minute once you continue.</p>
          </>
        )}
        <button
          className="btn btn-primary btn-large"
          onClick={signedOut ? () => login().catch(() => {}) : handleContinue}
          disabled={busy || authLoading}
        >
          {authLoading
            ? "Checking sign-in…"
            : busy
              ? "Processing…"
              : signedOut
                ? "Sign in again"
                : "Continue"}
        </button>
        {locationWarning && <p className="warning-text">{locationWarning}</p>}
        <ErrorBox message={signedOut ? null : errorText || authError} />
      </StudentCard>
    );
  }

  if (stage === "welcome") {
    // Returning student: the backend already holds a verified enrollment.
    return (
      <StudentCard>
        <h1 className="app-title">Welcome {found?.name}</h1>
        <p className="student-found-name">Enrollment: {found?.masked_enrollment}</p>
        <p className={`time-remaining ${windowExpired ? "expired" : ""}`}>
          Time remaining: {formatMMSS(timeLeft)}
        </p>
        {role === "cr" ? (
          <p className="warning-text">
            You are signed in as a CR. Attendance marking is for students.
          </p>
        ) : (
          <button
            className="btn btn-primary btn-large"
            onClick={handleSubmit}
            disabled={busy || windowExpired}
          >
            {busy ? "Processing…" : "Continue to Attendance"}
          </button>
        )}
        {windowExpired && (
          <p className="failed-reason">
            Attendance window expired. Please scan the current QR code again.
          </p>
        )}
        <ErrorBox message={errorText} />
      </StudentCard>
    );
  }

  if (stage === "last3") {
    return (
      <StudentCard>
        <h1 className="app-title">Student Attendance</h1>
        <p className="session-detected">Session detected.</p>
        <p className={`time-remaining ${windowExpired ? "expired" : ""}`}>
          Time remaining: {formatMMSS(timeLeft)}
        </p>
        {role === "cr" ? (
          <p className="warning-text">
            You are signed in as a CR. Attendance marking is for students.
          </p>
        ) : (
          <form onSubmit={handleLookup} disabled={windowExpired}>
            <label>
              Enter the last 3 digits of your enrollment number
              <input
                type="text"
                inputMode="numeric"
                pattern="\d{3}"
                title="Exactly 3 digits"
                value={last3}
                onChange={(e) => setLast3(e.target.value.replace(/\D/g, "").slice(0, 3))}
                placeholder="e.g. 001"
                required
                minLength={3}
                maxLength={3}
                disabled={windowExpired || busy}
                autoFocus
              />
            </label>
            <button
              className="btn btn-primary btn-large"
              type="submit"
              disabled={windowExpired || busy || last3.length !== 3}
            >
              {busy ? "Processing…" : "Find My Record"}
            </button>
          </form>
        )}
        {windowExpired && (
          <p className="failed-reason">
            Attendance window expired. Please scan the current QR code again.
          </p>
        )}
        {locationWarning && <p className="warning-text">{locationWarning}</p>}
        <ErrorBox message={errorText || authError} />
      </StudentCard>
    );
  }

  if (stage === "confirm") {
    return (
      <StudentCard>
        <h2 className="student-found-title">Student Found</h2>
        <div className="student-found-box">
          <p className="student-found-name">{found?.name}</p>
          <p className="student-found-enrollment">
            Enrollment: {found?.masked_enrollment}
          </p>
        </div>
        <p className={`time-remaining ${windowExpired ? "expired" : ""}`}>
          Time remaining: {formatMMSS(timeLeft)}
        </p>
        <p className="muted">
          Is this you? Attendance is only marked after you confirm.
        </p>
        <button
          className="btn btn-primary btn-large"
          onClick={handleSubmit}
          disabled={busy || windowExpired}
        >
          {busy ? "Processing…" : "Confirm & Mark Attendance"}
        </button>
        <button
          className="btn btn-ghost"
          onClick={() => {
            setFound(null);
            setLast3("");
            setStage("last3");
          }}
          disabled={busy}
        >
          Not you? Re-enter digits
        </button>
        <ErrorBox message={errorText} />
      </StudentCard>
    );
  }

  if (stage === "verifying") {
    return (
      <StudentCard>
        <h2>Verifying attendance…</h2>
        <ul className="checklist">
          {CHECKLIST.map((item, index) => (
            <li key={item} className={index < checkingStep ? "done" : ""}>
              {index < checkingStep ? "✓" : "…"} {item}
            </li>
          ))}
        </ul>
      </StudentCard>
    );
  }

  if (stage === "success") {
    return (
      <StudentCard>
        <h2 className="success-title">✓ Attendance Marked Successfully</h2>
        <p className="status-present">Status: PRESENT</p>
      </StudentCard>
    );
  }

  if (stage === "failed") {
    return (
      <StudentCard>
        <h2 className="failed-title">Attendance Not Marked</h2>
        <p className="failed-reason">
          {errorText || "Something went wrong."}
        </p>
        <p className="muted hint">If your QR expired, scan the current code again.</p>
      </StudentCard>
    );
  }

  // stage === "retry": recoverable failure (radius, mismatch, timeout…)
  return (
    <StudentCard>
      <h2 className="failed-title">Attendance Not Marked</h2>
      <p className="failed-reason">
        {errorText ||
          (windowExpired
            ? "Attendance window expired. Please scan the current QR code again."
            : "Something went wrong.")}
      </p>
      {!windowExpired && (
        <button
          className="btn btn-primary"
          onClick={handleSubmit}
          disabled={busy}
        >
          {busy ? "Processing…" : "Try Again"}
        </button>
      )}
      <p className="muted hint">If your QR expired, scan the current code again.</p>
    </StudentCard>
  );
}

// ---- helpers -------------------------------------------------------------
function mapError(err) {
  if (err.code === "QR_EXPIRED") return "QR code expired.";
  if (err.code === "QR_INVALID") return "Invalid QR code. Please scan the current QR.";
  if (err.code === "SESSION_NOT_ACTIVE") return "Attendance session has ended.";
  if (err.code === "SESSION_NOT_FOUND") return "Attendance session not found.";
  if (err.code === "ATTENDANCE_WINDOW_EXPIRED")
    return "Attendance window expired. Please scan the current QR code again.";
  if (err.code === "STUDENT_NOT_FOUND")
    return "No student record matches these digits. Please check and try again.";
  if (err.code === "MULTIPLE_STUDENTS_MATCH")
    return "Multiple students found with these digits. Please contact your CR.";
  if (err.code === "ENROLLMENT_NOT_VERIFIED")
    return "Please verify your enrollment details first.";
  if (err.code === "OUTSIDE_LOCATION") return "You are outside the attendance location.";
  if (err.code === "ALREADY_MARKED") return "Attendance has already been marked.";
  if (err.code === "PERMISSION_DENIED") return "Location permission is required.";
  return err.message || "Attendance could not be verified. Please try again.";
}

function StudentCard({ title, body, hint, children }) {
  return (
    <div className="page-center">
      <div className="student-shell">
        <SignOutChip />
        <div className="card student-card">
          {title && <h1 className="app-title">{title}</h1>}
          {body && <p>{body}</p>}
          {hint && <p className="muted hint">{hint}</p>}
          {children}
        </div>
      </div>
    </div>
  );
}

// Mobile students have no other way to leave a signed-in Google account
// (e.g. wrong account) — always show who is signed in + a sign-out button.
function SignOutChip() {
  const { firebaseUser, logout } = useAuth();
  if (!firebaseUser) return null;
  return (
    <div className="auth-chip">
      <span className="auth-chip-user">Signed in as {firebaseUser.email}</span>
      <button className="btn btn-ghost btn-small" onClick={() => logout().catch(() => {})}>
        Sign out
      </button>
    </div>
  );
}
