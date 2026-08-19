from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
import tempfile
import os
from app.api.v1.dependencies import require_profile
from app.domain.entities.user import User
from app.services.audioService import (
    transcribe,
    validate_transcription_input,
    cut_audio,
    validate_cut_input,
    AudioServiceError,
    AUDIO_EXTENSIONS
)

router = APIRouter()


def cleanup_files(*paths):
    """Remove arquivos temporários"""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass


@router.post("/cut")
async def audio_cut(
    file: UploadFile = File(...),
    start: float = Form(...),
    end: float = Form(...),
    _: User = Depends(require_profile("file_editor"))
):
    """
    Recorta um áudio entre os tempos definidos.
    """
    temp_in_path = None
    temp_out_path = None
    
    try:
        ext = validate_cut_input(file.filename, start, end)
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_in:
            temp_in.write(await file.read())
            temp_in_path = temp_in.name
        
        temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        temp_out_path = temp_out.name
        temp_out.close()
        
        cut_audio(temp_in_path, start, end, temp_out_path)
        
        original_name = os.path.splitext(file.filename or 'audio')[0]
        output_filename = f"{original_name}_recorte{ext}"
        
        media_types = {
            '.mp3': 'audio/mpeg',
            '.wav': 'audio/wav',
            '.m4a': 'audio/mp4',
            '.ogg': 'audio/ogg',
            '.flac': 'audio/flac',
            '.aac': 'audio/aac',
            '.wma': 'audio/x-ms-wma'
        }
        media_type = media_types.get(ext, 'audio/mpeg')
        
        return FileResponse(
            temp_out_path,
            filename=output_filename,
            media_type=media_type,
            background=BackgroundTask(cleanup_files, temp_in_path, temp_out_path)
        )
    
    except AudioServiceError as e:
        cleanup_files(temp_in_path, temp_out_path)
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        cleanup_files(temp_in_path, temp_out_path)
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


@router.post("/transcribe")
async def audio_transcribe(
    file: UploadFile = File(...),
    language: str = Form(None),
    _: User = Depends(require_profile("file_editor"))
):
    """
    Transcreve o áudio de um arquivo de vídeo ou áudio.
    Retorna um JSON com a transcrição completa e segmentos com timestamps.
    """
    temp_path = None
    
    try:
        ext = validate_transcription_input(file.filename, language)
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
            temp_file.write(await file.read())
            temp_path = temp_file.name
        
        result = transcribe(temp_path, language)
        
        return JSONResponse(content=result)
    
    except AudioServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    finally:
        cleanup_files(temp_path)
