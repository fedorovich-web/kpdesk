/**
 * Anonymous users legitimately receive 401. A confirmed, live server-side
 * session is the only signal for showing authenticated marketing navigation.
 * No identity data or CSRF tokens are read or stored by public pages.
 */
export async function hasActivePublicSession(fetcher: typeof fetch = fetch): Promise<boolean> {
  try {
    const response = await fetcher('/api/session/me', {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    return response.status === 200;
  } catch {
    // Never block a public page when auth is unavailable or the visitor is anonymous.
    return false;
  }
}
