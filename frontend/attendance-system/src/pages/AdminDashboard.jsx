// Admin dashboard: manage the authoritative CR email list.
// Every write goes through the protected FastAPI /admin/cr endpoints —
// the React app never touches Firestore, and the backend re-verifies the
// admin role from the signed JWT on every call (a manually crafted request
// from a student/CR is rejected with 403 regardless of this UI).
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as api from "../services/api";
import { useAuth } from "../context/AuthContext";
import { ErrorBox, BrandMark } from "../components/ui";

export default function AdminDashboard() {
  const { profile, logout } = useAuth();
  const navigate = useNavigate();
  const [emails, setEmails] = useState([]);
  const [listError, setListError] = useState(null);
  const [email, setEmail] = useState("");
  const [actionError, setActionError] = useState(null);
  const [notice, setNotice] = useState(null);
  // Disable "Add CR" on the first click while the request is in flight.
  const [adding, setAdding] = useState(false);
  // Email currently being removed (disables its X while the request settles).
  const [removing, setRemoving] = useState(null);

  const loadList = useCallback(async () => {
    try {
      const data = await api.listCRs();
      setEmails(data.cr_emails || []);
      setListError(null);
    } catch (err) {
      setListError(err.message || "Could not load the CR list.");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Initial load only; refresh runs from the button/event handlers.
    (async () => {
      try {
        const data = await api.listCRs();
        if (!cancelled) {
          setEmails(data.cr_emails || []);
          setListError(null);
        }
      } catch (err) {
        if (!cancelled) setListError(err.message || "Could not load the CR list.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleAdd = async (event) => {
    event.preventDefault();
    if (adding) return;
    const value = email.trim();
    if (!value) {
      setActionError("Enter a CR email address first.");
      return;
    }
    setAdding(true);
    setActionError(null);
    setNotice(null);
    try {
      const data = await api.addCR(value);
      setNotice(data.message || "CR added successfully.");
      setEmail("");
      await loadList();
    } catch (err) {
      setActionError(err.message || "Could not add that CR email.");
    } finally {
      setAdding(false);
    }
  };

  // Admin removes a CR from the authoritative list (backend DELETE /admin/cr).
  const handleRemove = async (addr) => {
    if (removing) return; // one removal at a time — avoids list races
    setRemoving(addr);
    setActionError(null);
    setNotice(null);
    try {
      const data = await api.removeCR(addr);
      setNotice(data.message || "CR removed successfully.");
      await loadList();
    } catch (err) {
      setActionError(err.message || "Could not remove that CR email.");
    } finally {
      setRemoving(null);
    }
  };

  return (
    <div className="page-center">
      <div className="card">
        <div className="cr-header">
          <div className="brand-lockup brand-lockup-row">
            <BrandMark size={44} />
            <div className="brand-text">
              <h1 className="app-title brand-name">Attendify</h1>
              <p className="app-subtitle">
                {profile?.email ? `Admin Console · ${profile.email}` : "Manage Class Representative access"}
              </p>
            </div>
          </div>
          <div className="header-actions">
            <button className="btn btn-ghost btn-small" onClick={() => navigate("/cr")}>
              Open CR Dashboard
            </button>
            <button className="btn btn-ghost btn-small" onClick={() => logout().catch(() => {})}>
              Sign out
            </button>
          </div>
        </div>

        <section className="admin-section">
          <h2 className="section-title">Add New CR</h2>
          <form className="admin-add-form" onSubmit={handleAdd}>
            <input
              type="email"
              className="input"
              placeholder="cr-student@gmail.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
            />
            <button type="submit" className="btn btn-primary" disabled={adding}>
              {adding ? "Adding…" : "Add CR"}
            </button>
          </form>
          {notice && <p className="admin-notice">{notice}</p>}
          <ErrorBox message={actionError} />
        </section>

        <section className="admin-section">
          <div className="admin-list-head">
            <h2 className="section-title">Current CRs ({emails.length})</h2>
            <button className="btn btn-ghost btn-small" onClick={loadList}>
              Refresh
            </button>
          </div>
          {emails.length === 0 ? (
            <p className="muted">No CR emails have been added yet.</p>
          ) : (
            <ul className="admin-cr-list">
              {emails.map((addr) => (
                <li key={addr} className="admin-cr-item">
                  <span className="admin-cr-email">{addr}</span>
                  <button
                    type="button"
                    className="admin-cr-remove"
                    onClick={() => handleRemove(addr)}
                    disabled={removing === addr || (removing !== null)}
                    title="Remove this CR"
                    aria-label={`Remove ${addr}`}
                  >
                    {removing === addr ? "…" : "✕"}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <ErrorBox message={listError} />
        </section>
      </div>
    </div>
  );
}
