// CR Dashboard: rejoin the single shared active session (or start one with
// a session NAME) -> live rotating QR (CR-adjustable lifetime, identical for
// every CR) -> water-fill stats polled from the backend (plus same-device/IP
// proxy warnings) -> end session -> server-computed summary with the
// absentee list sorted by enrollment, CSV download and clipboard copy.
// The QR only carries a random short-lived token URL — never JWTs,
// ID tokens or personal data. All validity decisions are made by FastAPI.
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { QRCodeCanvas } from "qrcode.react";
import * as api from "../services/api";
import { useAuth } from "../context/AuthContext";
import { BrandMark, ErrorBox, GoogleIcon, ProgressBar } from "../components/ui";
import { getCurrentLocation } from "../utils/geolocation";

const REFRESH_SECONDS_FALLBACK = 10;
// Live stats poll every 15 s: one interval per active session, stopped when the
// session ends and when this dashboard unmounts (see stopClock).
const STATS_POLL_MS = 15000;
const POPUP_LIFETIME_MS = 4200;
// Mirrors the backend allow-list (settings.QR_ALLOWED_LIFETIME_SECONDS) —
// the server validates every value again, this only shapes the dropdown.
// `0` is the backend's Permanent sentinel (never auto-expires; only the CR's
// manual refresh changes it).
const QR_LIFETIME_PERMANENT = 0;
const QR_LIFETIME_OPTIONS = [5, 10, 15, 20, 30, 60];

// A QR payload is "Permanent" when the backend flags it or reports a 0s life.
const isPermanentQr = (qr) =>
  !!qr && (qr.is_permanent === true || qr.expires_in_seconds === 0);

// Live "Students Present" panel: how many rows to show before "View all", and
// how long a freshly-arrived row keeps its highlight. Both are display-only —
// the list itself is driven by the SAME /stats poll as the water-fill (no extra
// request, no second timer).
const PRESENT_PREVIEW_COUNT = 6;
const PRESENT_HIGHLIGHT_MS = 2800;

// Server stores marked_at as a UTC ISO string; show it as a short local time.
const formatMarkedTime = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
};

// Ellipsize a string so it fits a canvas width, using the CURRENT ctx.font.
// Used when compositing the downloadable QR image (session names vary in length).
function fitText(ctx, text, maxWidth) {
  const value = String(text ?? "");
  if (ctx.measureText(value).width <= maxWidth) return value;
  let truncated = value;
  while (truncated.length > 1 && ctx.measureText(`${truncated}…`).width > maxWidth) {
    truncated = truncated.slice(0, -1);
  }
  return `${truncated.trimEnd()}…`;
}

