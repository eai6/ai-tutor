import { useEffect, useRef } from 'react';
import { Animated, StyleSheet, View } from 'react-native';

import { colors, radius, spacing } from '@/theme';

interface BlockProps {
  width?: number | string;
  height?: number;
  rounded?: number;
  style?: object;
}

export function SkeletonBlock({ width = '100%', height = 16, rounded, style }: BlockProps) {
  const opacity = useRef(new Animated.Value(0.5)).current;

  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(opacity, { toValue: 1, duration: 700, useNativeDriver: true }),
        Animated.timing(opacity, { toValue: 0.5, duration: 700, useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [opacity]);

  return (
    <Animated.View
      style={[
        {
          width: width as never,
          height,
          borderRadius: rounded ?? radius.sm,
          backgroundColor: colors.bgMuted,
          opacity,
        },
        style,
      ]}
    />
  );
}

export function SkeletonCard() {
  return (
    <View style={styles.card}>
      <SkeletonBlock width={80} height={10} />
      <SkeletonBlock width="70%" height={20} style={{ marginTop: spacing.sm }} />
      <SkeletonBlock width="90%" height={14} style={{ marginTop: spacing.sm }} />
      <SkeletonBlock width="60%" height={14} style={{ marginTop: spacing.xs }} />
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    padding: spacing.lg,
    borderWidth: 1,
    borderColor: colors.borderMuted,
    marginBottom: spacing.md,
  },
});
