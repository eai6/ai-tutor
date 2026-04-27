import { useEffect, useRef } from 'react';
import { Animated, StyleSheet, View } from 'react-native';

import { Avatar } from './Avatar';
import { colors, spacing } from '@/theme';

export function TypingIndicator() {
  const dots = [
    useRef(new Animated.Value(0)).current,
    useRef(new Animated.Value(0)).current,
    useRef(new Animated.Value(0)).current,
  ];

  useEffect(() => {
    const animations = dots.map((dot, idx) =>
      Animated.loop(
        Animated.sequence([
          Animated.delay(idx * 160),
          Animated.timing(dot, { toValue: 1, duration: 380, useNativeDriver: true }),
          Animated.timing(dot, { toValue: 0, duration: 380, useNativeDriver: true }),
        ]),
      ),
    );
    animations.forEach((a) => a.start());
    return () => animations.forEach((a) => a.stop());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <View style={styles.row}>
      <View style={styles.avatarSlot}>
        <Avatar variant="tutor" size={28} pulse />
      </View>
      <View style={styles.dotsRow}>
        {dots.map((dot, i) => (
          <Animated.View
            key={i}
            style={[
              styles.dot,
              {
                opacity: dot.interpolate({ inputRange: [0, 1], outputRange: [0.25, 1] }),
                transform: [
                  {
                    translateY: dot.interpolate({ inputRange: [0, 1], outputRange: [0, -3] }),
                  },
                ],
              },
            ]}
          />
        ))}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    width: '100%',
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: spacing.md,
    marginBottom: spacing.lg,
    paddingHorizontal: spacing.lg,
  },
  avatarSlot: { width: 28, alignItems: 'center', paddingTop: 2 },
  dotsRow: {
    flexDirection: 'row',
    gap: 6,
    paddingTop: 10,
  },
  dot: {
    width: 6,
    height: 6,
    borderRadius: 3,
    backgroundColor: colors.textMuted,
  },
});
