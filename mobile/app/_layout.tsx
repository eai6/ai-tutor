import { useEffect } from 'react';
import { Stack, useRouter, useSegments } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { KeyboardProvider } from 'react-native-keyboard-controller';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  useFonts,
  Inter_400Regular,
  Inter_500Medium,
  Inter_600SemiBold,
  Inter_700Bold,
  Inter_800ExtraBold,
} from '@expo-google-fonts/inter';
import {
  Lora_400Regular,
  Lora_500Medium,
  Lora_600SemiBold,
} from '@expo-google-fonts/lora';

import { OfflineBanner } from '@/components/OfflineBanner';
import { ensureSchema } from '@/db/client';
import { useAuthStore } from '@/stores/auth-store';
import { colors } from '@/theme';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 30_000 },
  },
});

function AuthGate() {
  const status = useAuthStore((s) => s.status);
  const router = useRouter();
  const segments = useSegments();

  useEffect(() => {
    if (status === 'idle' || status === 'loading') return;
    const inAuthGroup = segments[0] === '(auth)';
    if (status === 'guest' && !inAuthGroup) {
      router.replace('/(auth)/login');
    } else if (status === 'authed' && inAuthGroup) {
      router.replace('/(app)/(tabs)');
    }
  }, [status, segments, router]);

  return null;
}

export default function RootLayout() {
  const hydrate = useAuthStore((s) => s.hydrate);
  const status = useAuthStore((s) => s.status);
  const [fontsLoaded] = useFonts({
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_700Bold,
    Inter_800ExtraBold,
    Lora_400Regular,
    Lora_500Medium,
    Lora_600SemiBold,
  });

  useEffect(() => {
    ensureSchema();
    void hydrate();
  }, [hydrate]);

  if (status === 'idle' || status === 'loading' || !fontsLoaded) {
    return (
      <View style={styles.loading}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  return (
    <GestureHandlerRootView style={styles.flex}>
      <KeyboardProvider>
        <SafeAreaProvider>
          <QueryClientProvider client={queryClient}>
            <StatusBar style="auto" />
            <AuthGate />
            <OfflineBanner />
            <Stack screenOptions={{ headerShown: false }} />
          </QueryClientProvider>
        </SafeAreaProvider>
      </KeyboardProvider>
    </GestureHandlerRootView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  loading: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.bg,
  },
});
