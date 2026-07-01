from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.channel import ContentMode, OutputMode, ScriptSource

Potential = Literal["low", "medium", "high", "very_high"]
Platform = Literal["youtube", "tiktok", "instagram", "facebook"]
ScriptSourceRecommendation = Literal["reddit", "claude_generated"]
OutputModeRecommendation = Literal["youtube_and_shorts", "shorts_only"]


class PlatformSuitability(BaseModel):
    platform: Platform
    fit: Potential
    reasoning: str


class ResearchEditableConfig(BaseModel):
    channel_name: str
    description: str
    niche: str
    tone: str
    script_source: ScriptSource
    output_mode: OutputMode
    visual_style: str
    image_style: str
    languages: list[str] = Field(default_factory=list)
    platforms: list[Platform] = Field(default_factory=list)
    videos_per_week: int = 3
    subreddits: list[str] = Field(default_factory=list)
    story_generation_prompt: str | None = None


class ResearchRecommendation(BaseModel):
    recommended_channel_concept: str
    why_selected: str
    rpm_potential: Potential
    follower_growth_potential: Potential
    platform_suitability: list[PlatformSuitability]
    best_script_source: ScriptSourceRecommendation
    recommended_output_mode: OutputModeRecommendation
    recommended_visual_style: str
    recommended_image_style: str
    recommended_tone: str
    recommended_target_languages: list[str]
    recommended_platforms: list[Platform]
    suggested_channel_names: list[str]
    example_video_ideas: list[str]
    risks_difficulty: list[str]
    final_recommendation_summary: str
    assumption_note: str | None = None
    editable_config: ResearchEditableConfig


class ResearchAlternativeIdea(BaseModel):
    concept: str
    why_it_could_work: str
    main_tradeoff: str


class ResearchIdeasRequest(BaseModel):
    channel_description: str = ""
    mode: Literal["explore", "validate"] = "validate"
    content_mode: ContentMode = "single_story"
    target_languages: list[str] = Field(default_factory=list)
    target_platforms: list[Platform] = Field(default_factory=list)


class ResearchIdeasResponse(BaseModel):
    research_label: str = "AI market research estimate — not verified platform analytics"
    primary_recommendation: ResearchRecommendation
    alternative_ideas: list[ResearchAlternativeIdea] = Field(default_factory=list)
    references_used: list[str] = Field(
        default_factory=list,
        description="URLs or source references Claude used to inform this estimate. "
                    "Empty when web search was not available.",
    )
