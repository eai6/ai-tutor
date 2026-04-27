import { Tabs } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import { Platform } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { colors } from '@/theme';

export default function TabsLayout() {
  const insets = useSafeAreaInsets();
  // Add the bottom inset (gesture-nav bar / home indicator) onto the tab
  // bar so it doesn't overlap with system UI.
  const baseBottomPad = Platform.OS === 'ios' ? 8 : 8;
  const baseHeight = Platform.OS === 'ios' ? 56 : 56;

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.text,
        tabBarInactiveTintColor: colors.textSubtle,
        tabBarLabelStyle: {
          fontFamily: 'Inter_600SemiBold',
          fontSize: 11,
          marginTop: -2,
        },
        tabBarStyle: {
          backgroundColor: colors.bg,
          borderTopColor: colors.borderMuted,
          paddingTop: 6,
          paddingBottom: baseBottomPad + insets.bottom,
          height: baseHeight + insets.bottom,
        },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: 'Home',
          tabBarIcon: ({ color, focused }) => (
            <Ionicons name={focused ? 'home' : 'home-outline'} size={22} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="progress"
        options={{
          title: 'Progress',
          tabBarIcon: ({ color, focused }) => (
            <Ionicons name={focused ? 'trophy' : 'trophy-outline'} size={22} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: 'Settings',
          tabBarIcon: ({ color, focused }) => (
            <Ionicons name={focused ? 'settings' : 'settings-outline'} size={22} color={color} />
          ),
        }}
      />
    </Tabs>
  );
}
