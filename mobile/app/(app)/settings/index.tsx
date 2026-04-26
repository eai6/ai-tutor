import { StyleSheet, Text, View } from 'react-native';

import { Button } from '@/components/Button';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { useAuthStore } from '@/stores/auth-store';
import { API_BASE_URL } from '@/lib/constants';
import { colors, spacing, typography } from '@/theme';

export default function SettingsScreen() {
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);

  return (
    <Screen>
      <View style={styles.header}>
        <Text style={typography.h2}>Settings</Text>
      </View>

      <Card style={styles.card}>
        <Text style={typography.h3}>Account</Text>
        <Text style={[typography.small, styles.muted]}>
          {user ? `${user.username}${user.email ? ` · ${user.email}` : ''}` : 'Not signed in'}
        </Text>
        {user?.memberships?.[0] ? (
          <Text style={[typography.small, styles.muted]}>
            {user.memberships[0].institution_name}
          </Text>
        ) : null}
      </Card>

      <Card style={styles.card}>
        <Text style={typography.h3}>API server</Text>
        <Text style={[typography.small, styles.muted]}>{API_BASE_URL}</Text>
      </Card>

      <Button title="Sign out" variant="danger" onPress={() => void signOut()} />
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: { gap: spacing.xs },
  card: { gap: spacing.xs },
  muted: { color: colors.textMuted },
});
