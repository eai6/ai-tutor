import { useState } from 'react';
import { Link } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import { StyleSheet, Text, View } from 'react-native';
import { Controller, useForm } from 'react-hook-form';

import { login } from '@/api/auth';
import { extractErrorMessage } from '@/api/client';
import { Button } from '@/components/Button';
import { Screen } from '@/components/Screen';
import { TextField } from '@/components/TextField';
import { useAuthStore } from '@/stores/auth-store';
import { colors, spacing, typography } from '@/theme';

interface FormData {
  username: string;
  password: string;
}

export default function LoginScreen() {
  const setSession = useAuthStore((s) => s.setSession);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const {
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<FormData>({ defaultValues: { username: '', password: '' } });

  async function onSubmit(values: FormData) {
    setServerError(null);
    setSubmitting(true);
    try {
      const tokens = await login(values.username.trim(), values.password);
      await setSession(tokens.access, tokens.refresh, tokens.user);
    } catch (err) {
      setServerError(extractErrorMessage(err, 'Login failed'));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Screen>
      <View style={styles.brand}>
        <View style={styles.logoCircle}>
          <Ionicons name="sparkles" size={28} color={colors.primaryText} />
        </View>
        <Text style={styles.brandText}>AI Tutor</Text>
      </View>

      <View style={styles.header}>
        <Text style={typography.h1}>Welcome back</Text>
        <Text style={[typography.body, styles.subtitle]}>
          Sign in to continue your lessons.
        </Text>
      </View>

      <Controller
        control={control}
        name="username"
        rules={{ required: 'Username is required' }}
        render={({ field: { onChange, onBlur, value } }) => (
          <TextField
            label="Username"
            autoCapitalize="none"
            autoCorrect={false}
            placeholder="alice"
            value={value}
            onChangeText={onChange}
            onBlur={onBlur}
            error={errors.username?.message}
            returnKeyType="next"
          />
        )}
      />

      <Controller
        control={control}
        name="password"
        rules={{ required: 'Password is required' }}
        render={({ field: { onChange, onBlur, value } }) => (
          <TextField
            label="Password"
            secureTextEntry
            placeholder="••••••••"
            value={value}
            onChangeText={onChange}
            onBlur={onBlur}
            error={errors.password?.message}
            returnKeyType="go"
            onSubmitEditing={handleSubmit(onSubmit)}
          />
        )}
      />

      {serverError ? (
        <View style={styles.errorBar}>
          <Ionicons name="alert-circle" size={16} color={colors.danger} />
          <Text style={styles.serverError}>{serverError}</Text>
        </View>
      ) : null}

      <Button title="Sign in" onPress={handleSubmit(onSubmit)} loading={submitting} />

      <View style={styles.footer}>
        <Text style={typography.small}>Need an account? </Text>
        <Link href="/(auth)/register" style={styles.link}>
          Register
        </Link>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  brand: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    marginTop: spacing.xl,
  },
  logoCircle: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: colors.primary,
    alignItems: 'center',
    justifyContent: 'center',
  },
  brandText: { fontSize: 18, fontWeight: '700', color: colors.text },
  header: { gap: spacing.xs, marginTop: spacing.xl, marginBottom: spacing.md },
  subtitle: { color: colors.textMuted },
  errorBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    backgroundColor: colors.dangerSoft,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: 8,
    marginBottom: spacing.sm,
  },
  serverError: { color: colors.danger, flex: 1, fontSize: 13 },
  footer: {
    flexDirection: 'row',
    justifyContent: 'center',
    alignItems: 'center',
    marginTop: spacing.lg,
  },
  link: { color: colors.primary, fontWeight: '600', fontSize: 14 },
});
