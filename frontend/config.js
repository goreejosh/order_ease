// Set this to your Railway backend URL (no trailing slash)
const API_BASE = window.__API_BASE || 'https://mn-production.up.railway.app';

async function apiFetchJson(path, options = {}) {
  const res = await fetch(API_BASE + path, options);
  const contentType = res.headers.get('content-type') || '';
  const body = contentType.includes('application/json') ? await res.json() : await res.text();

  if (!res.ok) {
    const message =
      (body && typeof body === 'object' && (body.detail || body.error || body.message)) ||
      (typeof body === 'string' && body) ||
      `Request failed (${res.status})`;
    throw new Error(message);
  }

  return body;
}
