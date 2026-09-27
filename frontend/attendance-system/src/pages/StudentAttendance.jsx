// Student flow: /attendance?token=<qr_token>
// 1. Token found -> show info + Continue
// 2. Location + Google sign-in (POST /authorize-user)
// 3. POST /attendance/verify-token -> starts the SERVER-side 1-minute window
// 4. Name + Enrollment form -> POST /attendance/check (location re-read at submit)
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

export default function StudentAttendance() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") || "";
  const { firebaseUser, profile, role, error: authError, login } = useAuth();

  // found -> form -> verifying -> success | failed
  const [stage, setStage] = useState(token ? "found" : "invalid");
  const [windowSeconds, setWindowSeconds] = useState(0); // server-enforced window
  const [timeLeft, setTimeLeft] = useState(0);
  const [name, setName] = useState("");
  const [enrollment, setEnrollment] = useState("");
  const [submitError, setSubmitError] = useState(null);
  const [checkingStep, setCheckingStep] = useState(-1);
  const [locationWarning, setLocationWarning] = useState("");
  const expiredRef = useRef(false);

  expiredRef.current = stage !== "form" ? expiredRef.current : timeLeft <= 0 && windowSeconds > 0;

  // One-minute countdown (display only — the backend enforces the real limit).
  useEffect(() => {
    if (windowSeconds <= 0) return undefined;
    const timer = setInterval(() => setTimeLeft((t) => t - 1), 1000);
    return () => clearInterval(timer);
  }, [windowSeconds]);

  // Pre-fill the name from the Google profile for convenience.
  useEffect(() => {
    if (profile?.name) setName((current) => current || profile.name);
  }, [profile]);

  const handleContinue = useCallback(async () => {
    setSubmitError(null);
    try {
      // Location must be allowed before the 1-minute verification continues.
      await getCurrentLocation().catch((err) => {
        setLocationWarning(err.message);
      });
      if (!firebaseUser) await login();
      // Validate the QR token server-side; this starts the enforced window.
      const data = await api.verifyToken(token);
      const seconds = data.window_seconds || 60;
      setWindowSeconds(seconds);
      setTimeLeft(seconds);
      setStage("form");
    } catch (err) {
      setSubmitError(mapError(err));
      if (["QR_INVALID", "QR_EXPIRED", "SESSION_NOT_ACTIVE"].includes(err.code)) {
        setStage("failed");
      }
    }
  }, [firebaseUser, login, token]);

  const handleSubmit = async (event) => {
    event.preventDefault();
    setSubmitError(null);
    setStage("verifying");
    setCheckingStep(0);

    // Advance the checklist visually while the backend does the real checks.
    const stepTimer = setInterval(
      () => setCheckingStep((s) => Math.min(s + 1, CHECKLIST.length - 1)),
      400
    );

    try {
      // Fresh, high-accuracy location at submission time.
      const location = await getCurrentLocation();
      setLocationWarning("");
      const result = await api.checkAttendance({
        sessionToken: token,
        name: name.trim(),
        enrollmentNo: enrollment.trim(),
        latitude: location.latitude,
        longitude: location.longitude,
      });
      clearInterval(stepTimer);
      setCheckingStep(CHECKLIST.length);
      setStage("success");
      setSubmitError(null);
      return result;
    } catch (err) {
      clearInterval(stepTimer);
      setSubmitError(mapError(err));
      setStage("failed");
      return null;
    }
  };

  // ---- Render ------------------------------------------------------------
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
    return (
      <StudentCard>
        <h1 className="app-title">Student Attendance</h1>
        <p className="session-found">✓ Attendance Session Found</p>
        <p className="muted">This session is valid for 1 minute once you continue.</p>
        <button className="btn btn-primary btn-large" onClick={handleContinue}>
          Continue
        </button>
        {locationWarning && <p className="warning-text">{locationWarning}</p>}
        <ErrorBox message={submitError || authError} />
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
    const expired = expiredRef.current;
    return (
      <StudentCard>
        <h2 className="failed-title">Attendance Not Marked</h2>
        <p className="failed-reason">
          {submitError ||
            (expired
              ? "Attendance window expired. Please scan the current QR code again."
              : "Something went wrong.")}
        </p>
        {!expired && submitError && timeLeft > 0 && (
          <button className="btn btn-primary" onClick={() => setStage("form")}>
            Try Again
          </button>
        )}
        <p className="muted hint">If your QR expired, scan the current code again.</p>
      </StudentCard>
    );
  }

  // stage === "form"
  const windowExpired = timeLeft <= 0;
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
        <form onSubmit={handleSubmit} disabled={windowExpired}>
          <label>
            Name
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Your full name"
              required
              maxLength={120}
              disabled={windowExpired}
            />
          </label>
          <label>
            Enrollment No.
            <input
              type="text"
              value={enrollment}
              onChange={(e) => setEnrollment(e.target.value)}
              placeholder="e.g. MCA123"
              required
              maxLength={40}
              disabled={windowExpired}
            />
          </label>
          <button
            className="btn btn-primary btn-large"
            type="submit"
            disabled={windowExpired}
          >
            Mark Attendance
          </button>
        </form>
      )}

      {windowExpired && (
        <p className="failed-reason">
          Attendance window expired. Please scan the current QR code again.
        </p>
      )}
      {locationWarning && <p className="warning-text">{locationWarning}</p>}
      <ErrorBox message={authError} />
    </StudentCard>
  );
}

// ---- helpers -------------------------------------------------------------
function mapError(err) {
  if (err.code === "QR_EXPIRED") return "QR code expired.";
  if (err.code === "QR_INVALID") return "Invalid QR code. Please scan the current QR.";
  if (err.code === "SESSION_NOT_ACTIVE") return "Attendance session has ended.";
  if (err.code === "ATTENDANCE_WINDOW_EXPIRED")
    return "Attendance window expired. Please scan the current QR code again.";
  if (err.code === "STUDENT_NOT_FOUND")
    return "Student details do not match our records.";
  if (err.code === "OUTSIDE_LOCATION") return "You are outside the attendance location.";
  if (err.code === "ALREADY_MARKED") return "Attendance has already been marked.";
  if (err.code === "PERMISSION_DENIED") return "Location permission is required.";
  return err.message || "Attendance could not be verified. Please try again.";
}

function StudentCard({ title, body, hint, children }) {
  return (
    <div className="page-center">
      <div className="card student-card">
        {title && <h1 className="app-title">{title}</h1>}
        {body && <p>{body}</p>}
        {hint && <p className="muted hint">{hint}</p>}
        {children}
      </div>
    </div>
  );
}