// Absentee CSV (backend-verified data). Quoted fields + BOM so enrollment
// numbers with leading zeros open correctly in Excel.
function downloadAbsenteeCSV(summary) {
  const rows = [["Enrollment Number", "Student Name"]];
  for (const a of summary.absentees || []) rows.push([a.enrollment_no, a.name]);
  const csv =
    "\ufeff" +
    rows
      .map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(","))
      .join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `absentees-${summary.session_id}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

export default function CRDashboard() {
  const { profile, role, logout } = useAuth();
  const navigate = useNavigate();
  // loading -> idle -> starting -> active -> ending -> ended
  const [stage, setStage] = useState("loading");
  const [session, setSession] = useState(null); // { session_id, qr: {...} }
  const [stats, setStats] = useState(null);     // backend jar-fill data
  const [summary, setSummary] = useState(null); // backend end-of-session data
  const [popups, setPopups] = useState([]);     // "Rahul Sharma ✓" floats (+ Gmail avatar)
  const [countdown, setCountdown] = useState(0);
  const [startProgress, setStartProgress] = useState(0);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false); // disables Start/End after first click
  const [sessionName, setSessionName] = useState(""); // CR-entered, required
  const [qrLifetime, setQrLifetime] = useState(REFRESH_SECONDS_FALLBACK);
  const [lifetimeSaving, setLifetimeSaving] = useState(false);
  const [lifetimeNotice, setLifetimeNotice] = useState(null);
  const [qrRefreshing, setQrRefreshing] = useState(false); // manual QR refresh
  const [copied, setCopied] = useState(false); // "Copied!" on the absentee list
  const [presentCopied, setPresentCopied] = useState(false); // "Copied!" on the present list
  const qrFrameRef = useRef(null); // wraps the QR canvas, read when downloading
  const timerRef = useRef(null);
  const tickRef = useRef(0);
  const statsRef = useRef(null);
  const seenRef = useRef(new Set()); // recent-events already popped up
  const busyRef = useRef(false);     // blocks duplicate async invocations
  // Live "Students Present" panel state (driven by the existing /stats poll).
  const [showAllPresent, setShowAllPresent] = useState(false);
  const [highlighted, setHighlighted] = useState(() => new Set()); // enrollments
  const knownEnrollRef = useRef(new Set()); // enrollments already listed
  const presentInitRef = useRef(false); // first batch seeded without animating

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

  // Stop ONLY the QR auto-rotation clock (used for Permanent mode); the live
  // stats polling keeps running so the water-fill/popups stay live.
  const stopRotationClock = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
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
    knownEnrollRef.current = new Set();
    presentInitRef.current = false;
    setHighlighted(new Set());
    setShowAllPresent(false);
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
          // Reuse this same response to flag newly-arrived students in the
          // "Students Present" panel — enrollment is the unique key, so a
          // student can never be listed (or highlighted) twice. The very first
          // batch is seeded silently so an open session doesn't animate wholesale.
          const attendees = s.attendees || [];
          if (!presentInitRef.current) {
            attendees.forEach((a) => a.enrollment_no && knownEnrollRef.current.add(a.enrollment_no));
            presentInitRef.current = true;
          } else {
            const fresh = attendees
              .map((a) => a.enrollment_no)
              .filter((e) => e && !knownEnrollRef.current.has(e));
            if (fresh.length) {
              fresh.forEach((e) => knownEnrollRef.current.add(e));
              setHighlighted((prev) => new Set([...prev, ...fresh]));
              setTimeout(() => {
                setHighlighted((prev) => {
                  const next = new Set(prev);
                  fresh.forEach((e) => next.delete(e));
                  return next;
                });
              }, PRESENT_HIGHLIGHT_MS);
            }
          }
          if (s.session_status !== "active") {
            // Another CR ended the shared session — reflect it everywhere.
            await showFinalSummary(sessionId);
            return;
          }
          // Float a popup for each newly recorded arrival (backend data only).
          // `seenRef` guarantees one popup per attendance event, so the slower
          // 15 s polling can never repeat a student's arrival.
          for (const event of s.recent || []) {
            const key = `${event.name}|${event.marked_at}`;
            if (!seenRef.current.has(key)) {
              seenRef.current.add(key);
              const id = `${key}|${Date.now()}`;
              setPopups((current) => [
                ...current.slice(-5),
                { id, name: event.name, photoUrl: event.photo_url || "" },
              ]);
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
          setSession({
            session_id: data.session.session_id,
            session_name: data.session.session_name || "",
            qr: data.qr,
          });
          const rawLifetime = data.session.qr_lifetime_seconds;
          setQrLifetime(rawLifetime == null ? REFRESH_SECONDS_FALLBACK : rawLifetime);
          if (data.stats) setStats(data.stats);
          setStage("active");
          if (isPermanentQr(data.qr)) {
            // Permanent: no auto-rotation clock; CR refreshes manually.
            stopRotationClock();
            setCountdown(0);
          } else {
            startClock(
              data.session.session_id,
              data.qr.countdown_seconds || data.qr.expires_in_seconds || REFRESH_SECONDS_FALLBACK
            );
          }
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
  }, [startClock, startStatsPolling, stopRotationClock]);

  const handleStart = async () => {
    if (busyRef.current) return; // first click wins — no duplicate requests
    const name = sessionName.trim();
    if (!name) {
      setError("Please enter a session name before starting.");
      return;
    }
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
      // Fresh browser location for the session anchor point. The session
      // NAME rides along but the backend still mints the session_id.
      const location = await getCurrentLocation();
      const data = await api.startAttendance(location.latitude, location.longitude, name);
      seenRef.current = new Set();
      knownEnrollRef.current = new Set();
      presentInitRef.current = false;
      setHighlighted(new Set());
      setShowAllPresent(false);
      setSession(data);
      setQrLifetime(isPermanentQr(data.qr) ? QR_LIFETIME_PERMANENT : (data.qr?.expires_in_seconds || REFRESH_SECONDS_FALLBACK));
      setSessionName("");
      setSummary(null);
      setStage("active");
      if (isPermanentQr(data.qr)) {
        stopRotationClock();
        setCountdown(0);
      } else {
        startClock(
          data.session_id,
          data.qr.countdown_seconds || data.qr.expires_in_seconds || REFRESH_SECONDS_FALLBACK
        );
      }
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

  // CR changes how long FUTURE QR tokens live. The backend validates the
  // value against its allow-list. We then force-rotate immediately so the new
  // policy (especially Permanent) takes effect on the screen right away,
  // invalidating the previous QR.
  const handleLifetimeChange = async (seconds) => {
    if (!session?.session_id || lifetimeSaving) return;
    setLifetimeSaving(true);
    setLifetimeNotice(null);
    const previous = qrLifetime;
    setQrLifetime(seconds);
    const sessionId = session.session_id;
    try {
      const applied = await api.setQRLifetime(sessionId, seconds);
      const permanent = applied.qr_lifetime_seconds === QR_LIFETIME_PERMANENT;
      setQrLifetime(applied.qr_lifetime_seconds);
      // Mint a token under the new policy now (invalidates the old QR).
      const rotated = await api.rotateQR(sessionId);
      setSession((current) => (current ? { ...current, qr: rotated.qr } : current));
      if (permanent || isPermanentQr(rotated.qr)) {
        stopRotationClock();
        setCountdown(0);
        setLifetimeNotice(
          "QR is now Permanent — it stays valid until you refresh it manually."
        );
      } else {
        startClock(
          sessionId,
          rotated.qr.countdown_seconds ||
            rotated.qr.expires_in_seconds ||
            REFRESH_SECONDS_FALLBACK
        );
        setLifetimeNotice(`QR now valid for ${applied.qr_lifetime_seconds}s.`);
      }
    } catch (err) {
      setQrLifetime(previous);
      setError(err.message);
    } finally {
      setLifetimeSaving(false);
    }
  };

  // CR presses the refresh icon (Permanent mode): issue a brand-new QR and
  // invalidate the one currently on screen.
  const handleManualRefresh = async () => {
    if (!session?.session_id || qrRefreshing) return;
    setQrRefreshing(true);
    setLifetimeNotice(null);
    try {
      const data = await api.rotateQR(session.session_id);
      setSession((current) => (current ? { ...current, qr: data.qr } : current));
      if (isPermanentQr(data.qr)) {
        stopRotationClock();
        setCountdown(0);
      } else {
        startClock(
          session.session_id,
          data.qr.countdown_seconds ||
            data.qr.expires_in_seconds ||
            REFRESH_SECONDS_FALLBACK
        );
      }
      setLifetimeNotice("QR refreshed — the previous code no longer works.");
    } catch (err) {
      setError(err.message);
    } finally {
      setQrRefreshing(false);
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
      knownEnrollRef.current = new Set();
      presentInitRef.current = false;
      setHighlighted(new Set());
      setShowAllPresent(false);
      setStage("ended");
    } catch (err) {
      setError(err.message);
      setStage("active");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // Screen-filling water: the whole viewport rises like water based on the
  // backend-computed attendance percentage (fixed layer behind the card).
  const renderWater = () => {
    if (!stats) return null;
    const percent = Math.max(0, Math.min(100, stats.percentage ?? 0));
    return (
      <div className="water-bg" aria-hidden="true">
        <div className="water" style={{ height: `${percent}%` }}>
          <div className="water-wave wave-a" />
          <div className="water-wave wave-b" />
        </div>
      </div>
    );
  };

  const renderLiveStats = () => {
    if (!stats) return null;
    const percent = Math.max(0, Math.min(100, stats.percentage ?? 0));
    return (
      <div className="live-stats">
        <span className="live-count">
          {stats.present_count} / {stats.total_students} Present
        </span>
        <span className="live-percent">{percent.toFixed(1)}%</span>
      </div>
    );
  };

  // Backend-computed proxy-attendance flags (never raw IPs, never student data).
  const renderWarnings = () => {
    const warnings = stats?.warnings || [];
    if (warnings.length === 0) return null;
    return (
      <div className="session-warnings" role="alert">
        {warnings.map((w) => (
          <p key={w}>{w}</p>
        ))}
      </div>
    );
  };

  // Live "Students Present" panel. Rendered entirely from the SAME /stats
  // response that drives the water-fill — no separate request, no second timer.
  // Most recent arrival is on top; new arrivals get a one-off highlight.
  const renderAttendees = () => {
    if (!stats) {
      return (
        <section className="present-panel" aria-busy="true">
          <header className="present-panel-head">
            <h3 className="present-panel-title">Students Present</h3>
            <span className="present-live">
              <span className="present-live-dot" aria-hidden="true" />Live
            </span>
          </header>
          <p className="present-loading">Loading attendance…</p>
        </section>
      );
    }
    const attendees = stats.attendees || [];
    const present = stats.present_count ?? attendees.length;
    const total = stats.total_students ?? 0;
    const collapsed = attendees.slice(0, PRESENT_PREVIEW_COUNT);
    const rows = showAllPresent ? attendees : collapsed;
    return (
      <section className="present-panel" aria-live="polite">
        <header className="present-panel-head">
          <h3 className="present-panel-title">Students Present</h3>
          <div className="present-panel-meta">
            <span className="present-live">
              <span className="present-live-dot" aria-hidden="true" />Live
            </span>
            <span className="present-panel-count">
              {present} / {total}
            </span>
            <button
              type="button"
              className="btn btn-ghost present-copy-btn"
              onClick={copyPresentList}
              disabled={attendees.length === 0}
              title={presentCopied ? "Copied!" : "Copy the students-present list"}
              aria-label="Copy the students-present list"
            >
              {presentCopied ? (
                <svg
                  className="btn-icon"
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              ) : (
                <svg
                  className="btn-icon"
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <rect x="9" y="9" width="11" height="11" rx="2" />
                  <path d="M5 15V5a2 2 0 0 1 2-2h10" />
                </svg>
              )}
              {presentCopied ? "Copied" : "Copy"}
            </button>
          </div>
        </header>

        {attendees.length === 0 ? (
          <div className="present-empty">
            <p className="present-empty-title">No students have marked yet.</p>
            <p className="present-empty-sub">They will appear here as they scan the QR.</p>
          </div>
        ) : (
          <>
            <ul className="present-list">
              {rows.map((a, i) => (
                <li
                  key={a.enrollment_no || `${a.name}-${a.marked_at}-${i}`}
                  className={
                    highlighted.has(a.enrollment_no)
                      ? "present-row present-row-new"
                      : "present-row"
                  }
                >
                  <span className="present-row-idx">{i + 1}</span>
                  <span className="present-row-name">{a.name || "Student"}</span>
                  <span className="present-row-enroll">{a.enrollment_no}</span>
                  <span className="present-row-time">{formatMarkedTime(a.marked_at)}</span>
                </li>
              ))}
            </ul>
            {attendees.length > PRESENT_PREVIEW_COUNT && (
              <button
                type="button"
                className="btn btn-ghost btn-small present-toggle"
                onClick={() => setShowAllPresent((v) => !v)}
              >
                {showAllPresent ? "Show less" : `View all (${attendees.length})`}
              </button>
            )}
          </>
        )}
      </section>
    );
  };

  const renderPopups = () => (
    /* "Student present" floats: start at the BOTTOM edge of the viewport and
       travel up towards the top-right corner (backend-sourced; pointer-events
       none so they never block CR controls). Staggered delays keep simultaneous
       arrivals from completely overlapping. The avatar is the student's own
       Google profile picture; when it is missing or fails to load the Google
       icon is shown instead. */
    <div className="popup-layer" aria-live="polite">
      {popups.map((popup, index) => (
        <div
          key={popup.id}
          className="present-popup"
          style={{ animationDelay: `${index * 0.18}s` }}
        >
          {popup.photoUrl ? (
            <img
              className="present-popup-avatar"
              src={popup.photoUrl}
              alt=""
              referrerPolicy="no-referrer"
              onError={(event) => {
                // Hide the broken image and reveal the Google fallback icon.
                event.currentTarget.style.display = "none";
                const fallback = event.currentTarget.nextElementSibling;
                if (fallback) fallback.style.display = "inline-flex";
              }}
            />
          ) : null}
          <span
            className="present-popup-avatar present-popup-avatar-fallback"
            style={{ display: popup.photoUrl ? "none" : "inline-flex" }}
          >
            <GoogleIcon />
          </span>
          <span className="present-popup-name">{popup.name}</span>
          <span className="present-popup-check">✓</span>
        </div>
      ))}
    </div>
  );

  const copyAbsenteeNames = async () => {
    const names = (summary?.absentees || []).map((a) => a.name).join("\n");
    try {
      await navigator.clipboard.writeText(names);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Could not copy to the clipboard on this device.");
    }
  };

  // Copy the live "Students Present" list (ranked, with enrollment + time) so
  // the CR can paste it elsewhere. Built entirely from the current /stats data.
  const copyPresentList = async () => {
    const attendees = stats?.attendees || [];
    if (attendees.length === 0) {
      setError("No students have marked attendance yet to copy.");
      return;
    }
    const heading = `Students Present (${stats.present_count ?? attendees.length}/${
      stats.total_students ?? attendees.length
    }) — ${stats?.session_name || session?.session_name || "Session"}`;
    const lines = attendees.map(
      (a, i) =>
        `${i + 1}. ${a.name || "Student"}\t${a.enrollment_no || ""}\t${formatMarkedTime(a.marked_at)}`
    );
    try {
      await navigator.clipboard.writeText([heading, ...lines].join("\n"));
      setPresentCopied(true);
      setTimeout(() => setPresentCopied(false), 2000);
    } catch {
      setError("Could not copy to the clipboard on this device.");
    }
  };

  // Render the QR to a PNG card that also shows the session title + name, then
  // download it. The QR canvas on screen is composited onto a fresh canvas so
  // the saved image is self-describing (not just a bare code).
  const handleDownloadQR = () => {
    const source = qrFrameRef.current?.querySelector("canvas");
    if (!source || !session?.qr) {
      setError("The QR code is not ready to download yet.");
      return;
    }
    const name = stats?.session_name || session?.session_name || "Session";
    const scale = 2; // 2x for a crisp image
    const W = 360;
    const PAD = 28;
    const QR = 224;
    const titleH = 30;
    const nameH = 26;
    const footerH = 22;
    const gap = 18;
    const H = PAD + titleH + gap + nameH + gap + QR + gap + footerH + PAD;

    const out = document.createElement("canvas");
    out.width = W * scale;
    out.height = H * scale;
    const ctx = out.getContext("2d");
    if (!ctx) {
      setError("Could not create the QR image on this device.");
      return;
    }
    ctx.scale(scale, scale);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    // Background + thin navy→gold top accent bar (matches the on-screen card).
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "#123a66";
    ctx.fillRect(0, 0, W, 6);

    // Title.
    ctx.fillStyle = "#0d2f57";
    ctx.font = "bold 20px system-ui, -apple-system, Segoe UI, Arial, sans-serif";
    ctx.fillText("Attendance Session Active", W / 2, PAD + titleH / 2);

    // Session name (ellipsized to fit).
    ctx.fillStyle = "#17497f";
    ctx.font = "600 17px system-ui, -apple-system, Segoe UI, Arial, sans-serif";
    const nameY = PAD + titleH + gap + nameH / 2;
    ctx.fillText(fitText(ctx, name, W - PAD * 2), W / 2, nameY);

    // QR (centered, scaled from the live canvas).
    const qrX = (W - QR) / 2;
    const qrY = PAD + titleH + gap + nameH + gap;
    ctx.drawImage(source, qrX, qrY, QR, QR);

    // Footer: session id.
    ctx.fillStyle = "#5c6b7d";
    ctx.font = "12px ui-monospace, SFMono-Regular, Menlo, monospace";
    const footerY = qrY + QR + gap + footerH / 2;
    ctx.fillText(fitText(ctx, `Session ID: ${session.session_id}`, W - PAD * 2), W / 2, footerY);

    const link = document.createElement("a");
    link.href = out.toDataURL("image/png");
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || session.session_id;
    link.download = `attendance-qr-${slug}.png`;
    link.click();
  };

  const renderSummary = () => {
    if (!summary) return null;
    return (
      <div className="summary-block">
        <h2>Attendance Session Ended</h2>
        {summary.session_name && <p className="summary-name">{summary.session_name}</p>}
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
        {/* Sorted ASCENDING by enrollment number (backend order preserved). */}
        <table className="absentee-table">
          <thead>
            <tr>
              <th>Enrollment No</th>
              <th>Name</th>
            </tr>
          </thead>
          <tbody>
            {summary.absentees.length === 0 && (
              <tr>
                <td colSpan={2} className="absentee-empty">
                  No absentees — everyone marked present.
                </td>
              </tr>
            )}
            {summary.absentees.map((student) => (
              <tr key={student.enrollment_no}>
                <td className="absentee-enr">{student.enrollment_no}</td>
                <td>{student.name}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {(summary.absentees?.length ?? 0) > 0 && (
          <div className="summary-actions">
            <button
              type="button"
              className="btn btn-primary btn-small"
              onClick={() => downloadAbsenteeCSV(summary)}
            >
              Download CSV
            </button>
            <button type="button" className="btn btn-ghost btn-small" onClick={copyAbsenteeNames}>
              {copied ? "Copied!" : "Copy Names"}
            </button>
          </div>
        )}

        <p className="summary-foot">
          Present Students: {summary.present_count} / {summary.total_students}
        </p>
      </div>
    );
  };

  // Small start form: the CR names the session (backend still generates
  // the authoritative session_id — the name is a human label only).
  const renderStartForm = (label) => (
    <form
      className="start-form"
      onSubmit={(e) => {
        e.preventDefault();
        handleStart();
      }}
    >
      <label className="start-form-label" htmlFor="session-name-input">
        Session Name
      </label>
      <input
        id="session-name-input"
        className="input"
        type="text"
        maxLength={80}
        placeholder="e.g. MCA Gen-AI - Morning Attendance"
        value={sessionName}
        onChange={(e) => setSessionName(e.target.value)}
        disabled={busy}
      />
      <button type="submit" className="btn btn-primary btn-large" disabled={busy}>
        {label}
      </button>
    </form>
  );

  const sessionLive = stage === "active" || stage === "ending";
  // Permanent QR: never auto-rotates, so the CR gets a manual refresh icon.
  const qrPermanent =
    sessionLive && !!session?.qr &&
    (isPermanentQr(session.qr) || qrLifetime === QR_LIFETIME_PERMANENT);

  return (
    <>
      {sessionLive && renderWater()}
      {sessionLive && renderPopups()}
      <div className="page-center">
        <div className="card cr-card">
          <header className="cr-header">
            <div className="brand-lockup brand-lockup-row">
              <BrandMark size={44} />
              <div className="brand-text">
                <h1 className="app-title brand-name">Attendify</h1>
                <p className="app-subtitle">
                  Welcome, {profile?.name || profile?.email || "CR"}
                </p>
              </div>
            </div>
            <div className="cr-header-actions">
              {role === "admin" && (
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => navigate("/admin")}
                >
                  Admin Dashboard
                </button>
              )}
              <button className="btn btn-ghost" onClick={logout}>
                Sign out
              </button>
            </div>
          </header>

          {stage === "loading" && <ProgressBar percent={40} />}

          {stage === "idle" && renderStartForm("Start Session Attendance")}

          {stage === "starting" && (
            <div className="starting-block">
              <p>Starting Attendance Session…</p>
              <ProgressBar percent={startProgress} />
            </div>
          )}

          {sessionLive && session?.qr && (
            <div className="qr-block">
              <h2 className="session-active-label">Attendance Session Active</h2>
              {(stats?.session_name || session.session_name) && (
                <p className="session-name">{stats?.session_name || session.session_name}</p>
              )}
              <div className="qr-frame" ref={qrFrameRef}>
                <QRCodeCanvas value={session.qr.qr_url} size={224} marginSize={2} />
              </div>
              <div className="qr-actions">
                <button
                  type="button"
                  className="btn btn-ghost btn-small qr-download-btn"
                  onClick={handleDownloadQR}
                  title="Download the QR code as an image"
                >
                  <svg
                    className="btn-icon"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M12 3v12" />
                    <path d="M7 10l5 5 5-5" />
                    <path d="M5 21h14" />
                  </svg>
                  Download QR
                </button>
              </div>
              {renderLiveStats()}
              {renderWarnings()}
              <p className="session-status">Session Status: Active</p>
              <div className="qr-lifetime-control">
                <label className="qr-lifetime-label" htmlFor="qr-lifetime-select">
                  QR Valid For
                </label>
                <select
                  id="qr-lifetime-select"
                  className="qr-lifetime-select"
                  value={qrLifetime}
                  onChange={(e) => handleLifetimeChange(Number(e.target.value))}
                  disabled={lifetimeSaving}
                >
                  <option value={QR_LIFETIME_PERMANENT}>Permanent</option>
                  {QR_LIFETIME_OPTIONS.map((seconds) => (
                    <option key={seconds} value={seconds}>
                      {seconds} seconds
                    </option>
                  ))}
                </select>
              </div>
              {lifetimeNotice && <p className="qr-lifetime-notice">{lifetimeNotice}</p>}
              {qrPermanent ? (
                <div className="qr-permanent-row">
                  <p className="qr-countdown qr-countdown-permanent">
                    QR is Permanent — it stays valid until you refresh it.
                  </p>
                  <button
                    type="button"
                    className="btn btn-ghost qr-refresh-btn"
                    onClick={handleManualRefresh}
                    disabled={qrRefreshing || lifetimeSaving}
                    title="Refresh the QR and invalidate the previous code"
                    aria-label="Refresh QR code"
                  >
                    <span
                      className={qrRefreshing ? "qr-refresh-icon spinning" : "qr-refresh-icon"}
                      aria-hidden="true"
                    >
                      ↻
                    </span>
                    Refresh QR
                  </button>
                </div>
              ) : (
                <p className="qr-countdown">QR refreshes in: {countdown} seconds</p>
              )}
              <p className="session-id muted">Session ID: {session.session_id}</p>
              {renderAttendees()}
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
              {renderStartForm("Start New Session")}
            </div>
          )}

          <ErrorBox message={error} />
        </div>
      </div>
    </>
  );
}
