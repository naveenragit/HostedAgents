export interface AppConfig {
  fallbackMaxInputChars: number;
}

type RuntimeEnvironment = Record<string, string | boolean | undefined>;

function requireValue(env: RuntimeEnvironment, name: string): string {
  const value = env[name];
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${name} is required.`);
  }
  return value.trim();
}

export function loadAppConfig(env: RuntimeEnvironment = import.meta.env): AppConfig {
  const fallbackMaxInputChars = Number(requireValue(env, 'VITE_MAX_INPUT_CHARS'));
  if (!Number.isSafeInteger(fallbackMaxInputChars) || fallbackMaxInputChars < 1) {
    throw new Error('VITE_MAX_INPUT_CHARS must be a positive integer.');
  }

  return {
    fallbackMaxInputChars,
  };
}