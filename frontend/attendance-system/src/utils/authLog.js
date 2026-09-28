// Development-safe logging for the authentication state machine.
//
// The Google redirect bug can only be diagnosed in a real browser, and the
// build must stay quiet in production, so every line is behind a flag that is
// false in a production bundle (Vite replaces import.meta.env.DEV at build
// time). Set VITE_AUTH_DEBUG=true to force logging on for a staging build.
//
// HARD RULE: nothing here may receive a Firebase ID token, the application
// JWT, a raw QR token, an enrollment number, an email address or any personal
// data. Callers pass booleans, codes and pathnames only.
const DEBUG =
  import.meta.env.DEV === true || import.meta.env.VITE_AUTH_DEBUG === "true";

export function authLog(stage, details) {
  if (!DEBUG) return;
  if (details === undefined) {
    console.info(`[AUTH] ${stage}`);
    return;
  }
  console.info(`[AUTH] ${stage}`, details);
}

export function attendanceLog(stage, details) {
  if (!DEBUG) return;
  if (details === undefined) {
    console.info(`[ATTENDANCE] ${stage}`);
    return;
  }
  console.info(`[ATTENDANCE] ${stage}`, details);
}

export default authLog;
