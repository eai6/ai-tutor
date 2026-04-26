import { apiClient } from './client';
import type { Course, Lesson, LessonStep, Paginated, ProgressRow } from './types';

export async function listCourses(): Promise<Course[]> {
  const res = await apiClient.get<Paginated<Course>>('/courses/');
  return res.data.results;
}

export async function listLessons(courseId?: number, unitId?: number): Promise<Lesson[]> {
  const params: Record<string, string | number> = {};
  if (courseId) params.course_id = courseId;
  if (unitId) params.unit_id = unitId;
  const res = await apiClient.get<Paginated<Lesson>>('/lessons/', { params });
  return res.data.results;
}

export async function getLesson(lessonId: number): Promise<Lesson> {
  const res = await apiClient.get<Lesson>(`/lessons/${lessonId}/`);
  return res.data;
}

export async function listLessonSteps(lessonId: number): Promise<LessonStep[]> {
  const res = await apiClient.get<LessonStep[] | Paginated<LessonStep>>(
    `/lessons/${lessonId}/steps/`,
  );
  return Array.isArray(res.data) ? res.data : res.data.results;
}

export async function listProgress(): Promise<ProgressRow[]> {
  const res = await apiClient.get<Paginated<ProgressRow>>('/progress/');
  return res.data.results;
}
