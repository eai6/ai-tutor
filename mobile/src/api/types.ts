// Hand-written API types mirroring apps/api/serializers/*.
// Replace with `npm run schema:gen` (drf-spectacular → openapi-typescript)
// once the dev server is reachable from the build host.

export type Role = 'student' | 'teacher' | 'admin';

export interface Membership {
  institution_id: number;
  institution_slug: string;
  institution_name: string;
  role: Role;
}

export interface StudentProfile {
  school: string | null;
  grade_level: string | null;
}

export interface User {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  memberships: Membership[];
  student_profile: StudentProfile | null;
}

export interface TokenPair {
  access: string;
  refresh: string;
  user: User;
}

export interface Course {
  id: number;
  title: string;
  description: string;
  subject_type: string | null;
  is_math: boolean;
  grade_level: string;
  is_published: boolean;
  institution_id: number | null;
}

export interface Unit {
  id: number;
  course_id: number;
  title: string;
  description: string;
  grade_level: string;
  order_index: number;
}

export interface Lesson {
  id: number;
  unit_id: number;
  unit_title: string;
  course_id: number;
  course_title: string;
  title: string;
  objective: string;
  estimated_minutes: number | null;
  order_index: number;
  is_published: boolean;
  content_status: string;
  allow_group_mode: boolean;
  max_group_size: number | null;
  group_requires_approval: boolean;
}

export interface LessonStep {
  id: number;
  order_index: number;
  step_type: string;
  phase: string;
  concept_tag: string;
  enabling_objective: string;
  teacher_script: string;
  question: string;
  answer_type: string;
  choices: unknown;
  expected_answer: string;
  rubric: string;
  hint_1: string;
  hint_2: string;
  hint_3: string;
  max_attempts: number;
  media: unknown;
}

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface ProgressRow {
  id: number;
  lesson_id: number;
  lesson_title: string;
  mastery_level: string;
  best_score: number | null;
  attempts_count: number;
  last_session_at: string | null;
}
