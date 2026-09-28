from pydantic import BaseModel, Field


class QuestionHistoryItem(BaseModel):
    question: str = Field(description="The exact text of the question asked.")
    answer_summary: str = Field(
        description="A concise summary and evaluation of the candidate's answer."
    )
    score: float = Field(
        ge=0, le=10, description="Score assigned to the answer from 0 to 10."
    )


class InterviewState(BaseModel):
    turn: int = Field(
        ge=1, le=15, description="Current question turn number (1 to 15)."
    )
    last_question_score: float | None = Field(
        default=None,
        ge=0,
        le=10,
        description="Score of the previous answer (0-10), or None for Turn 1.",
    )
    question_history: list[QuestionHistoryItem] = Field(
        default_factory=list,
        description="Cumulative log of questions, summaries, and scores.",
    )
    skills_assessed: list[str] = Field(
        default_factory=list,
        description="List of must-have or matching skills assessed so far.",
    )
    next_target_skill: str | None = Field(
        default=None,
        description="Name of the skill targeted in the current question.",
    )
    strike_count: int = Field(
        default=0,
        ge=0,
        le=3,
        description="Count of consecutive adversarial or timeout strikes.",
    )


class FinalReport(BaseModel):
    overall_l1_score: float = Field(
        description="Weighted or average score across all answered questions."
    )
    question_scores: list[QuestionHistoryItem] = Field(
        description="Complete record of all questions, answers, and scores."
    )


class InterviewTurnResponse(BaseModel):
    is_interview_complete: bool = Field(
        description="Set to true if 15 turns are reached, all skills are assessed, 3 strikes occur, or candidate quits."
    )
    public_response: str = Field(
        description="The candidate-facing message. This must contain ONLY the greeting/question or closing message, with zero internal scoring or audit data."
    )
    interview_state: InterviewState = Field(
        description="Internal state tracking. Appended and maintained every turn."
    )
    final_report: FinalReport | None = Field(
        default=None,
        description="Populated ONLY when is_interview_complete is true. Otherwise null.",
    )