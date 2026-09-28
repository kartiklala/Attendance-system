// Centralized FastAPI client. Every network call in the app goes through here.
// The application JWT is kept IN MEMORY and sent as an 'Authorization: Bearer'
// header on every protected request. The frontend (Firebase Hosting) and the
// backend (Render) are different sites in production, where browsers block the
// third-party HttpOnly cookie — the header is the reliable transport. The
// cookie is still set by the backend as a same-site/desktop fallback, so all
// requests keep sending credentials: "include".
const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

// Held only in memory (never localStorage) — refreshed on every /authorize-user.
let applicationJwt = null;

// Set by AuthContext: async () => refetch /authorize-user with a fresh
// Firebase ID token. Used to transparently recover when the 3-minute
// application JWT expires mid-session.
let recoverAuth = null;
// Set by AuthContext: called when re-authorization is impossible or was
// rejected — the frontend must drop all authenticated/session UI state and
// go back to the Google sign-in screen (never reuse an expired JWT).
let onAuthPermanent = null;
export function setAuthRecovery(fn, permanentFn) {
  recoverAuth = fn;
  onAuthPermanent = permanentFn;
}

async function request(path, { method = "GET", body, firebaseIdToken } = {}, isRetry = false) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (firebaseIdToken) headers.Authorization = `Bearer ${firebaseIdToken}`;
  else if (applicationJwt && path !== "/authorize-user")
    headers.Authorization = `Bearer ${applicationJwt}`;

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "include",
    });
  } catch {
    const err = new Error("Cannot reach the server. Please try again.");
    err.code = "NETWORK_ERROR";
    throw err;
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    /* empty/non-JSON body */
  }

  // Application JWT expired -> silently re-authorize once, then retry.
  // If recovery is impossible or fails, treat the user as unauthenticated
  // so stale screens (Continue buttons etc.) can never keep working.
  if (response.status === 401 && !isRetry && path !== "/authorize-user") {
    const recovered = recoverAuth ? await recoverAuth() : false;
    if (recovered) return request(path, { method, body, firebaseIdToken }, true);
    if (onAuthPermanent) onAuthPermanent();
  }

  if (!response.ok) {
    const err = new Error(data?.error?.message || `Request failed (${response.status})`);
    err.code = data?.error?.code || "API_ERROR";
    err.status = response.status;
    throw err;
  }
  return data;
}

// ---- Auth ---------------------------------------------------------------
export const authorizeUser = async (firebaseIdToken) => {
  const data = await request("/authorize-user", { method: "POST", firebaseIdToken });
  // Capture the bearer token so every subsequent protected call authenticates
  // via header even when the cross-site cookie is blocked.
  if (data?.application_token) applicationJwt = data.application_token;
  return data;
};

// True while the in-memory application JWT is still usable (checked with >30s
// of slack). Part of removing redundant authorization latency: the student flow
// only re-runs /authorize-user when this says the session is actually gone.
// The payload is decoded for its expiry claim only — authorization itself is
// always verified server-side, so an inaccurate local read is harmless.
export function hasValidApplicationToken() {
  if (!applicationJwt) return false;
  try {
    const part = applicationJwt.split(".")[1];
    if (!part) return false;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64.padEnd(Math.ceil(b64.length / 4) * 4, "=");
    const { exp } = JSON.parse(atob(padded));
    return typeof exp === "number" && exp * 1000 - Date.now() > 30_000;
  } catch {
    return false;
  }
}

export const logout = async () => {
  try {
    return await request("/logout", { method: "POST" });
  } finally {
    applicationJwt = null;
  }
};
export const getMe = () => request("/me");

// ---- CR session ---------------------------------------------------------
export const getActiveSession = () => request("/active-session");

// session_name is required by the backend (human label only; the session_id
// stays backend-generated). Latitude/longitude captured from the CR device.
export const startAttendance = (latitude, longitude, sessionName) =>
  request("/start-attendance", {
    method: "POST",
    body: { latitude, longitude, session_name: sessionName },
  });

export const refreshQR = (sessionId) =>
  request("/qr/refresh", { method: "POST", body: { session_id: sessionId } });

// CR requests a NEW lifetime for future QR rotations; the backend validates
// it against an allow-list (the frontend never decides QR validity).
export const setQRLifetime = (sessionId, lifetimeSeconds) =>
  request("/qr/lifetime", {
    method: "POST",
    body: { session_id: sessionId, lifetime_seconds: lifetimeSeconds },
  });

export const endAttendance = (sessionId) =>
  request("/end-attendance", { method: "POST", body: { session_id: sessionId } });

export const getSessionStats = (sessionId) =>
  request(`/sessions/${sessionId}/stats`);

export const getSessionSummary = (sessionId) =>
  request(`/sessions/${sessionId}/summary`);

// ---- Student ------------------------------------------------------------
// Entry gateway: fired the moment /attendance?token=… opens, BEFORE Google
// sign-in and before any location prompt. Public by design (no JWT), and the
// QR token is the only thing it sends — no name, enrollment or status.
// The backend answers with an opaque attempt id plus its own server-side
// completion window, so a QR rotating seconds later cannot invalidate the
// student's attempt.
export const startAttempt = (token) =>
  request("/attendance/attempt/start", { method: "POST", body: { token } });

// After authentication: the backend binds the attempt to the verified Firebase
// UID (identity is never claimed by the client) and returns the saved student
// identity in the same round trip.
export const bindAttempt = (attemptId) =>
  request("/attendance/attempt/bind", { method: "POST", body: { attempt_id: attemptId } });

// Whether this Google account already has a backend-verified enrollment.
export const getStudentMe = () => request("/student/me");

// Resolve the last 3 enrollment digits against Sheet1 (server-side lookup).
export const studentLookup = (last3) =>
  request("/student/lookup", { method: "POST", body: { last3 } });

// Confirm the match: backend verifies again and links enrollment to the UID.
export const studentConfirm = (last3) =>
  request("/student/confirm", { method: "POST", body: { last3 } });

// Identity comes from the verified enrollment saved against the UID, and the
// completion window from the bound attendance attempt — the request body
// carries no name, enrollment, QR token or status field at all.
export const checkAttendance = ({ attemptId, latitude, longitude }) =>
  request("/attendance/check", {
    method: "POST",
    body: {
      attempt_id: attemptId,
      latitude,
      longitude,
    },
  });

// ---- Admin --------------------------------------------------------------
// Protected FastAPI endpoints. A student/CR calling these is rejected 403 by
// the backend (role comes from the signed JWT) regardless of the UI.
export const listCRs = () => request("/admin/cr");

export const addCR = (email) =>
  request("/admin/cr", { method: "POST", body: { email } });

export default request;
