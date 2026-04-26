import { create } from 'zustand';

import { logout as apiLogout } from '@/api/auth';
import type { User } from '@/api/types';
import { clearTokens, loadTokens, loadUser, saveTokens, saveUser } from '@/lib/secure-store';

type Status = 'idle' | 'loading' | 'authed' | 'guest';

interface AuthState {
  status: Status;
  user: User | null;
  hydrate: () => Promise<void>;
  setSession: (access: string, refresh: string, user: User) => Promise<void>;
  signOut: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  status: 'idle',
  user: null,

  async hydrate() {
    set({ status: 'loading' });
    const [{ access }, user] = await Promise.all([loadTokens(), loadUser<User>()]);
    if (access && user) {
      set({ status: 'authed', user });
    } else {
      set({ status: 'guest', user: null });
    }
  },

  async setSession(access, refresh, user) {
    await saveTokens(access, refresh);
    await saveUser(user);
    set({ status: 'authed', user });
  },

  async signOut() {
    const { refresh } = await loadTokens();
    if (refresh) {
      try {
        await apiLogout(refresh);
      } catch {
        // ignore — proceed with local clear
      }
    }
    await clearTokens();
    set({ status: 'guest', user: null });
  },
}));

// expose signOut for the API client's 401 handler without circular import.
import { setOnUnauthorized } from '@/api/client';
setOnUnauthorized(() => {
  void useAuthStore.getState().signOut();
});
