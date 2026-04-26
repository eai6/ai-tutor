import { useState } from 'react';
import { Link } from 'expo-router';
import { StyleSheet, Text, View } from 'react-native';
import { Controller, useForm } from 'react-hook-form';

import { register } from '@/api/auth';
import { extractErrorMessage } from '@/api/client';
import { Button } from '@/components/Button';
import { Screen } from '@/components/Screen';
import { TextField } from '@/components/TextField';
import { useAuthStore } from '@/stores/auth-store';
import { colors, spacing, typography } from '@/theme';

interface FormData {
  username: string;
  password: string;
  email: string;
  institution_slug: string;
  grade_level: string;
}

export default function RegisterScreen() {
  const setSession = useAuthStore((s) => s.setSession);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const {
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<FormData>({
    defaultValues: {
      username: '',
      password: '',
      email: '',
      institution_slug: '',
      grade_level: 'S3',
    },
  });

  async function onSubmit(values: FormData) {
    setServerError(null);
    setSubmitting(true);
    try {
      const tokens = await register({
        username: values.username.trim(),
        password: values.password,
        email: values.email.trim() || undefined,
        institution_slug: values.institution_slug.trim() || undefined,
        grade_level: values.grade_level.trim() || undefined,
      });
      await setSession(tokens.access, tokens.refresh, tokens.user);
    } catch (err) {
      setServerError(extractErrorMessage(err, 'Registration failed'));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Screen>
      <View style={styles.header}>
        <Text style={typography.h1}>Create account</Text>
        <Text style={[typography.body, styles.subtitle]}>
          Use your school code if you have one.
        </Text>
      </View>

      <Controller
        control={control}
        name="username"
        rules={{ required: 'Username is required', minLength: { value: 3, message: 'Too short' } }}
        render={({ field: { onChange, onBlur, value } }) => (
          <TextField
            label="Username"
            autoCapitalize="none"
            autoCorrect={false}
            value={value}
            onChangeText={onChange}
            onBlur={onBlur}
            error={errors.username?.message}
          />
        )}
      />

      <Controller
        control={control}
        name="password"
        rules={{ required: 'Password is required', minLength: { value: 8, message: 'Min 8 chars' } }}
        render={({ field: { onChange, onBlur, value } }) => (
          <TextField
            label="Password"
            secureTextEntry
            value={value}
            onChangeText={onChange}
            onBlur={onBlur}
            error={errors.password?.message}
          />
        )}
      />

      <Controller
        control={control}
        name="email"
        render={({ field: { onChange, onBlur, value } }) => (
          <TextField
            label="Email (optional)"
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="email-address"
            value={value}
            onChangeText={onChange}
            onBlur={onBlur}
          />
        )}
      />

      <Controller
        control={control}
        name="institution_slug"
        render={({ field: { onChange, onBlur, value } }) => (
          <TextField
            label="School code (optional)"
            autoCapitalize="none"
            autoCorrect={false}
            placeholder="e.g. seychelles-pilot"
            value={value}
            onChangeText={onChange}
            onBlur={onBlur}
          />
        )}
      />

      <Controller
        control={control}
        name="grade_level"
        render={({ field: { onChange, onBlur, value } }) => (
          <TextField
            label="Grade level"
            placeholder="S3"
            value={value}
            onChangeText={onChange}
            onBlur={onBlur}
          />
        )}
      />

      {serverError ? <Text style={styles.serverError}>{serverError}</Text> : null}

      <Button title="Create account" onPress={handleSubmit(onSubmit)} loading={submitting} />

      <View style={styles.footer}>
        <Text style={typography.small}>Already have an account? </Text>
        <Link href="/(auth)/login" style={styles.link}>
          Sign in
        </Link>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: { gap: spacing.xs, marginBottom: spacing.lg, marginTop: spacing.xl },
  subtitle: { color: colors.textMuted },
  serverError: { color: colors.danger, marginBottom: spacing.sm },
  footer: {
    flexDirection: 'row',
    justifyContent: 'center',
    alignItems: 'center',
    marginTop: spacing.lg,
  },
  link: { color: colors.primary, fontWeight: '600', fontSize: 14 },
});
