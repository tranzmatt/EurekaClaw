function readableError(errorText: string, status: number): string {
  const trimmed = errorText.trim();
  if (trimmed) {
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object' && typeof parsed.error === 'string') {
        return parsed.error;
      }
    } catch {
      // plain text body — fall through
    }
    return trimmed;
  }
  return `Request failed: ${status}`;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    const errorText = await response.text().catch(() => '');
    throw new Error(readableError(errorText, response.status));
  }
  return response.json() as Promise<T>;
}

export async function apiPost<T>(path: string, payload: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const errorText = await response.text();
    if (response.status === 501 && errorText.includes('Unsupported method')) {
      throw new Error(
        'This page is being served by a static file server. Start the real backend with `eurekaclaw ui` and open http://127.0.0.1:8080/.'
      );
    }
    throw new Error(readableError(errorText, response.status));
  }
  return response.json() as Promise<T>;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(path, { method: 'DELETE' });
  if (!response.ok) {
    const errorText = await response.text().catch(() => '');
    throw new Error(readableError(errorText, response.status));
  }
  return response.json() as Promise<T>;
}
