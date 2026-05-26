"""apps.tutoring.v2 — New conversational tutor engine.

Phased refactor per design/refactor/refactor-implementation-plan.md.
Phase 1 lands typed contracts, service skeletons, the kill-switch
plumbing, and schema-tight tools. Phase 2 fills in StudentGrader /
StudentTutor / conformance. Phase 3 flips NEW_TUTOR default-on.

The legacy ConversationalTutor at apps/tutoring/conversational_tutor.py
keeps serving every session until Phase 3.
"""
