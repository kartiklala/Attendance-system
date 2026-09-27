// AuthContext: Firebase sign-in state + backend authorization (role) state.
// The role always comes from FastAPI's /authorize-user response — the
// frontend never decides whether a user is a CR.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import * as api from "../services/api";
import {
  getFirebaseIdToken,
  observeAuthUser,
  signInWithGoogle,
  signOutUser,
} from "../services/auth";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [firebaseUser, setFirebaseUser] = useState(undefined); // undefined = loading
  const [role, setRole] = useState(null); // null | "cr" | "student"
  const [profile, setProfile] = useState(null); // { uid, name, email, role }
  const [error, setError] = useState(null);
  // Set when the backend permanently rejects the application JWT and it
  // could not be re-issued: all session/attendance UI state must be dropped
  // and fresh authorization required (spec: no stale "Continue").
  const [requiresReauth, setRequiresReauth] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const authorize = useCallback(async (idToken) => {
    const data = await api.authorizeUser(idToken);
    if (!mounted.current) return null;
    setProfile(data.user);
    setRole(data.user.role);
    setError(null);
    setRequiresReauth(false);
    return data.user;
  }, []);

  // Re-authorize with the current Firebase session (used by the api layer
  // when the 3-minute application JWT expires mid-session). If recovery is
  // impossible, the api layer calls the permanent handler: clear the
  // authenticated profile so every protected screen falls back to sign-in.
  useEffect(() => {
    api.setAuthRecovery(
      async () => {
        try {
          const token = await getFirebaseIdToken(true);
          if (!token) return false;
          await authorize(token);
          return true;
        } catch {
          return false;
        }
      },
      () => {
        if (!mounted.current) return;
        setProfile(null);
        setRole(null);
        setRequiresReauth(true);
        setError("Your session expired. Please sign in again.");
      }
    );
    return () => api.setAuthRecovery(null, null);
  }, [authorize]);

  // Bootstrap: restore an existing Firebase session and re-authorize.
  useEffect(() => {
    const unsubscribe = observeAuthUser(async (user) => {
      setFirebaseUser(user || null);
      if (!user) {
        setRole(null);
        setProfile(null);
        setRequiresReauth(false);
        return;
      }
      try {
        const token = await user.getIdToken(true);
        await authorize(token);
      } catch (err) {
        if (mounted.current) setError(err.message || "Authorization failed.");
      }
    });
    return unsubscribe;
  }, [authorize]);

  const login = useCallback(async () => {
    setError(null);
    try {
      const result = await signInWithGoogle();
      const token = await result.user.getIdToken(true);
      return await authorize(token);
    } catch (err) {
      const message = err?.code === "auth/popup-closed-by-user"
        ? "Sign-in was cancelled."
        : err?.message || "Sign-in failed. Please try again.";
      setError(message);
      throw err;
    }
  }, [authorize]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* cookie already gone or network issue — continue signing out locally */
    }
    await signOutUser();
    setRole(null);
    setProfile(null);
    setRequiresReauth(false);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        firebaseUser,   // Firebase user object or null (undefined while loading)
        profile,        // { uid, name, email, role } from the backend
        role,           // "cr" | "student" | null
        error,
        requiresReauth, // JWT rejected and could not be re-issued
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
