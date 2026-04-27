import { useEffect, useRef } from 'react';
import { Animated, StyleSheet, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, fonts } from '@/theme';

interface Props {
  initial?: string;
  size?: number;
  variant?: 'tutor' | 'student';
  pulse?: boolean;
}

export function Avatar({ initial, size = 28, variant = 'tutor', pulse = false }: Props) {
  const isStudent = variant === 'student';
  const opacity = useRef(new Animated.Value(1)).current;

  useEffect(() => {
    if (!pulse) return;
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(opacity, { toValue: 0.4, duration: 750, useNativeDriver: true }),
        Animated.timing(opacity, { toValue: 1, duration: 750, useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [pulse, opacity]);

  if (isStudent) {
    return (
      <View
        style={[
          styles.base,
          {
            width: size,
            height: size,
            borderRadius: size / 2,
            backgroundColor: colors.text,
          },
        ]}
      >
        <Animated.Text
          style={{
            color: colors.primaryText,
            fontFamily: fonts.uiBold,
            fontSize: Math.max(11, size * 0.42),
          }}
        >
          {(initial || '?').slice(0, 1).toUpperCase()}
        </Animated.Text>
      </View>
    );
  }

  // Tutor avatar — abstract sparkle in a soft tinted disc.
  return (
    <Animated.View
      style={[
        styles.base,
        {
          width: size,
          height: size,
          borderRadius: size / 2,
          backgroundColor: colors.primarySoft,
          opacity,
        },
      ]}
    >
      <Ionicons name="sparkles" size={Math.max(13, size * 0.5)} color={colors.primary} />
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  base: {
    alignItems: 'center',
    justifyContent: 'center',
  },
});
