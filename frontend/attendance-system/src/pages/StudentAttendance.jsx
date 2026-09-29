// Student flow: /attendance?token=<qr_token>
//
// Two timers are deliberately kept apart (see Backend/app/services/qr_service.py):
//   • the QR token's short lifetime only decides whether this page may be
//     ENTERED, and
//   • the student's individual attendance attempt decides how long they have to
//     COMPLETE it — a server-side window that starts the instant the QR link is
//     opened, BEFORE Google sign-in and BEFORE any location prompt.
//
// 1. On mount: POST /attendance/attempt/start with the QR token (public, no JWT)
//    -> an opaque attempt id + the server's countdown. A live attempt is kept in
//    sessionStorage so the full-page Google redirect resumes the SAME attempt
//    instead of restarting the timer.
// 2. The UI then advances itself: sign in when needed, else bind the attempt to
//    the authenticated Firebase UID (POST /attendance/attempt/bind), which also
//    returns the saved student identity in one round trip.
// 3. New student: POST /student/lookup (last 3 digits, matched on the backend
//    against Sheet1) -> POST /student/confirm links enrollment to the UID.
// 4. Submit: fresh high-accuracy location + POST /attendance/check carrying the
//    attempt id only — no name, enrollment, QR token or status.
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as api from "../services/api";
import { useAuth } from "../context/AuthContext";
import {
  clearAttendanceAttempt,
  consumeReturnFromGoogleRedirect,
  loadAttendanceAttempt,
  saveAttendanceAttempt,
} from "../services/auth";
import { attendanceLog } from "../utils/authLog";
import { ErrorBox } from "../components/ui";
import { getCurrentLocation } from "../utils/geolocation";

const formatMMSS = (seconds) =>
  `${String(Math.max(0, Math.floor(seconds / 60))).padStart(2, "0")}:${String(
    Math.max(0, seconds % 60)
  ).padStart(2, "0")}`;

// These mean the attempt can never complete: the student must scan a fresh QR.
// The expired attempt is dropped, never silently restarted.
const FATAL_CODES = [
  "QR_INVALID",
  "QR_EXPIRED",
  "SESSION_NOT_ACTIVE",
  "SESSION_NOT_FOUND",
  "ATTENDANCE_WINDOW_EXPIRED",
  "ATTENDANCE_ATTEMPT_NOT_FOUND",
  "ATTENDANCE_ATTEMPT_USED",
  "ATTENDANCE_ATTEMPT_BOUND",
];

