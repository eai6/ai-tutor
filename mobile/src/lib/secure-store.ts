import * as SecureStore from 'expo-secure-store';

const ACCESS = 'auth.access';
const REFRESH = 'auth.refresh';
const USER = 'auth.user';

export async function saveTokens(access: string, refresh: string) {
  await SecureStore.setItemAsync(ACCESS, access);
  await SecureStore.setItemAsync(REFRESH, refresh);
}

export async function loadTokens(): Promise<{ access: string | null; refresh: string | null }> {
  const [access, refresh] = await Promise.all([
    SecureStore.getItemAsync(ACCESS),
    SecureStore.getItemAsync(REFRESH),
  ]);
  return { access, refresh };
}

export async function clearTokens() {
  await SecureStore.deleteItemAsync(ACCESS);
  await SecureStore.deleteItemAsync(REFRESH);
  await SecureStore.deleteItemAsync(USER);
}

export async function saveUser<T>(user: T) {
  await SecureStore.setItemAsync(USER, JSON.stringify(user));
}

export async function loadUser<T>(): Promise<T | null> {
  const raw = await SecureStore.getItemAsync(USER);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}
