import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, createApiClient } from './client';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('POC API client', () => {
  it('sends neither an identity header nor a bearer token', async () => {
    const fetchMock = vi.fn<
      (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
    >(() =>
      Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    await createApiClient().listAgents();

    const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(headers.has('Authorization')).toBe(false);
    expect(headers.has('x-dev-user-id')).toBe(false);
    expect(headers.get('Accept')).toBe('application/json');
  });

  it('keeps API errors generic and typed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ detail: 'Request was rejected.' }), {
            status: 403,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    );

    await expect(createApiClient().listAgents()).rejects.toEqual(
      new ApiError(403, 'Request was rejected.'),
    );
  });
});
