import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type Organization } from '../api';
import { Card, ErrorMessage } from '../components/ui';
import { t } from '../i18n';

export function NewClientPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: '', siren: '', address: '' });
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const org = await api.post<Organization>('/organizations', {
        name: form.name, siren: form.siren || null, address: form.address || null,
      });
      // Étapes suivantes de l'onboarding : sites, points de livraison, consentement.
      navigate(`/organizations/${org.id}/manage`);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="stack narrow">
      <h1>{t.newClient.title}</h1>
      <ErrorMessage message={error} />
      <Card>
        <form className="form" onSubmit={submit}>
          <label>{t.newClient.name}<input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></label>
          <label>{t.newClient.siren}
            <input value={form.siren} pattern="\d{9}" onChange={(e) => setForm({ ...form, siren: e.target.value })} />
          </label>
          <label>{t.newClient.address}<input value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} /></label>
          <button className="btn btn-primary" type="submit">{t.newClient.next}</button>
        </form>
      </Card>
    </div>
  );
}
