/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_MAX_INPUT_CHARS?: string;
  readonly VITE_API_PROXY_TARGET?: string;
  readonly VITE_BUILD_SOURCEMAP?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
