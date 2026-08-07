import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from './App';
import type { AppConfig } from './config';

const config: AppConfig = {
  fallbackMaxInputChars: 80,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('App accessibility', () => {
  it('announces completed API activity through a polite status region', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const payload = path.endsWith('/api/capabilities')
        ? { maxInputChars: 80 }
        : path.endsWith('/api/agents')
          ? [{ name: 'triage-agent' }]
          : { items: [], nextCursor: null };
      return Promise.resolve(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App config={config} />);

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Conversations loaded.'),
    );
    expect(screen.getByText('Local POC')).toBeVisible();
  });
});