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

// Render spins the free backend instance down when it has been idle. The first
// business request then hits a cold / gateway 404, and the retry chain in
// `request` turned that single logical call into a burst of duplicate POSTs
// (this is exactly why /attendance/verify-token was firing three times). Waking
// the instance up front with a cheap, idempotent GET /health means the real
// request runs once against an already-warm backend. Best-effort only: it never
// throws and gives up after a few seconds so a slow boot can't hang the UI.
export async function warmUp() {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const res = await fetch(`${API_BASE}/health`, { method: "GET", credentials: "include" });
      if (res.ok) return true;
    } catch {
      /* still booting / connection refused — keep polling */
    }
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
  return false;
}

async function request(path, { method = "GET", body, firebaseIdToken } = {}, meta = {}) {
  const { coldRetried = false, authRetried = false } = meta;
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (firebaseIdToken) headers.Authorization = `Bearer ${firebaseIdToken}`;
  else if (applicationJwt && path !== "/authorize-user")
    headers.Authorization = `Bearer ${applicationJwt}`;

  const retryOpts = { method, body, firebaseIdToken };

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "include",
    });
  } catch {
    // Connection blip / backend cold start (Render spins the free instance
    // down when idle): retry ONCE after a short wait before surfacing.
    if (!coldRetried && path !== "/authorize-user") {
      await new Promise((r) => setTimeout(r, 1600));
      return request(path, retryOpts, { ...meta, coldRetried: true });
    }
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

  // Cold-start / gateway transient (404/408/425/429/5xx). Render's edge can
  // answer the very first request with a bare 404/502 while the app boots.
  // We retry ONLY when the body is NOT a structured FastAPI error (our real
  // errors always carry `error.code`), so genuine 404s (e.g. an unknown
  // session) are surfaced immediately instead of being retried.
  const transient = [404, 408, 425, 429, 502, 503, 504].includes(response.status);
  if (
    !response.ok &&
    transient &&
    !coldRetried &&
    !(data && data.error) &&
    path !== "/authorize-user"
  ) {
    await new Promise((r) => setTimeout(r, 1600));
    return request(path, retryOpts, { ...meta, coldRetried: true });
  }

  // Application JWT expired -> silently re-authorize once, then retry.
  // If recovery is impossible or fails, treat the user as unauthenticated
  // so stale screens (Continue buttons etc.) can never keep working.
  if (response.status === 401 && !authRetried && path !== "/authorize-user") {
    const recovered = recoverAuth ? await recoverAuth() : false;
    if (recovered) return request(path, retryOpts, { ...meta, authRetried: true });
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

// Force a NEW QR token immediately (invalidates the previous one). Used by
// the CR's manual "Refresh QR" control and to apply a lifetime change at once.
export const rotateQR = (sessionId) =>
  request("/qr/rotate", { method: "POST", body: { session_id: sessionId } });

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
export const verifyToken = (sessionToken) =>
  request("/attendance/verify-token", { method: "POST", body: { session_token: sessionToken } });

// Whether this Google account already has a backend-verified enrollment.
export const getStudentMe = () => request("/student/me");

// Resolve the last 3 enrollment digits against Sheet1 (server-side lookup).
export const studentLookup = (last3) =>
  request("/student/lookup", { method: "POST", body: { last3 } });

// Confirm the match: backend verifies again and links enrollment to the UID.
export const studentConfirm = (last3) =>
  request("/student/confirm", { method: "POST", body: { last3 } });

// Identity comes from the verified enrollment saved against the UID — the
// request body carries no name/enrollment fields at all.
export const checkAttendance = ({ sessionToken, latitude, longitude }) =>
  request("/attendance/check", {
    method: "POST",
    body: {
      session_token: sessionToken,
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
