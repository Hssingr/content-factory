from pydantic import BaseModel


class SuggestRequest(BaseModel):
    field: str
    context: dict


class SuggestResponse(BaseModel):
    field: str
    suggestion: str
