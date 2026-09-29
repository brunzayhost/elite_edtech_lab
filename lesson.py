"""Lesson plan schema.

One Pydantic model does two jobs:
1. ``LessonPlan.model_json_schema()`` is sent to Ollama as the ``format``
   parameter, so decoding is constrained to this structure.
2. ``LessonPlan.model_validate_json()`` re-validates the reply, including the
   rules below (min lengths etc.), before anything is saved.

This is a *research-defined* schema, not an official EDB format. Cite the
curriculum documents you derived the fields from in the methodology chapter.
Adjust the fields to match your repo's real schema.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LearningActivity(BaseModel):
    title: str = Field(min_length=3)
    description: str = Field(min_length=10)
    duration_minutes: int = Field(ge=1, le=120)


class LessonPlan(BaseModel):
    title_en: str = Field(min_length=3, description="Lesson title in English")
    title_zh: str = Field(min_length=2, description="Lesson title in Traditional Chinese (Hong Kong)")
    grade_level: str = Field(min_length=1, description="For example 'Primary 4'")
    key_learning_area: str = Field(min_length=3, description="EDB Key Learning Area")
    learning_objectives: list[str] = Field(min_length=1)
    activities: list[LearningActivity] = Field(min_length=1)
    assessment: str = Field(min_length=10)
    values_education_link: str = Field(
        min_length=10,
        description="How the lesson connects to values education (for example stewardship).",
    )
