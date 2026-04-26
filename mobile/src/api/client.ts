import axios, { AxiosError, AxiosInstance, InternalAxiosRequestConfig } from 'axios';

import { API_BASE_URL } from '@/lib/constants';
import { clearTokens, loadTokens, saveTokens } from '@/lib/secure-store';

type RetryConfig = InternalAxiosRequestConfig & { _retry?: boolean };

let onUnauthorized: (() => void) | null = null;

export function setOnUnauthorized(fn: (() => void) | null) {
  onUnauthorized = fn;
}

export const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 20_000,
  headers: { 'Content-Type': 'application/json' },
});

apiClient.interceptors.request.use(async (config) => {
  const { access } = await loadTokens();
  if (access) {
    config.headers = config.headers ?? {};
    (config.headers as Record<string, string>).Authorization = `Bearer ${access}`;
  }
  return config;
});

let refreshInflight: Promise<string | null> | null = null;

async function refreshAccessToken(): Promise<string | null> {
  if (refreshInflight) return refreshInflight;
  refreshInflight = (async () => {
    const { refresh } = await loadTokens();
    if (!refresh) return null;
    try {
      const res = await axios.post(
        `${API_BASE_URL}/auth/refresh/`,
        { refresh },
        { headers: { 'Content-Type': 'application/json' } },
      );
      const access = res.data?.access as string | undefined;
      if (!access) return null;
      await saveTokens(access, refresh);
      return access;
    } catch {
      return null;
    } finally {
      refreshInflight = null;
    }
  })();
  return refreshInflight;
}

apiClient.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError) => {
    const original = error.config as RetryConfig | undefined;
    if (
      error.response?.status === 401 &&
      original &&
      !original._retry &&
      !original.url?.includes('/auth/')
    ) {
      original._retry = true;
      const access = await refreshAccessToken();
      if (access) {
        original.headers = original.headers ?? {};
        (original.headers as Record<string, string>).Authorization = `Bearer ${access}`;
        return apiClient(original);
      }
      await clearTokens();
      onUnauthorized?.();
    }
    return Promise.reject(error);
  },
);

export function extractErrorMessage(err: unknown, fallback = 'Something went wrong'): string {
  if (axios.isAxiosError(err)) {
    const data = err.response?.data as Record<string, unknown> | undefined;
    if (data) {
      if (typeof data.detail === 'string') return data.detail;
      const firstField = Object.entries(data).find(([, v]) =>
        Array.isArray(v) ? v.length > 0 : typeof v === 'string',
      );
      if (firstField) {
        const [, val] = firstField;
        return Array.isArray(val) ? String(val[0]) : String(val);
      }
    }
    return err.message || fallback;
  }
  return fallback;
}