export default function StudentAttendance() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const token = searchParams.get("token") || "";
  const { firebaseUser, error: authError, requiresReauth, login, ensureSession } = useAuth();
  // entry -> auth -> (identity | last3 -> confirm) -> verifying -> success
  //                                                        -> failed | retry
  // "auth" is also where a signed-out student is automatically sent to Google;
  // "signin" is only shown if that redirect could not be started (or was
  // cancelled), so no student presses "Continue" just to discover their state.
  const [phase, setPhase] = useState("entry");
  const [attempt, setAttempt] = useState(null); // { attemptId, timeLeft }
  const [timeLeft, setTimeLeft] = useState(0); // display only — backend enforces
  const [last3, setLast3] = useState("");
  const [found, setFound] = useState(null); // { name, masked_enrollment }
  const [submitError, setSubmitError] = useState(null);
  const [statusLine, setStatusLine] = useState(""); // real, contextual progress
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const autoSignInRef = useRef(false); // one automatic redirect per page life
  const boundRef = useRef(false); // the attempt is bound at most once per phase
  // Absolute local deadline for the current attempt. It is only ever a display
  // approximation (device clock skew, redirect time); the backend's own
  // `expires_at` is what enforces the window.
  const deadlineRef = useRef(0);
  const attemptIdRef = useRef(null);

  // Drop a doomed attempt for good: cleared locally and the UI asks for a scan.
  const failFatal = useCallback((err) => {
    clearAttendanceAttempt();
    attemptIdRef.current = null;
    deadlineRef.current = 0;
    boundRef.current = false;
    setAttempt(null);
    setSubmitError(err);
    setPhase("failed");
  }, []);

  // ---- Step 1: open (or resume) the attendance attempt, pre-auth ------------
  useEffect(() => {
    let cancelled = false;

    const adoptAttempt = (stored) => {
      attemptIdRef.current = stored.attemptId;
      deadlineRef.current = Date.now() + stored.timeLeft * 1000;
      setAttempt({ attemptId: stored.attemptId });
      setTimeLeft(stored.timeLeft);
      setPhase("auth");
      attendanceLog("attempt restored", { secondsLeft: stored.timeLeft });
      // The QR token has done its job (it proved entry). Removing it from the
      // address bar means a refresh resumes the attempt instead of trying to
      // open a new one with a QR that has long since rotated.
      navigate("/attendance", { replace: true });
    };

    const open = async () => {
      const resumed = loadAttendanceAttempt(); // returning from Google redirect
      if (resumed) {
        if (!cancelled) adoptAttempt(resumed);
        return;
      }
      if (!token) {
        if (!cancelled) setPhase("invalid");
        return;
      }
      try {
        const data = await api.startAttempt(token);
        if (cancelled) return;
        adoptAttempt(saveAttendanceAttempt(data));
      } catch (err) {
        if (cancelled) return;
        clearAttendanceAttempt();
        setSubmitError(err);
        setPhase(FATAL_CODES.includes(err.code) ? "failed" : "retry");
      }
    };

    open();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Countdown (display only; the backend stamps enforce the real window) --
  useEffect(() => {
    if (!attempt) return undefined;
    const tick = () => {
      const left = Math.max(0, Math.round((deadlineRef.current - Date.now()) / 1000));
      setTimeLeft(left);
      if (left > 0) return;
      // Expired while authenticating or while reading the countdown: the
      // attempt is discarded, never restarted, and a fresh QR is demanded.
      clearInterval(timer);
      clearAttendanceAttempt();
      attemptIdRef.current = null;
      setAttempt(null);
      setSubmitError({ code: "ATTENDANCE_WINDOW_EXPIRED" });
      setPhase("failed");
    };
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [attempt]);

  // The backend rejected the application JWT and it could not be re-issued:
  // go back to the authentication step (the attempt itself is still valid and
  // server-bound, so no rescan is needed unless it also expired).
  useEffect(() => {
    if (!requiresReauth) return;
    setFound(null);
    setBusy(false);
    busyRef.current = false;
    boundRef.current = false;
    if (attemptIdRef.current) setPhase("auth");
  }, [requiresReauth]);

  // ---- Step 2: authentication -> bind the attempt -> load saved student ------
  const advance = useCallback(async () => {
    if (!attempt) return;
    // Advance from BOTH the automatic "auth" phase and the manual "signin"
    // fallback: a real user can be restored slightly after an initial
    // signed-out read, and the flow must then continue without another scan.
    if (phase !== "auth" && phase !== "signin") return;
    // Firebase is still restoring the session after the redirect reload.
    // Signing in here would re-open the account chooser and loop.
    if (firebaseUser === undefined) return;
    if (firebaseUser === null) {
      // The manual sign-in screen is already showing: wait for the student's
      // tap instead of firing another automatic redirect (that is the loop).
      if (phase === "signin") return;
      // Did we JUST come back from Google and still be signed out? The
      // redirect did not yield a session (cancelled / rejected / wrong
      // account). Firing signInWithRedirect again is the infinite loop, so we
      // consume the one-time marker and fall back to a manual button instead.
      const cameBackSignedOut = consumeReturnFromGoogleRedirect();
      if (cameBackSignedOut) {
        attendanceLog("returned from Google still signed out; awaiting manual sign-in");
      }
      // Not signed in: go straight to Google — no "Continue" tap needed. Only
      // the first pass is automatic; a cancelled chooser falls back to a
      // visible button instead of looping the redirect.
      if (autoSignInRef.current || cameBackSignedOut) {
        setPhase("signin");
        return;
      }
      autoSignInRef.current = true;
      try {
        await login();
      } catch {
        setPhase("signin");
      }
      return;
    }

    // Reuses the existing application session when it is still valid — a
    // redundant getIdToken + /authorize-user round trip is skipped.
    const authed = await ensureSession();
    if (!authed) {
      setSubmitError("Could not verify your sign-in. Please try again.");
      return;
    }
    if (authed !== "student") {
      setPhase("notstudent");
      return;
    }
    // `advance` is re-created when the ambient /authorize-user finally lands,
    // so the binding itself is guarded: one bind call per attempt phase.
    if (boundRef.current) return;
    boundRef.current = true;
    try {
      // Server-side binding (the client never claims an identity) + the saved
      // enrollment in the same response.
      const data = await api.bindAttempt(attempt.attemptId);
      attendanceLog("attempt bound");
      // Re-sync the display with the server's remaining time.
      const left = Number(data.remaining_seconds) || 0;
      deadlineRef.current = Date.now() + left * 1000;
      setTimeLeft(left);
      if (data.verified) {
        setFound({ name: data.name, masked_enrollment: data.masked_enrollment });
        setPhase("identity");
      } else {
        setPhase("last3");
      }
    } catch (err) {
      boundRef.current = false; // a retry (network / expired JWT) may re-bind
      setSubmitError(err);
      if (FATAL_CODES.includes(err.code)) failFatal(err);
    }
  }, [attempt, phase, firebaseUser, login, ensureSession, failFatal]);

  useEffect(() => {
    advance();
  }, [advance]);

  // ---- Step 3: last-3 lookup (backend resolves against Sheet1) --------------
  const handleLookup = async (event) => {
    event.preventDefault();
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setSubmitError(null);
    try {
      const data = await api.studentLookup(last3);
      setFound({ name: data.name, masked_enrollment: data.masked_enrollment });
      setPhase("confirm");
    } catch (err) {
      setSubmitError(err);
      if (FATAL_CODES.includes(err.code)) failFatal(err);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // ---- Step 4: confirm -> link enrollment -> fresh location -> check --------
  const handleSubmit = async () => {
    if (busyRef.current || !attempt) return;
    busyRef.current = true;
    setBusy(true);
    setSubmitError(null);
    setPhase("verifying");

    try {
      if (phase === "confirm") {
        setStatusLine("Linking your verified enrollment…");
        await api.studentConfirm(last3);
      }
      // Fresh, high-accuracy location — read only at submission time.
      setStatusLine("Getting your current location…");
      const location = await getCurrentLocation();
      setStatusLine("Recording your attendance…");
      await api.checkAttendance({
        attemptId: attempt.attemptId,
        latitude: location.latitude,
        longitude: location.longitude,
      });
      clearAttendanceAttempt();
      attemptIdRef.current = null;
      setAttempt(null);
      setPhase("success");
      setSubmitError(null);
    } catch (err) {
      setSubmitError(err);
      if (FATAL_CODES.includes(err.code)) failFatal(err);
      else setPhase("retry");
    } finally {
      setStatusLine("");
      busyRef.current = false;
      setBusy(false);
    }
  };

  // ---- Render --------------------------------------------------------------
  const windowExpired = Boolean(attempt) && timeLeft <= 0;
  const errorText = submitError
    ? typeof submitError === "string"
      ? submitError
      : mapError(submitError)
    : null;

  if (phase === "entry") {
    return (
      <StudentCard>
        <h1 className="app-title">Student Attendance</h1>
        <p className="muted">Starting your attendance session…</p>
        {errorText && <ErrorBox message={errorText} />}
      </StudentCard>
    );
  }

  if (phase === "invalid") {
    return (
      <StudentCard
        title="Student Attendance"
        body="No attendance session was found in this link."
        hint="Please scan the current QR code shown by your Class Representative."
      />
    );
  }

  if (phase === "auth" || phase === "signin") {
    // The attempt is already running: show its countdown while the student
    // authenticates so the decoupling from the QR lifetime is visible.
    const authLoading = firebaseUser === undefined;
    return (
      <StudentCard>
        <h1 className="app-title">Student Attendance</h1>
        <p className="session-found">✓ Attendance attempt started</p>
        <p className={`time-remaining ${windowExpired ? "expired" : ""}`}>
          Time remaining: {formatMMSS(timeLeft)}
        </p>
        <p className="muted">
          {authLoading
            ? "Checking your sign-in…"
            : phase === "signin"
              ? "Sign in with your Google account to continue."
              : "Signing you in…"}
        </p>
        {phase === "signin" && (
          <button
            className="btn btn-primary btn-large"
            onClick={() => login().catch(() => {})}
            disabled={busy}
          >
            Sign in with Google
          </button>
        )}
        <ErrorBox message={errorText || authError} />
      </StudentCard>
    );
  }

  if (phase === "notstudent") {
    return (
      <StudentCard>
        <h1 className="app-title">Student Attendance</h1>
        <p className="warning-text">
          You are signed in as a CR or admin. Attendance marking is for students.
        </p>
        <p className={`time-remaining ${windowExpired ? "expired" : ""}`}>
          Time remaining: {formatMMSS(timeLeft)}
        </p>
        <ErrorBox message={errorText} />
      </StudentCard>
    );
  }

  if (phase === "identity") {
    // Returning student: the backend already holds a verified enrollment.
    return (
      <StudentCard>
        <h1 className="app-title">Welcome {found?.name}</h1>
        <p className="student-found-name">Enrollment: {found?.masked_enrollment}</p>
        <p className={`time-remaining ${windowExpired ? "expired" : ""}`}>
          Time remaining: {formatMMSS(timeLeft)}
        </p>
        <button
          className="btn btn-primary btn-large"
          onClick={handleSubmit}
          disabled={busy || windowExpired || !attempt}
        >
          {busy ? "Processing…" : "Continue to Attendance"}
        </button>
        <ErrorBox message={errorText} />
      </StudentCard>
    );
  }

  if (phase === "last3") {
    return (
      <StudentCard>
        <h1 className="app-title">Student Attendance</h1>
        <p className="session-detected">Session detected.</p>
        <p className={`time-remaining ${windowExpired ? "expired" : ""}`}>
          Time remaining: {formatMMSS(timeLeft)}
        </p>
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
        {windowExpired && (
          <p className="failed-reason">
            Attendance window expired. Please scan the current QR code again.
          </p>
        )}
        <ErrorBox message={errorText || authError} />
      </StudentCard>
    );
  }

  if (phase === "confirm") {
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
          disabled={busy || windowExpired || !attempt}
        >
          {busy ? "Processing…" : "Confirm & Mark Attendance"}
        </button>
        <button
          className="btn btn-ghost"
          onClick={() => {
            setFound(null);
            setLast3("");
            setPhase("last3");
          }}
          disabled={busy}
        >
          Not you? Re-enter digits
        </button>
        <ErrorBox message={errorText} />
      </StudentCard>
    );
  }

  if (phase === "verifying") {
    // Contextual state driven by the actual operation in flight — no simulated
    // progress bars.
    return (
      <StudentCard>
        <h2>Verifying attendance…</h2>
        <p className="muted">{statusLine || "Please wait."}</p>
      </StudentCard>
    );
  }

  if (phase === "success") {
    return (
      <StudentCard>
        <h2 className="success-title">✓ Attendance Marked Successfully</h2>
        <p className="status-present">Status: PRESENT</p>
      </StudentCard>
    );
  }

  if (phase === "failed") {
    return (
      <StudentCard>
        <h2 className="failed-title">Attendance Not Marked</h2>
        <p className="failed-reason">{errorText || "Something went wrong."}</p>
        <p className="muted hint">
          {submitError?.code === "ATTENDANCE_WINDOW_EXPIRED"
            ? "Your attendance attempt has expired and cannot be reused. Please scan the QR code currently shown by your CR."
            : "If your QR expired, scan the current code again."}
        </p>
      </StudentCard>
    );
  }

  // phase === "retry": recoverable failure (radius, mismatch, network…)
  return (
    <StudentCard>
      <h2 className="failed-title">Attendance Not Marked</h2>
      <p className="failed-reason">{errorText || "Something went wrong."}</p>
      {!windowExpired && attempt && (
        <button className="btn btn-primary" onClick={handleSubmit} disabled={busy}>
          {busy ? "Processing…" : "Try Again"}
        </button>
      )}
      {!attempt && (
        <p className="muted hint">Please scan the current QR code again.</p>
      )}
    </StudentCard>
  );
}

// ---- helpers -------------------------------------------------------------
function mapError(err) {
  if (err.code === "QR_EXPIRED")
    return "That QR code has expired. Please scan the current one.";
  if (err.code === "QR_INVALID") return "Invalid QR code. Please scan the current QR.";
  if (err.code === "SESSION_NOT_ACTIVE") return "Attendance session has ended.";
  if (err.code === "SESSION_NOT_FOUND") return "Attendance session not found.";
  if (err.code === "ATTENDANCE_WINDOW_EXPIRED")
    return "Your 60-second attendance window expired. Please scan the current QR code again.";
  if (err.code === "ATTENDANCE_ATTEMPT_NOT_FOUND")
    return "Your attendance attempt is no longer available. Please scan the QR code again.";
  if (err.code === "ATTENDANCE_ATTEMPT_USED")
    return "This attendance attempt has already been used. Please scan a fresh QR.";
  if (err.code === "ATTENDANCE_ATTEMPT_BOUND")
    return "This attendance attempt belongs to a different signed-in account. Please scan a fresh QR.";
  if (err.code === "STUDENT_NOT_FOUND")
    return "No student record matches these digits. Please check and try again.";
  if (err.code === "MULTIPLE_STUDENTS_MATCH")
    return "Multiple students found with these digits. Please contact your CR.";
  if (err.code === "ENROLLMENT_ALREADY_SET")
    return "An enrollment is already linked to your account. Please contact your CR.";
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
          <BrandMark size={54} className="brand-student" />
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
