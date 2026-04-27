import { useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Avatar } from '@/components/Avatar';
import { Button } from '@/components/Button';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { useAuthStore } from '@/stores/auth-store';
import { useModelStore } from '@/stores/model-store';
import { API_BASE_URL } from '@/lib/constants';
import { colors, radius, spacing, typography } from '@/theme';

export default function SettingsScreen() {
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const modelStatus = useModelStore((s) => s.status);
  const router = useRouter();
  const initial = user?.first_name?.[0] || user?.username?.[0] || '?';
  const fullName =
    [user?.first_name, user?.last_name].filter(Boolean).join(' ') || user?.username;

  return (
    <Screen>
      <View style={styles.header}>
        <Text style={typography.h1}>Settings</Text>
      </View>

      <Card style={styles.profile}>
        <Avatar initial={initial} size={56} variant="student" />
        <View style={{ flex: 1 }}>
          <Text style={typography.h3}>{fullName ?? 'Not signed in'}</Text>
          {user?.email ? (
            <Text style={[typography.small, styles.muted]}>{user.email}</Text>
          ) : null}
          {user?.memberships?.[0] ? (
            <Text style={[typography.small, styles.muted]}>
              {user.memberships[0].institution_name}
            </Text>
          ) : null}
        </View>
      </Card>

      <SettingsRow
        icon="hardware-chip-outline"
        label="AI Model"
        sublabel={modelStatusLabel(modelStatus)}
        onPress={() => router.push('/(app)/model-store')}
      />

      <Card flat style={styles.row}>
        <Ionicons name="server-outline" size={18} color={colors.textMuted} />
        <View style={{ flex: 1 }}>
          <Text style={[typography.caption, styles.muted]}>API SERVER</Text>
          <Text style={[typography.small, { color: colors.text }]}>{API_BASE_URL}</Text>
        </View>
      </Card>

      <Button title="Sign out" variant="danger" onPress={() => void signOut()} />
    </Screen>
  );
}

function modelStatusLabel(status: string): string {
  switch (status) {
    case 'idle':
      return 'Not downloaded';
    case 'downloading':
      return 'Downloading…';
    case 'downloaded':
      return 'Saved · not loaded';
    case 'loading':
      return 'Loading…';
    case 'loaded':
      return 'Loaded · ready';
    default:
      return '';
  }
}

interface RowProps {
  icon: React.ComponentProps<typeof Ionicons>['name'];
  label: string;
  sublabel?: string;
  onPress: () => void;
}

function SettingsRow({ icon, label, sublabel, onPress }: RowProps) {
  return (
    <Pressable
      onPress={onPress}
      style={(state) => {
        const hovered = (state as { hovered?: boolean }).hovered;
        return [
          styles.linkRow,
          hovered && styles.linkRowHover,
          state.pressed && { opacity: 0.6 },
        ];
      }}
    >
      <Ionicons name={icon} size={20} color={colors.text} />
      <View style={{ flex: 1 }}>
        <Text style={[typography.bodyMedium, { color: colors.text }]}>{label}</Text>
        {sublabel ? (
          <Text style={[typography.small, styles.muted]}>{sublabel}</Text>
        ) : null}
      </View>
      <Ionicons name="chevron-forward" size={18} color={colors.textSubtle} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  header: { gap: spacing.xs, marginBottom: spacing.sm },
  profile: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  row: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  linkRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.borderMuted,
    borderRadius: radius.lg,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.lg,
  },
  linkRowHover: { borderColor: colors.border },
  muted: { color: colors.textMuted },
});
