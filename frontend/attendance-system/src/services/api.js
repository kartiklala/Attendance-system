// Centralized FastAPI client. Every network call in the app goes through here.
// The application JWT lives in an HttpOnly cookie, so all requests are made
// with credentials: "include" and the frontend never reads the token itself.
const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

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
export const authorizeUser = (firebaseIdToken) =>
  request("/authorize-user", { method: "POST", firebaseIdToken });

export const logout = () => request("/logout", { method: "POST" });
export const getMe = () => request("/me");

// ---- CR session ---------------------------------------------------------
export const getActiveSession = () => request("/active-session");

export const startAttendance = (latitude, longitude) =>
  request("/start-attendance", { method: "POST", body: { latitude, longitude } });

export const refreshQR = (sessionId) =>
  request("/qr/refresh", { method: "POST", body: { session_id: sessionId } });

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

export default request;
