from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional
from app.api.v1.dependencies import get_current_active_user
from app.domain.entities.user import User
from app.services.emailService import send_feedback_email, EmailServiceError

router = APIRouter()


@router.post("/feedback")
async def send_feedback(
    type: str = Form(...),
    message: str = Form(...),
    email: Optional[str] = Form(None),
    _: User = Depends(get_current_active_user)
):
    """
    Enviar feedback, sugestão ou reporte de bug.
    
    - **type**: Tipo do feedback (suggestion, bug, other)
    - **message**: Mensagem do feedback (max 5000 caracteres)
    - **email**: Email para contato (opcional)
    """
    if type not in ("suggestion", "bug", "other"):
        raise HTTPException(
            status_code=400,
            detail="Tipo deve ser 'suggestion', 'bug' ou 'other'"
        )
    
    try:
        send_feedback_email(
            feedback_type=type,
            message=message,
            user_email=email
        )
        
        return JSONResponse(content={
            "success": True,
            "message": "Feedback enviado com sucesso! Obrigado pela contribuição."
        })
    
    except EmailServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
