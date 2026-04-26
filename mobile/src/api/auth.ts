import { apiClient } from './client';
import type { TokenPair, User } from './types';

export async function login(username: string, password: string): Promise<TokenPair> {
  const res = await apiClient.post<TokenPair>('/auth/login/', { username, password });
  return res.data;
}

export interface RegisterPayload {
  username: string;
  password: string;
  email?: string;
  first_name?: string;
  last_name?: string;
  institution_slug?: string;
  school?: string;
  grade_level?: string;
}

export async function register(payload: RegisterPayload): Promise<TokenPair> {
  const res = await apiClient.post<TokenPair>('/auth/register/', payload);
  return res.data;
}

export async function logout(refresh: string): Promise<void> {
  await apiClient.post('/auth/logout/', { refresh });
}

export async function me(): Promise<User> {
  const res = await apiClient.get<User>('/me/');
  return res.data;
}
