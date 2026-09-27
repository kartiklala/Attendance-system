// Centralized FastAPI client. Every network call in the app goes through here.
// The application JWT lives in an HttpOnly cookie, so all requests are made
// with credentials: "include" and the frontend never reads the token itself.
const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

// Set by AuthContext: async () => refetch /authorize-user with a fresh
// Firebase ID token. Used to transparently recover when the 3-minute
// application JWT expires mid-session.
let recoverAuth = null;
export function setAuthRecovery(fn) {
  recoverAuth = fn;
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
  if (response.status === 401 && !isRetry && recoverAuth && path !== "/authorize-user") {
    const recovered = await recoverAuth();
    if (recovered) return request(path, { method, body, firebaseIdToken }, true);
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
export const startAttendance = (latitude, longitude) =>
  request("/start-attendance", { method: "POST", body: { latitude, longitude } });

export const refreshQR = (sessionId) =>
  request("/qr/refresh", { method: "POST", body: { session_id: sessionId } });

export const endAttendance = (sessionId) =>
  request("/end-attendance", { method: "POST", body: { session_id: sessionId } });

// ---- Student ------------------------------------------------------------
export const verifyToken = (sessionToken) =>
  request("/attendance/verify-token", { method: "POST", body: { session_token: sessionToken } });

export const checkAttendance = ({ sessionToken, name, enrollmentNo, latitude, longitude }) =>
  request("/attendance/check", {
    method: "POST",
    body: {
      session_token: sessionToken,
      name,
      enrollment_no: enrollmentNo,
      latitude,
      longitude,
    },
  });

export default request;
