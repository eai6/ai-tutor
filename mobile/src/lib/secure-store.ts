import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';

const ACCESS = 'auth.access';
const REFRESH = 'auth.refresh';
const USER = 'auth.user';

// expo-secure-store doesn't ship a real web implementation — calling
// its async helpers throws "getValueWithKeyAsync is not a function".
// Fall back to localStorage on web (not actually secure, but fine for
// dev). Native builds use the keychain / keystore as designed.
const useNativeSecureStore = Platform.OS !== 'web';

function webStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

async function setItem(key: string, value: string): Promise<void> {
  if (useNativeSecureStore) {
    await SecureStore.setItemAsync(key, value);
    return;
  }
  webStorage()?.setItem(key, value);
}

async function getItem(key: string): Promise<string | null> {
  if (useNativeSecureStore) {
    return SecureStore.getItemAsync(key);
  }
  return webStorage()?.getItem(key) ?? null;
}

async function deleteItem(key: string): Promise<void> {
  if (useNativeSecureStore) {
    await SecureStore.deleteItemAsync(key);
    return;
  }
  webStorage()?.removeItem(key);
}

export async function saveTokens(access: string, refresh: string) {
  await setItem(ACCESS, access);
  await setItem(REFRESH, refresh);
}

export async function loadTokens(): Promise<{ access: string | null; refresh: string | null }> {
  const [access, refresh] = await Promise.all([getItem(ACCESS), getItem(REFRESH)]);
  return { access, refresh };
}

export async function clearTokens() {
  await deleteItem(ACCESS);
  await deleteItem(REFRESH);
  await deleteItem(USER);
}

export async function saveUser<T>(user: T) {
  await setItem(USER, JSON.stringify(user));
}

export async function loadUser<T>(): Promise<T | null> {
  const raw = await getItem(USER);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}
