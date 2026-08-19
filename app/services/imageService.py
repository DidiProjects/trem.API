import io
import os
from typing import Literal
from PIL import Image

# Import lazy para evitar falha de startup quando libcairo não está disponível (dev local)
try:
    import cairosvg as _cairosvg
except OSError:
    _cairosvg = None  # type: ignore[assignment]

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.svg'}
OUTPUT_FORMATS = {'jpeg', 'png', 'webp', 'gif', 'bmp', 'tiff', 'svg'}


class ImageServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


def validate_image_file(filename: str) -> str:
    if not filename:
        raise ImageServiceError("Nome do arquivo é obrigatório")
    
    ext = os.path.splitext(filename)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        raise ImageServiceError(
            f"Formato não suportado. Use: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )
    return ext


def validate_output_format(format: str) -> str:
    format_lower = format.lower()
    if format_lower not in OUTPUT_FORMATS:
        raise ImageServiceError(
            f"Formato de saída não suportado. Use: {', '.join(sorted(OUTPUT_FORMATS))}"
        )
    return format_lower


def is_svg(content: bytes) -> bool:
    try:
        header = content[:1000].decode('utf-8', errors='ignore').lower()
        return '<svg' in header or ('<?xml' in header and 'svg' in header)
    except:
        return False


def images_to_pdf(
    image_contents: list[bytes],
    layout: Literal["single", "grouped"] = "single",
    images_per_page: int = 4
) -> io.BytesIO:
    if not image_contents:
        raise ImageServiceError("Nenhuma imagem fornecida")
    
    images = []
    for content in image_contents:
        if is_svg(content):
            png_bytes = _cairosvg.svg2png(bytestring=content, scale=2.0)
            img = Image.open(io.BytesIO(png_bytes))
        else:
            img = Image.open(io.BytesIO(content))
        
        if img.mode == 'RGBA':
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        images.append(img)
    
    if layout == "single":
        pdf_buffer = io.BytesIO()
        images[0].save(
            pdf_buffer,
            format='PDF',
            save_all=True,
            append_images=images[1:] if len(images) > 1 else []
        )
        pdf_buffer.seek(0)
        return pdf_buffer
    
    else:
        a4_width, a4_height = 2480, 3508
        margin = 50
        spacing = 30
        
        if images_per_page == 1:
            cols, rows = 1, 1
        elif images_per_page == 2:
            cols, rows = 1, 2
        elif images_per_page <= 4:
            cols, rows = 2, 2
        elif images_per_page <= 6:
            cols, rows = 2, 3
        else:
            cols, rows = 3, 3
            images_per_page = min(images_per_page, 9)
        
        cell_width = (a4_width - 2 * margin - (cols - 1) * spacing) // cols
        cell_height = (a4_height - 2 * margin - (rows - 1) * spacing) // rows
        
        pages = []
        for i in range(0, len(images), images_per_page):
            page_images = images[i:i + images_per_page]
            page = Image.new('RGB', (a4_width, a4_height), (255, 255, 255))
            
            for idx, img in enumerate(page_images):
                row = idx // cols
                col = idx % cols
                
                img_ratio = img.width / img.height
                cell_ratio = cell_width / cell_height
                
                if img_ratio > cell_ratio:
                    new_width = cell_width
                    new_height = int(cell_width / img_ratio)
                else:
                    new_height = cell_height
                    new_width = int(cell_height * img_ratio)
                
                resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                
                x = margin + col * (cell_width + spacing) + (cell_width - new_width) // 2
                y = margin + row * (cell_height + spacing) + (cell_height - new_height) // 2
                
                page.paste(resized, (x, y))
            
            pages.append(page)
        
        pdf_buffer = io.BytesIO()
        pages[0].save(
            pdf_buffer,
            format='PDF',
            save_all=True,
            append_images=pages[1:] if len(pages) > 1 else []
        )
        pdf_buffer.seek(0)
        return pdf_buffer


def convert_svg_to_png(content: bytes, scale: float = 1.0) -> bytes:
    return _cairosvg.svg2png(bytestring=content, scale=scale)


def convert_image(
    content: bytes,
    output_format: str,
    quality: int = 95,
    scale: float = 1.0
) -> tuple[io.BytesIO, str]:
    validate_output_format(output_format)
    
    output_buffer = io.BytesIO()
    
    if is_svg(content):
        if output_format == 'svg':
            output_buffer.write(content)
            output_buffer.seek(0)
            return output_buffer, 'svg'
        
        if output_format == 'png':
            png_bytes = _cairosvg.svg2png(bytestring=content, scale=scale)
            output_buffer.write(png_bytes)
            output_buffer.seek(0)
            return output_buffer, 'png'
        
        png_bytes = _cairosvg.svg2png(bytestring=content, scale=scale)
        img = Image.open(io.BytesIO(png_bytes))
    else:
        if output_format == 'svg':
            raise ImageServiceError("Conversão de imagem raster para SVG não é suportada")
        img = Image.open(io.BytesIO(content))
    
    if output_format in ('jpeg', 'jpg'):
        if img.mode in ('RGBA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'RGBA':
                bg.paste(img, mask=img.split()[3])
            else:
                bg.paste(img)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(output_buffer, format='JPEG', quality=quality)
        ext = 'jpg'
    
    elif output_format == 'png':
        img.save(output_buffer, format='PNG', optimize=True)
        ext = 'png'
    
    elif output_format == 'webp':
        img.save(output_buffer, format='WEBP', quality=quality)
        ext = 'webp'
    
    elif output_format == 'gif':
        if img.mode != 'P':
            img = img.convert('P', palette=Image.Palette.ADAPTIVE)
        img.save(output_buffer, format='GIF')
        ext = 'gif'
    
    elif output_format == 'bmp':
        if img.mode == 'RGBA':
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        img.save(output_buffer, format='BMP')
        ext = 'bmp'
    
    elif output_format == 'tiff':
        img.save(output_buffer, format='TIFF')
        ext = 'tiff'
    
    output_buffer.seek(0)
    return output_buffer, ext


def compress_image(
    content: bytes,
    quality: int = 70,
    max_dimension: int = None
) -> tuple[io.BytesIO, str, dict]:
    if quality < 1 or quality > 100:
        raise ImageServiceError("Qualidade deve estar entre 1 e 100")
    
    img = Image.open(io.BytesIO(content))
    original_format = img.format or 'JPEG'
    original_size = len(content)
    original_dimensions = img.size
    
    if max_dimension and (img.width > max_dimension or img.height > max_dimension):
        ratio = min(max_dimension / img.width, max_dimension / img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    
    output_buffer = io.BytesIO()
    
    if original_format.upper() in ('JPEG', 'JPG'):
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(output_buffer, format='JPEG', quality=quality, optimize=True)
        ext = 'jpg'
    elif original_format.upper() == 'PNG':
        img.save(output_buffer, format='PNG', optimize=True)
        ext = 'png'
    elif original_format.upper() == 'WEBP':
        img.save(output_buffer, format='WEBP', quality=quality)
        ext = 'webp'
    else:
        if img.mode in ('RGBA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'RGBA':
                bg.paste(img, mask=img.split()[3])
            else:
                bg.paste(img)
            img = bg
        img.save(output_buffer, format='JPEG', quality=quality, optimize=True)
        ext = 'jpg'
    
    output_buffer.seek(0)
    compressed_size = len(output_buffer.getvalue())
    
    stats = {
        "original_size": original_size,
        "compressed_size": compressed_size,
        "reduction_percent": round((1 - compressed_size / original_size) * 100, 2),
        "original_dimensions": original_dimensions,
        "final_dimensions": img.size
    }
    
    output_buffer.seek(0)
    return output_buffer, ext, stats
