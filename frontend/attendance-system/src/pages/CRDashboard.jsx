// CR Dashboard: start session -> live rotating QR (10s) -> end session.
// The QR only carries a random short-lived token URL — never JWTs,
// ID tokens or personal data. All validity decisions are made by FastAPI.
import { useCallback, useEffect, useRef, useState } from "react";
import { QRCodeCanvas } from "qrcode.react";
import * as api from "../services/api";
import { useAuth } from "../context/AuthContext";
import { ErrorBox, ProgressBar } from "../components/ui";
import { getCurrentLocation } from "../utils/geolocation";

const REFRESH_SECONDS_FALLBACK = 10;

export default function CRDashboard() {
  const { profile, logout } = useAuth();
  // idle -> starting -> active -> ending -> ended
  const [stage, setStage] = useState("idle");
  const [session, setSession] = useState(null); // { session_id, qr: {...} }
  const [countdown, setCountdown] = useState(0);
  const [startProgress, setStartProgress] = useState(0);
  const [error, setError] = useState(null);
  const timerRef = useRef(null);

  const stopClock = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => stopClock, [stopClock]);

  // 10-second QR rotation clock: refresh via backend when it hits zero.
  const startClock = useCallback(
    (sessionId, seconds) => {
      stopClock();
      setCountdown(seconds);
      timerRef.current = setInterval(async () => {
        setCountdown((prev) => {
          if (prev > 1) return prev - 1;
          // Fire refresh; the new token restarts the clock.
          api
            .refreshQR(sessionId)
            .then((data) => {
              setSession((current) =>
                current ? { ...current, qr: data.qr } : current
              );
              setError(null);
            })
            .catch((err) => {
              if (err.code === "SESSION_NOT_ACTIVE") {
                setStage("ended");
                stopClock();
              } else {
                setError(err.message);
              }
            });
          return seconds; // avoid negative display while awaiting the response
        });
      }, 1000);
    },
    [stopClock]
  );

  const handleStart = async () => {
    setError(null);
    setStage("starting");
    setStartProgress(8);
    const progressTimer = setInterval(
      () => setStartProgress((p) => Math.min(p + 12, 92)),
      250
    );
    try {
      // Fresh browser location for the session anchor point.
      const location = await getCurrentLocation();
      const data = await api.startAttendance(location.latitude, location.longitude);
      setSession(data);
      setStage("active");
      startClock(
        data.session_id,
        data.qr.countdown_seconds || data.qr.expires_in_seconds || REFRESH_SECONDS_FALLBACK
      );
    } catch (err) {
      setError(err.message);
      setStage("idle");
    } finally {
      clearInterval(progressTimer);
      setStartProgress(100);
    }
  };

  const handleEnd = async () => {
    if (!session?.session_id) return;
    setError(null);
    setStage("ending");
    try {
      await api.endAttendance(session.session_id);
      stopClock();
      setStage("ended");
      setSession(null);
    } catch (err) {
      setError(err.message);
      setStage("active");
    }
  };

  return (
    <div className="page-center">
      <div className="card cr-card">
        <header className="cr-header">
          <div>
            <h1 className="app-title">Student Attendance System</h1>
            <p className="app-subtitle">
              Welcome, {profile?.name || profile?.email || "CR"}
            </p>
          </div>
          <button className="btn btn-ghost" onClick={logout}>
            Sign out
          </button>
        </header>

        {stage === "idle" && (
          <button className="btn btn-primary btn-large" onClick={handleStart}>
            Start Session Attendance
          </button>
        )}

        {stage === "starting" && (
          <div className="starting-block">
            <p>Starting Attendance Session…</p>
            <ProgressBar percent={startProgress} />
          </div>
        )}

        {(stage === "active" || stage === "ending") && session?.qr && (
          <div className="qr-block">
            <h2 className="session-active-label">Attendance Session Active</h2>
            <div className="qr-frame">
              <QRCodeCanvas value={session.qr.qr_url} size={224} marginSize={2} />
            </div>
            <p className="session-status">Session Status: Active</p>
            <p className="qr-countdown">QR refreshes in: {countdown} seconds</p>
            <p className="session-id muted">Session ID: {session.session_id}</p>
            <button
              className="btn btn-danger btn-large"
              onClick={handleEnd}
              disabled={stage === "ending"}
            >
              {stage === "ending" ? "Ending…" : "End Session"}
            </button>
          </div>
        )}

        {stage === "ended" && (
          <div className="ended-block">
            <h2>Attendance Session Ended</h2>
            <button className="btn btn-primary btn-large" onClick={handleStart}>
              Start New Session
            </button>
          </div>
        )}

        <ErrorBox message={error} />
      </div>
    </div>
  );
}
