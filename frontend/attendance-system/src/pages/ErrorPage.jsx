// Generic error page (unknown routes / access errors).
import { Link } from "react-router-dom";

export default function ErrorPage({ message }) {
  return (
    <div className="page-center">
      <div className="card">
        <h1 className="app-title">Something went wrong</h1>
        <p className="muted">
          {message || "The page you are looking for does not exist."}
        </p>
        <Link className="btn btn-primary" to="/">
          Go to Home
        </Link>
      </div>
    </div>
  );
}
