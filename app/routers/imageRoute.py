from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.background import BackgroundTask
import tempfile
import os
import io
import base64
from typing import List
from app.api.v1.dependencies import require_profile
from app.domain.entities.user import User
from app.services.imageService import (
    images_to_pdf,
    convert_image,
    compress_image,
    validate_image_file,
    validate_output_format,
    ImageServiceError,
    IMAGE_EXTENSIONS
)

router = APIRouter()


def cleanup_files(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass


@router.post("/to-pdf")
async def images_to_pdf_endpoint(
    files: List[UploadFile] = File(...),
    layout: str = Form("single"),
    images_per_page: int = Form(4),
    _: User = Depends(require_profile("file_editor"))
):
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado")
    
    if layout not in ("single", "grouped"):
        raise HTTPException(status_code=400, detail="Layout deve ser 'single' ou 'grouped'")
    
    if images_per_page < 1 or images_per_page > 9:
        raise HTTPException(status_code=400, detail="Imagens por página deve ser entre 1 e 9")
    
    try:
        image_contents = []
        for file in files:
            validate_image_file(file.filename)
            content = await file.read()
            image_contents.append(content)
        
        pdf_buffer = images_to_pdf(image_contents, layout, images_per_page)
        
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=images_converted.pdf"
            }
        )
    
    except ImageServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


@router.post("/convert")
async def convert_image_endpoint(
    file: UploadFile = File(...),
    format: str = Form(...),
    quality: int = Form(95),
    _: User = Depends(require_profile("file_editor"))
):
    try:
        validate_image_file(file.filename)
        validate_output_format(format)
        
        if quality < 1 or quality > 100:
            raise HTTPException(status_code=400, detail="Qualidade deve estar entre 1 e 100")
        
        content = await file.read()
        output_buffer, ext = convert_image(content, format, quality)
        
        original_name = os.path.splitext(file.filename or 'image')[0]
        output_filename = f"{original_name}.{ext}"
        
        media_types = {
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'webp': 'image/webp',
            'gif': 'image/gif',
            'bmp': 'image/bmp',
            'tiff': 'image/tiff'
        }
        
        return StreamingResponse(
            output_buffer,
            media_type=media_types.get(ext, 'application/octet-stream'),
            headers={
                "Content-Disposition": f"attachment; filename={output_filename}"
            }
        )

    except ImageServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except HTTPException:
        raise  # já é um erro de cliente — não remapear para 500
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


@router.post("/compress")
async def compress_image_endpoint(
    file: UploadFile = File(...),
    quality: int = Form(70),
    max_dimension: int = Form(None),
    response_type: str = Form("file"),
    _: User = Depends(require_profile("file_editor"))
):
    """
    Comprime uma imagem.
    
    - **response_type**: 
      - `file` (padrão): Retorna o arquivo comprimido para download
      - `json`: Retorna JSON com métricas e arquivo em base64 (ideal para Swagger)
    """
    try:
        validate_image_file(file.filename)
        
        if response_type not in ("file", "json"):
            raise HTTPException(status_code=400, detail="response_type deve ser 'file' ou 'json'")
        
        content = await file.read()
        output_buffer, ext, stats = compress_image(content, quality, max_dimension)
        
        original_name = os.path.splitext(file.filename or 'image')[0]
        output_filename = f"{original_name}_compressed.{ext}"
        
        media_types = {
            'jpg': 'image/jpeg',
            'png': 'image/png',
            'webp': 'image/webp'
        }
        
        if response_type == "json":
            file_bytes = output_buffer.getvalue()
            return JSONResponse(content={
                "metrics": {
                    "original_size_bytes": stats["original_size"],
                    "compressed_size_bytes": stats["compressed_size"],
                    "reduction_percent": stats["reduction_percent"],
                    "original_dimensions": {
                        "width": stats["original_dimensions"][0],
                        "height": stats["original_dimensions"][1]
                    },
                    "final_dimensions": {
                        "width": stats["final_dimensions"][0],
                        "height": stats["final_dimensions"][1]
                    }
                },
                "file": {
                    "filename": output_filename,
                    "media_type": media_types.get(ext, 'image/jpeg'),
                    "size_bytes": len(file_bytes),
                    "base64": base64.b64encode(file_bytes).decode('utf-8')
                }
            })
        
        return StreamingResponse(
            output_buffer,
            media_type=media_types.get(ext, 'image/jpeg'),
            headers={
                "Content-Disposition": f"attachment; filename={output_filename}",
                "X-Original-Size": str(stats["original_size"]),
                "X-Compressed-Size": str(stats["compressed_size"]),
                "X-Reduction-Percent": str(stats["reduction_percent"])
            }
        )

    except ImageServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except HTTPException:
        raise  # já é um erro de cliente — não remapear para 500
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


@router.post("/compress/info")
async def compress_image_info_endpoint(
    file: UploadFile = File(...),
    quality: int = Form(70),
    max_dimension: int = Form(None),
    include_file: bool = Form(False),
    _: User = Depends(require_profile("file_editor"))
):
    """
    Retorna métricas de compressão em JSON.
    
    - **include_file**: Se True, inclui o arquivo comprimido em base64 na resposta
    """
    try:
        validate_image_file(file.filename)
        
        content = await file.read()
        output_buffer, ext, stats = compress_image(content, quality, max_dimension)
        
        original_name = os.path.splitext(file.filename or 'image')[0]
        output_filename = f"{original_name}_compressed.{ext}"
        
        media_types = {
            'jpg': 'image/jpeg',
            'png': 'image/png',
            'webp': 'image/webp'
        }
        
        response = {
            "metrics": {
                "original_size_bytes": stats["original_size"],
                "compressed_size_bytes": stats["compressed_size"],
                "reduction_percent": stats["reduction_percent"],
                "original_dimensions": {
                    "width": stats["original_dimensions"][0],
                    "height": stats["original_dimensions"][1]
                },
                "final_dimensions": {
                    "width": stats["final_dimensions"][0],
                    "height": stats["final_dimensions"][1]
                }
            }
        }
        
        if include_file:
            file_bytes = output_buffer.getvalue()
            response["file"] = {
                "filename": output_filename,
                "media_type": media_types.get(ext, 'image/jpeg'),
                "size_bytes": len(file_bytes),
                "base64": base64.b64encode(file_bytes).decode('utf-8')
            }
        
        return JSONResponse(content=response)
    
    except ImageServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
