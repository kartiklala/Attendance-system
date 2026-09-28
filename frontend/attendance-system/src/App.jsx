// App shell: routing + auth provider.
// Routes: "/" (login), "/cr" (CR dashboard), "/admin" (admin dashboard),
// "/attendance" (student QR flow).
// UI selection is based on the backend authorization response, but every
// security decision is enforced by FastAPI.
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import Login from "./pages/Login";
import CRDashboard from "./pages/CRDashboard";
import AdminDashboard from "./pages/AdminDashboard";
import StudentAttendance from "./pages/StudentAttendance";
import ErrorPage from "./pages/ErrorPage";
import { Loading } from "./components/ui";
import "./App.css";

function CRRoute() {
  const { firebaseUser, role, requiresReauth } = useAuth();
  if (firebaseUser === undefined || (firebaseUser && role === null)) {
    // JWT rejected and not re-issuable: no stale dashboard — back to sign-in.
    if (requiresReauth) return <Navigate to="/" replace />;
    return <Loading label="Checking your access…" />;
  }
  if (!firebaseUser) return <Navigate to="/" replace />;
  // Admins inherit every CR capability (require_cr() lets them through
  // server-side), so both roles may open the CR dashboard.
  if (role !== "cr" && role !== "admin")
    return <ErrorPage message="This area is only available to Class Representatives." />;
  return <CRDashboard />;
}

function AdminRoute() {
  const { firebaseUser, role, requiresReauth } = useAuth();
  if (firebaseUser === undefined || (firebaseUser && role === null)) {
    if (requiresReauth) return <Navigate to="/" replace />;
    return <Loading label="Checking your access…" />;
  }
  if (!firebaseUser) return <Navigate to="/" replace />;
  // Role comes from the backend-signed JWT; the /admin/* APIs are also
  // admin-protected server-side, so this is UI routing, not a security gate.
  if (role !== "admin") return <ErrorPage message="This area is only available to administrators." />;
  return <AdminDashboard />;
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Login />} />
          <Route path="/cr" element={<CRRoute />} />
          <Route path="/admin" element={<AdminRoute />} />
          <Route path="/attendance" element={<StudentAttendance />} />
          <Route path="*" element={<ErrorPage />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;
