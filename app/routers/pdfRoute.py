import io
from typing import List, Literal, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
import pikepdf
from app.api.v1.dependencies import require_profile
from app.domain.entities.user import User
from app.services import PdfService
from app.utils import safe_filename, get_output_filename
from app.utils.security import validate_pdf_upload, sanitize_filename

router = APIRouter()


@router.post("/split")
async def split_pdf(
    file: UploadFile = File(...),
    pages: str = Form(...),
    _: User = Depends(require_profile("file_editor"))
):
    content = await validate_pdf_upload(file)
    
    try:
        output, _ = PdfService.split(content, pages)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={safe_filename(get_output_filename(sanitize_filename(file.filename), 'split'))}"}
    )


@router.post("/extract-pages")
async def extract_pages(
    file: UploadFile = File(...),
    _: User = Depends(require_profile("file_editor"))
):
    content = await validate_pdf_upload(file)
    
    try:
        zip_buffer = PdfService.extract_pages(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    output_name = sanitize_filename(file.filename).rsplit(".", 1)[0] + "-extracted.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={safe_filename(output_name)}"}
    )


@router.post("/merge")
async def merge_pdfs(
    files: List[UploadFile] = File(...),
    _: User = Depends(require_profile("file_editor"))
):
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 PDF files")
    
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Maximum of 20 files at a time")
    
    contents = []
    for file in files:
        content = await validate_pdf_upload(file)
        contents.append((sanitize_filename(file.filename), content))
    
    try:
        output = PdfService.merge(contents)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    first_name = sanitize_filename(files[0].filename).rsplit(".", 1)[0]
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={safe_filename(first_name + '-merged.pdf')}"}
    )


@router.post("/add-password")
async def add_password(
    file: UploadFile = File(...),
    user_password: str = Form(...),
    owner_password: Optional[str] = Form(None),
    _: User = Depends(require_profile("file_editor"))
):
    content = await validate_pdf_upload(file)
    
    try:
        output = PdfService.add_password(content, user_password, owner_password)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={safe_filename(get_output_filename(sanitize_filename(file.filename), 'protected'))}"}
    )


@router.post("/remove-password")
async def remove_password(
    file: UploadFile = File(...),
    password: str = Form(...),
    _: User = Depends(require_profile("file_editor"))
):
    content = await validate_pdf_upload(file)
    
    try:
        output = PdfService.remove_password(content, password)
    except pikepdf.PasswordError:
        raise HTTPException(status_code=400, detail="Incorrect password")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={safe_filename(get_output_filename(sanitize_filename(file.filename), 'unlocked'))}"}
    )


@router.post("/info")
async def pdf_info(
    file: UploadFile = File(...),
    _: User = Depends(require_profile("file_editor"))
):
    content = await validate_pdf_upload(file)
    
    try:
        return PdfService.get_info(content, sanitize_filename(file.filename))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")


@router.post("/convert-to-image")
async def convert_to_image(
    file: UploadFile = File(...),
    format: Literal["png", "jpeg", "tiff"] = Form("png"),
    dpi: int = Form(150),
    pages: Optional[str] = Form(None),
    _: User = Depends(require_profile("file_editor"))
):
    if dpi < 72 or dpi > 600:
        raise HTTPException(status_code=400, detail="DPI must be between 72 and 600")
    
    content = await validate_pdf_upload(file)
    
    try:
        buffer, ext, is_single, page_num, mime_type = PdfService.convert_to_image(content, format, dpi, pages)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    base_name = sanitize_filename(file.filename).rsplit(".", 1)[0]
    if is_single:
        output_name = f"{base_name}_page_{page_num}.{ext}"
    else:
        output_name = f"{base_name}-images-{format}.zip"
    
    return StreamingResponse(
        buffer,
        media_type=mime_type,
        headers={"Content-Disposition": f"attachment; filename={safe_filename(output_name)}"}
    )


@router.post("/convert-to-ofx")
async def convert_to_ofx(
    file: UploadFile = File(...),
    bank_id: str = Form("0000"),
    account_id: str = Form("0000000000"),
    account_type: Literal["CHECKING", "SAVINGS", "CREDITCARD"] = Form("CHECKING"),
    _: User = Depends(require_profile("file_editor"))
):
    content = await validate_pdf_upload(file)
    
    try:
        ofx_content = PdfService.convert_to_ofx(content, bank_id, account_id, account_type)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    if not ofx_content:
        raise HTTPException(
            status_code=400, 
            detail="Could not extract transactions from PDF. Please verify it is a valid bank statement."
        )
    
    output_name = sanitize_filename(file.filename).rsplit(".", 1)[0] + ".ofx"
    return StreamingResponse(
        io.BytesIO(ofx_content.encode("utf-8")),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={safe_filename(output_name)}"}
    )


@router.post("/extract-text")
async def extract_text(
    file: UploadFile = File(...),
    _: User = Depends(require_profile("file_editor"))
):
    content = await validate_pdf_upload(file)
    
    try:
        pages_text = PdfService.extract_text(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")
    
    return {
        "filename": sanitize_filename(file.filename),
        "total_pages": len(pages_text),
        "pages": pages_text
    }
