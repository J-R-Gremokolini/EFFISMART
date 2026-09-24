import { useState, type FormEvent } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from '../auth';
import { t } from '../i18n';

export function LoginPage() {
  const { user, login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (user) return <Navigate to="/" replace />;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(email, password);
    } catch {
      setError(t.auth.invalid);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <form className="card login-card" onSubmit={onSubmit}>
        <div className="brand brand-large">
          <span className="brand-mark" aria-hidden>⚡</span>
          <span className="brand-name">{t.app.name}</span>
        </div>
        <p className="muted">{t.app.tagline}</p>
        <h1>{t.auth.title}</h1>
        <label>
          {t.auth.email}
          <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <label>
          {t.auth.password}
          <input type="password" autoComplete="current-password" value={password}
                 onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p className="alert alert-error">{error}</p>}
        <button className="btn btn-primary" type="submit" disabled={submitting}>{t.auth.submit}</button>
        <p className="hint">{t.auth.demoHint}</p>
      </form>
    </div>
  );
}
