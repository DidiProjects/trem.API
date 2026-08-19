from moviepy import VideoFileClip
import os

VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.wmv', '.flv', '.m4v'}


class VideoServiceError(Exception):
    """Exceção customizada para erros do serviço de vídeo"""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


def validate_cut_input(filename: str, start: float, end: float) -> str:
    """
    Valida os parâmetros de entrada para recorte de vídeo.
    
    Args:
        filename: Nome do arquivo
        start: Tempo inicial em segundos
        end: Tempo final em segundos
    
    Returns:
        Extensão do arquivo validada
    
    Raises:
        VideoServiceError: Se validação falhar
    """
    if not filename:
        raise VideoServiceError("Nome do arquivo é obrigatório")
    
    ext = os.path.splitext(filename)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        raise VideoServiceError(
            f"Formato não suportado. Use: {', '.join(sorted(VIDEO_EXTENSIONS))}"
        )
    
    if start < 0:
        raise VideoServiceError("Tempo inicial deve ser >= 0")
    
    if end <= start:
        raise VideoServiceError("Tempo final deve ser maior que o inicial")
    
    return ext


def cut_video(input_path: str, start: float, end: float, output_path: str) -> str:
    """
    Recorta um vídeo entre os tempos start e end (em segundos).
    
    Args:
        input_path: Caminho do arquivo de vídeo de entrada
        start: Tempo inicial em segundos
        end: Tempo final em segundos
        output_path: Caminho do arquivo de saída
    
    Returns:
        Caminho do arquivo de saída
    
    Raises:
        VideoServiceError: Se ocorrer erro no processamento
    """
    clip = None
    subclip = None
    
    try:
        clip = VideoFileClip(input_path)
        
        if end > clip.duration:
            end = clip.duration
        
        if start >= clip.duration:
            raise VideoServiceError(
                f"Tempo inicial ({start}s) excede a duração do vídeo ({clip.duration:.2f}s)"
            )
        
        subclip = clip.subclipped(start, end)
        subclip.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            logger=None
        )
        return output_path
    
    except VideoServiceError:
        raise
    except Exception as e:
        raise VideoServiceError(f"Erro ao processar vídeo: {str(e)}", status_code=500)
    
    finally:
        if subclip:
            subclip.close()
        if clip:
            clip.close()
