import { defineConfig, loadEnv, type ResolvedConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The unauthenticated POC must stay on loopback. Binding both Vite and FastAPI
// locally prevents another machine from reaching the API through this proxy.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const backend = env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000';

  return {
    plugins: [
      react(),
      {
        name: 'enforce-poc-loopback',
        configResolved(config: ResolvedConfig) {
          const hosts = [config.server.host, config.preview.host];
          if (hosts.some((host) => host !== '127.0.0.1')) {
            throw new Error(
              'The unauthenticated POC dev and preview servers must bind to 127.0.0.1.',
            );
          }
        },
      },
    ],
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/health': { target: backend, changeOrigin: true },
      },
    },
    preview: {
      host: '127.0.0.1',
      port: 4173,
      strictPort: true,
    },
    build: {
      outDir: 'dist',
      sourcemap: env.VITE_BUILD_SOURCEMAP === 'true',
    },
  };
});
