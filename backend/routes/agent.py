import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.models.schemas import ChatRequest
from backend.services import claude_agent

router = APIRouter()


@router.post("/agent/chat")
async def chat(request: ChatRequest):
    """Stream agent replies as Server-Sent Events."""
    session_id = request.session_id

    def event_stream():
        try:
            for chunk in claude_agent.chat_turn(session_id, request.message):
                data = json.dumps({"type": "text", "content": chunk})
                yield f"data: {data}\n\n"

            export_ready   = claude_agent.is_export_ready(session_id)
            dates_updated  = claude_agent.pop_dates_updated(session_id)
            yield f"data: {json.dumps({'type': 'done', 'export_ready': export_ready, 'dates_updated': dates_updated})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/session/{session_id}/export")
async def export(session_id: str):
    """Return the assembled METI GeoJSON payload as a downloadable file."""
    payload = claude_agent.get_export_payload(session_id)
    if not payload:
        raise HTTPException(
            status_code=404,
            detail="Export not ready. Complete the agent conversation first."
        )

    content = json.dumps(payload, indent=2)
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="meti_{session_id[:8]}.json"'},
    )
