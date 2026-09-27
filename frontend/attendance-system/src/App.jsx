// App shell: routing + auth provider.
// Routes: "/" (login), "/cr" (CR dashboard), "/attendance" (student QR flow).
// UI selection is based on the backend authorization response, but every
// security decision is enforced by FastAPI.
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import Login from "./pages/Login";
import CRDashboard from "./pages/CRDashboard";
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
  if (role !== "cr") return <ErrorPage message="This area is only available to Class Representatives." />;
  return <CRDashboard />;
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Login />} />
          <Route path="/cr" element={<CRRoute />} />
          <Route path="/attendance" element={<StudentAttendance />} />
          <Route path="*" element={<ErrorPage />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;
