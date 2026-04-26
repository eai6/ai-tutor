import Constants from 'expo-constants';

const extra = (Constants.expoConfig?.extra ?? {}) as Record<string, string>;

export const API_BASE_URL: string =
  process.env.EXPO_PUBLIC_API_BASE_URL ||
  extra.apiBaseUrl ||
  'http://localhost:8000/api/v1';
