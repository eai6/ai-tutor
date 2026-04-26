import { StyleSheet, Text, View } from 'react-native';

import { useNetworkState } from '@/hooks/useNetworkState';
import { colors, spacing, typography } from '@/theme';

export function OfflineBanner() {
  const { isConnected } = useNetworkState();
  if (isConnected) return null;
  return (
    <View style={styles.bar}>
      <Text style={[typography.small, styles.text]}>You're offline</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    backgroundColor: colors.warning,
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.md,
    alignItems: 'center',
  },
  text: { color: colors.primaryText, fontWeight: '600' },
});
