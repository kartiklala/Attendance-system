// CR Dashboard: rejoin the single shared active session (or start one)
// -> live rotating QR (10s, identical for every CR) -> jar-filling stats
// polled from the backend -> end session -> server-computed summary.
// The QR only carries a random short-lived token URL — never JWTs,
// ID tokens or personal data. All validity decisions are made by FastAPI.
import { useCallback, useEffect, useRef, useState } from "react";
import { QRCodeCanvas } from "qrcode.react";
import * as api from "../services/api";
import { useAuth } from "../context/AuthContext";
import { ErrorBox, ProgressBar } from "../components/ui";
import { getCurrentLocation } from "../utils/geolocation";

const REFRESH_SECONDS_FALLBACK = 10;
const STATS_POLL_MS = 5000; // reasonable interval — no hammering
const POPUP_LIFETIME_MS = 4200;

export default function CRDashboard() {
  const { profile, logout } = useAuth();
  // loading -> idle -> starting -> active -> ending -> ended
  const [stage, setStage] = useState("loading");
  const [session, setSession] = useState(null); // { session_id, qr: {...} }
  const [stats, setStats] = useState(null);     // backend jar-fill data
  const [summary, setSummary] = useState(null); // backend end-of-session data
  const [popups, setPopups] = useState([]);     // "Rahul Sharma ✓" floats
  const [countdown, setCountdown] = useState(0);
  const [startProgress, setStartProgress] = useState(0);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false); // disables Start/End after first click
  const timerRef = useRef(null);
  const tickRef = useRef(0);
  const statsRef = useRef(null);
  const seenRef = useRef(new Set()); // recent-events already popped up
  const busyRef = useRef(false);     // blocks duplicate async invocations

  const stopClock = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (statsRef.current) {
      clearInterval(statsRef.current);
      statsRef.current = null;
    }
  }, []);

  useEffect(() => stopClock, [stopClock]);

  const showFinalSummary = useCallback(async (sessionId) => {
    stopClock();
    try {
      const data = await api.getSessionSummary(sessionId);
      setSummary(data.summary);
    } catch {
      setSummary(null);
    }
    setSession(null);
    setStats(null);
    setStage("ended");
  }, [stopClock]);

  // Poll the backend for live present/total counts (jar fill + popups).
  const startStatsPolling = useCallback(
    (sessionId) => {
      if (statsRef.current) clearInterval(statsRef.current);
      const pull = async () => {
        try {
          const data = await api.getSessionStats(sessionId);
          const s = data.stats;
          setStats(s);
          setError(null);
          if (s.session_status !== "active") {
            // Another CR ended the shared session — reflect it everywhere.
            await showFinalSummary(sessionId);
            return;
          }
          // Float a popup for each newly recorded arrival (backend data only).
          for (const event of s.recent || []) {
            const key = `${event.name}|${event.marked_at}`;
            if (!seenRef.current.has(key)) {
              seenRef.current.add(key);
              const id = `${key}|${Date.now()}`;
              setPopups((current) => [...current.slice(-5), { id, name: event.name }]);
              setTimeout(
                () => setPopups((current) => current.filter((p) => p.id !== id)),
                POPUP_LIFETIME_MS
              );
            }
          }
        } catch (err) {
          if (err.code === "SESSION_NOT_ACTIVE") {
            await showFinalSummary(sessionId);
          } else if (err.code !== "UNAUTHORIZED") {
            setError(err.message);
          }
        }
      };
      pull();
      statsRef.current = setInterval(pull, STATS_POLL_MS);
    },
    [showFinalSummary]
  );

  // 10-second QR rotation clock: refresh via backend when it hits zero.
  // get_current_qr only rotates when the stored token expired, so every CR
  // polling the same session always renders the SAME QR.
  const startClock = useCallback(
    (sessionId, seconds) => {
      if (timerRef.current) clearInterval(timerRef.current);
      tickRef.current = seconds;
      setCountdown(seconds);
      timerRef.current = setInterval(() => {
        if (tickRef.current > 1) {
          tickRef.current -= 1;
          setCountdown(tickRef.current);
          return;
        }
        // Fire refresh; the new token restarts the clock.
        api
          .refreshQR(sessionId)
          .then((data) => {
            setSession((current) =>
              current ? { ...current, qr: data.qr } : current
            );
            tickRef.current =
              data.qr.countdown_seconds ||
              data.qr.expires_in_seconds ||
              REFRESH_SECONDS_FALLBACK;
            setCountdown(tickRef.current);
            setError(null);
          })
          .catch((err) => {
            if (err.code === "SESSION_NOT_ACTIVE") {
              showFinalSummary(sessionId);
            } else {
              setError(err.message);
              tickRef.current = seconds; // retry on the next tick
            }
          });
        setCountdown(seconds > 1 ? seconds : 1); // avoid showing 0 briefly
      }, 1000);
    },
    [showFinalSummary]
  );

  // On mount (and after re-authorization): rejoin the shared active session
  // if the backend says one exists — the backend is the source of truth.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getActiveSession();
        if (cancelled) return;
        if (data.session && data.qr) {
          setSession({ session_id: data.session.session_id, qr: data.qr });
          if (data.stats) setStats(data.stats);
          setStage("active");
          startClock(
            data.session.session_id,
            data.qr.countdown_seconds || data.qr.expires_in_seconds || REFRESH_SECONDS_FALLBACK
          );
          startStatsPolling(data.session.session_id);
        } else if (!cancelled) {
          setStage("idle");
        }
      } catch (err) {
        if (!cancelled) {
          // 401 is handled globally (re-auth / forced sign-in).
          if (err.code === "UNAUTHORIZED") return;
          setError(err.message);
          setStage("idle");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [startClock, startStatsPolling]);

  const handleStart = async () => {
    if (busyRef.current) return; // first click wins — no duplicate requests
    busyRef.current = true;
    setBusy(true);
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
      seenRef.current = new Set();
      setSession(data);
      setStage("active");
      startClock(
        data.session_id,
        data.qr.countdown_seconds || data.qr.expires_in_seconds || REFRESH_SECONDS_FALLBACK
      );
      startStatsPolling(data.session_id);
    } catch (err) {
      setError(err.message);
      setStage("idle");
    } finally {
      clearInterval(progressTimer);
      setStartProgress(100);
      busyRef.current = false;
      setBusy(false);
    }
  };

  const handleEnd = async () => {
    if (!session?.session_id || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setStage("ending");
    try {
      const data = await api.endAttendance(session.session_id);
      stopClock();
      setSummary(data.summary || null);
      setSession(null);
      setStats(null);
      setPopups([]);
      setStage("ended");
    } catch (err) {
      setError(err.message);
      setStage("active");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const renderJar = () => {
    if (!stats) return null;
    const percent = Math.max(0, Math.min(100, stats.percentage ?? 0));
    return (
      <div className="jar-section">
        <div className="jar-wrap" aria-hidden="true">
          <div className="jar-lid" />
          <div className="jar-neck" />
          <div className="jar-body">
            <div className="jar-fill" style={{ height: `${percent}%` }}>
              <div className="jar-surface" />
            </div>
            <div className="jar-label">
              {stats.present_count} / {stats.total_students}
            </div>
          </div>
        </div>
        <div className="jar-stats">
          <p className="jar-count">
            {stats.present_count} / {stats.total_students} Present
          </p>
          <p className="jar-percent">{percent.toFixed(1)}%</p>
        </div>
        {/* Floating "student present" notifications (backend-sourced). */}
        <div className="popup-layer" aria-live="polite">
          {popups.map((popup) => (
            <div key={popup.id} className="present-popup">
              {popup.name} ✓
            </div>
          ))}
        </div>
      </div>
    );
  };

  const renderSummary = () => {
    if (!summary) return null;
    return (
      <div className="summary-block">
        <h2>Attendance Session Ended</h2>
        <div className="summary-grid">
          <div className="summary-stat">
            <span className="summary-stat-value">{summary.total_students}</span>
            <span className="summary-stat-label">Total Students</span>
          </div>
          <div className="summary-stat present">
            <span className="summary-stat-value">{summary.present_count}</span>
            <span className="summary-stat-label">Present</span>
          </div>
          <div className="summary-stat absent">
            <span className="summary-stat-value">{summary.absent_count}</span>
            <span className="summary-stat-label">Absent</span>
          </div>
          <div className="summary-stat">
            <span className="summary-stat-value">{summary.percentage.toFixed(2)}%</span>
            <span className="summary-stat-label">Attendance</span>
          </div>
        </div>

        <h3 className="absentee-heading">Absent Students</h3>
        <ol className="absentee-list">
          {summary.absentees.length === 0 && (
            <li className="absentee-empty">No absentees — everyone marked present.</li>
          )}
          {summary.absentees.map((student) => (
            <li key={student.enrollment_no}>
              {student.name} — {student.enrollment_no}
            </li>
          ))}
        </ol>

        <p className="summary-foot">
          Present Students: {summary.present_count} / {summary.total_students}
        </p>
      </div>
    );
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

        {stage === "loading" && <ProgressBar percent={40} />}

        {stage === "idle" && (
          <button
            className="btn btn-primary btn-large"
            onClick={handleStart}
            disabled={busy}
          >
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
            <div className="active-split">
              <div className="qr-side">
                <div className="qr-frame">
                  <QRCodeCanvas value={session.qr.qr_url} size={224} marginSize={2} />
                </div>
                <p className="session-status">Session Status: Active</p>
                <p className="qr-countdown">QR refreshes in: {countdown} seconds</p>
                <p className="session-id muted">Session ID: {session.session_id}</p>
              </div>
              <div className="jar-side">{renderJar()}</div>
            </div>
            <button
              className="btn btn-danger btn-large"
              onClick={handleEnd}
              disabled={stage === "ending" || busy}
            >
              {stage === "ending" ? "Processing…" : "End Session"}
            </button>
          </div>
        )}

        {stage === "ended" && (
          <div className="ended-block">
            {renderSummary()}
            <button
              className="btn btn-primary btn-large"
              onClick={handleStart}
              disabled={busy}
            >
              Start New Session
            </button>
          </div>
        )}

        <ErrorBox message={error} />
      </div>
    </div>
  );
}
