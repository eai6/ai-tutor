import { Stack } from 'expo-router';

import { colors } from '@/theme';

export default function AppStackLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.bg },
        headerTitleStyle: { fontFamily: 'Inter_600SemiBold', fontSize: 16 },
        headerTintColor: colors.text,
        headerShadowVisible: false,
        headerBackTitle: 'Back',
        contentStyle: { backgroundColor: colors.bg },
      }}
    >
      <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
      <Stack.Screen name="courses/[id]" options={{ title: 'Lessons' }} />
      <Stack.Screen name="lessons/[id]/index" options={{ title: 'Lesson' }} />
      <Stack.Screen name="lessons/[id]/review" options={{ title: 'Review' }} />
      <Stack.Screen name="chat/[lessonId]" options={{ title: 'Tutor' }} />
      <Stack.Screen name="model-store" options={{ title: 'AI Model' }} />
    </Stack>
  );
}
