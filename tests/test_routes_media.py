"""
Testes das rotas de mídia: /movie, /audio, /image e /support.

O processamento pesado (ffmpeg via moviepy, Whisper, SMTP) é mockado — o que
se testa aqui é o contrato HTTP: autenticação, perfil, validação de entrada,
códigos de status e headers.

As rotas de imagem rodam contra o Pillow de verdade, porque é barato.
"""
import io
from unittest.mock import patch

import pytest

from app.services.audioService import AudioServiceError
from app.services.emailService import EmailServiceError
from app.services.imageService import _cairosvg
from app.services.videoService import VideoServiceError

requires_cairo = pytest.mark.skipif(
    _cairosvg is None,
    reason="libcairo não disponível (Windows local); o SVG roda no container",
)


def _upload(name, content=b"conteudo-binario-falso", mime="application/octet-stream"):
    return {"file": (name, io.BytesIO(content), mime)}


# ---------------------------------------------------------------------------
# /movie
# ---------------------------------------------------------------------------

class TestMovieCut:
    def test_success_returns_video(self, client):
        with patch("app.routers.videoRoute.cut_video") as cut:
            cut.return_value = "saida.mp4"
            response = client.post(
                "/movie/cut", files=_upload("filme.mp4"), data={"start": 0, "end": 5}
            )
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"

    def test_output_filename_is_derived_from_input(self, client):
        with patch("app.routers.videoRoute.cut_video"):
            response = client.post(
                "/movie/cut", files=_upload("ferias.mp4"), data={"start": 0, "end": 5}
            )
        assert "ferias_recorte.mp4" in response.headers["content-disposition"]

    def test_unsupported_extension_returns_400(self, client):
        response = client.post(
            "/movie/cut", files=_upload("arquivo.txt"), data={"start": 0, "end": 5}
        )
        assert response.status_code == 400

    def test_end_before_start_returns_400(self, client):
        response = client.post(
            "/movie/cut", files=_upload("filme.mp4"), data={"start": 10, "end": 5}
        )
        assert response.status_code == 400

    def test_negative_start_returns_400(self, client):
        response = client.post(
            "/movie/cut", files=_upload("filme.mp4"), data={"start": -1, "end": 5}
        )
        assert response.status_code == 400

    def test_missing_times_returns_422(self, client):
        response = client.post("/movie/cut", files=_upload("filme.mp4"))
        assert response.status_code == 422

    def test_service_error_propagates_status(self, client):
        with patch("app.routers.videoRoute.cut_video") as cut:
            cut.side_effect = VideoServiceError("ffmpeg falhou", status_code=500)
            response = client.post(
                "/movie/cut", files=_upload("filme.mp4"), data={"start": 0, "end": 5}
            )
        assert response.status_code == 500

    def test_requires_authentication(self, client_no_auth):
        response = client_no_auth.post(
            "/movie/cut", files=_upload("filme.mp4"), data={"start": 0, "end": 5}
        )
        assert response.status_code == 401

    def test_requires_file_editor_profile(self, client_airline):
        response = client_airline.post(
            "/movie/cut", files=_upload("filme.mp4"), data={"start": 0, "end": 5}
        )
        assert response.status_code == 403

    def test_blocked_while_password_change_pending(self, client_must_change):
        response = client_must_change.post(
            "/movie/cut", files=_upload("filme.mp4"), data={"start": 0, "end": 5}
        )
        assert response.status_code == 403


class TestMovieTranscribe:
    def test_success_returns_transcription(self, client):
        with patch("app.routers.videoRoute.transcribe") as tr:
            tr.return_value = {
                "text": "olá mundo",
                "segments": [{"start": 0.0, "end": 1.5, "text": "olá mundo"}],
                "language": "pt",
                "duration": 1.5,
            }
            response = client.post(
                "/movie/transcribe", files=_upload("filme.mp4"), data={"language": "pt"}
            )
        assert response.status_code == 200
        assert response.json()["text"] == "olá mundo"

    def test_works_without_language(self, client):
        with patch("app.routers.videoRoute.transcribe") as tr:
            tr.return_value = {"text": "auto", "segments": [], "language": "en", "duration": 1}
            response = client.post("/movie/transcribe", files=_upload("filme.mp4"))
        assert response.status_code == 200

    def test_unsupported_language_returns_400(self, client):
        response = client.post(
            "/movie/transcribe", files=_upload("filme.mp4"), data={"language": "xx"}
        )
        assert response.status_code == 400

    def test_requires_authentication(self, client_no_auth):
        response = client_no_auth.post("/movie/transcribe", files=_upload("filme.mp4"))
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# /audio
# ---------------------------------------------------------------------------

class TestAudioCut:
    def test_success_returns_audio(self, client):
        with patch("app.routers.audioRoute.cut_audio"):
            response = client.post(
                "/audio/cut", files=_upload("musica.mp3"), data={"start": 0, "end": 10}
            )
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("a.mp3", "audio/mpeg"),
            ("a.wav", "audio/wav"),
            ("a.ogg", "audio/ogg"),
            ("a.flac", "audio/flac"),
        ],
    )
    def test_media_type_matches_extension(self, client, filename, expected):
        with patch("app.routers.audioRoute.cut_audio"):
            response = client.post(
                "/audio/cut", files=_upload(filename), data={"start": 0, "end": 10}
            )
        assert response.headers["content-type"] == expected

    def test_video_extension_is_rejected(self, client):
        """/audio/cut só aceita áudio — vídeo tem rota própria."""
        response = client.post(
            "/audio/cut", files=_upload("filme.mp4"), data={"start": 0, "end": 10}
        )
        assert response.status_code == 400

    def test_service_error_propagates_status(self, client):
        with patch("app.routers.audioRoute.cut_audio") as cut:
            cut.side_effect = AudioServiceError("falhou", status_code=500)
            response = client.post(
                "/audio/cut", files=_upload("musica.mp3"), data={"start": 0, "end": 10}
            )
        assert response.status_code == 500

    def test_requires_file_editor_profile(self, client_airline):
        response = client_airline.post(
            "/audio/cut", files=_upload("musica.mp3"), data={"start": 0, "end": 10}
        )
        assert response.status_code == 403


class TestAudioTranscribe:
    def test_success_returns_segments(self, client):
        with patch("app.routers.audioRoute.transcribe") as tr:
            tr.return_value = {
                "text": "texto",
                "segments": [{"start": 0.0, "end": 2.0, "text": "texto"}],
                "language": "pt",
                "duration": 2.0,
            }
            response = client.post("/audio/transcribe", files=_upload("musica.mp3"))
        assert response.status_code == 200
        assert len(response.json()["segments"]) == 1

    def test_accepts_video_file(self, client):
        """A transcrição aceita vídeo também — extrai a trilha de áudio."""
        with patch("app.routers.audioRoute.transcribe") as tr:
            tr.return_value = {"text": "t", "segments": [], "language": "pt", "duration": 1}
            response = client.post("/audio/transcribe", files=_upload("filme.mp4"))
        assert response.status_code == 200

    def test_unsupported_extension_returns_400(self, client):
        response = client.post("/audio/transcribe", files=_upload("planilha.xlsx"))
        assert response.status_code == 400

    def test_video_without_audio_track_returns_400(self, client):
        with patch("app.routers.audioRoute.transcribe") as tr:
            tr.side_effect = AudioServiceError("O vídeo não possui trilha de áudio")
            response = client.post("/audio/transcribe", files=_upload("mudo.mp4"))
        assert response.status_code == 400

    def test_requires_authentication(self, client_no_auth):
        response = client_no_auth.post("/audio/transcribe", files=_upload("musica.mp3"))
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# /image
# ---------------------------------------------------------------------------

class TestImageToPdf:
    def test_single_layout_returns_pdf(self, client, sample_png_bytes):
        response = client.post(
            "/image/to-pdf",
            files=[("files", ("a.png", io.BytesIO(sample_png_bytes), "image/png"))],
            data={"layout": "single"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_grouped_layout_returns_pdf(self, client, sample_png_bytes, sample_jpeg_bytes):
        response = client.post(
            "/image/to-pdf",
            files=[
                ("files", ("a.png", io.BytesIO(sample_png_bytes), "image/png")),
                ("files", ("b.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")),
            ],
            data={"layout": "grouped", "images_per_page": 4},
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")

    def test_rgba_image_is_flattened(self, client, sample_rgba_png_bytes):
        response = client.post(
            "/image/to-pdf",
            files=[("files", ("a.png", io.BytesIO(sample_rgba_png_bytes), "image/png"))],
        )
        assert response.status_code == 200

    def test_invalid_layout_returns_400(self, client, sample_png_bytes):
        response = client.post(
            "/image/to-pdf",
            files=[("files", ("a.png", io.BytesIO(sample_png_bytes), "image/png"))],
            data={"layout": "diagonal"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("per_page", [0, 10])
    def test_images_per_page_out_of_range_returns_400(
        self, client, sample_png_bytes, per_page
    ):
        response = client.post(
            "/image/to-pdf",
            files=[("files", ("a.png", io.BytesIO(sample_png_bytes), "image/png"))],
            data={"layout": "grouped", "images_per_page": per_page},
        )
        assert response.status_code == 400

    def test_unsupported_extension_returns_400(self, client, sample_png_bytes):
        response = client.post(
            "/image/to-pdf",
            files=[("files", ("a.txt", io.BytesIO(sample_png_bytes), "text/plain"))],
        )
        assert response.status_code == 400

    def test_requires_file_editor_profile(self, client_airline, sample_png_bytes):
        response = client_airline.post(
            "/image/to-pdf",
            files=[("files", ("a.png", io.BytesIO(sample_png_bytes), "image/png"))],
        )
        assert response.status_code == 403


class TestImageConvert:
    @pytest.mark.parametrize(
        "fmt,content_type",
        [
            ("png", "image/png"),
            ("jpeg", "image/jpeg"),
            ("webp", "image/webp"),
            ("gif", "image/gif"),
            ("bmp", "image/bmp"),
            ("tiff", "image/tiff"),
        ],
    )
    def test_converts_to_each_format(self, client, sample_png_bytes, fmt, content_type):
        response = client.post(
            "/image/convert",
            files={"file": ("a.png", io.BytesIO(sample_png_bytes), "image/png")},
            data={"format": fmt},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == content_type

    def test_raster_to_svg_returns_400(self, client, sample_png_bytes):
        response = client.post(
            "/image/convert",
            files={"file": ("a.png", io.BytesIO(sample_png_bytes), "image/png")},
            data={"format": "svg"},
        )
        assert response.status_code == 400

    def test_invalid_output_format_returns_400(self, client, sample_png_bytes):
        response = client.post(
            "/image/convert",
            files={"file": ("a.png", io.BytesIO(sample_png_bytes), "image/png")},
            data={"format": "heic"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("quality", [0, 101])
    def test_quality_out_of_range_returns_400(self, client, sample_png_bytes, quality):
        response = client.post(
            "/image/convert",
            files={"file": ("a.png", io.BytesIO(sample_png_bytes), "image/png")},
            data={"format": "jpeg", "quality": quality},
        )
        assert response.status_code == 400

    def test_rgba_to_jpeg_is_flattened(self, client, sample_rgba_png_bytes):
        response = client.post(
            "/image/convert",
            files={"file": ("a.png", io.BytesIO(sample_rgba_png_bytes), "image/png")},
            data={"format": "jpeg"},
        )
        assert response.status_code == 200

    @requires_cairo
    def test_svg_to_png(self, client, sample_svg_bytes):
        response = client.post(
            "/image/convert",
            files={"file": ("a.svg", io.BytesIO(sample_svg_bytes), "image/svg+xml")},
            data={"format": "png"},
        )
        assert response.status_code == 200

    def test_requires_authentication(self, client_no_auth, sample_png_bytes):
        response = client_no_auth.post(
            "/image/convert",
            files={"file": ("a.png", io.BytesIO(sample_png_bytes), "image/png")},
            data={"format": "jpeg"},
        )
        assert response.status_code == 401


class TestImageCompress:
    def test_returns_file_by_default(self, client, sample_jpeg_bytes):
        response = client.post(
            "/image/compress",
            files={"file": ("a.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")},
            data={"quality": 50},
        )
        assert response.status_code == 200
        assert "X-Original-Size" in response.headers
        assert "X-Reduction-Percent" in response.headers

    def test_json_response_carries_metrics_and_base64(self, client, sample_jpeg_bytes):
        response = client.post(
            "/image/compress",
            files={"file": ("a.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")},
            data={"quality": 50, "response_type": "json"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "metrics" in body
        assert "base64" in body["file"]

    def test_invalid_response_type_returns_400(self, client, sample_jpeg_bytes):
        response = client.post(
            "/image/compress",
            files={"file": ("a.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")},
            data={"response_type": "xml"},
        )
        assert response.status_code == 400

    def test_invalid_quality_returns_400(self, client, sample_jpeg_bytes):
        response = client.post(
            "/image/compress",
            files={"file": ("a.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")},
            data={"quality": 0},
        )
        assert response.status_code == 400

    def test_max_dimension_resizes(self, client, sample_png_bytes):
        response = client.post(
            "/image/compress",
            files={"file": ("a.png", io.BytesIO(sample_png_bytes), "image/png")},
            data={"quality": 70, "max_dimension": 40, "response_type": "json"},
        )
        assert response.status_code == 200
        final = response.json()["metrics"]["final_dimensions"]
        assert max(final["width"], final["height"]) <= 40

    def test_info_endpoint_omits_file_by_default(self, client, sample_jpeg_bytes):
        response = client.post(
            "/image/compress/info",
            files={"file": ("a.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")},
        )
        assert response.status_code == 200
        assert "file" not in response.json()

    def test_info_endpoint_includes_file_when_asked(self, client, sample_jpeg_bytes):
        response = client.post(
            "/image/compress/info",
            files={"file": ("a.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")},
            data={"include_file": True},
        )
        assert "file" in response.json()

    def test_requires_authentication(self, client_no_auth, sample_jpeg_bytes):
        response = client_no_auth.post(
            "/image/compress",
            files={"file": ("a.jpg", io.BytesIO(sample_jpeg_bytes), "image/jpeg")},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# /support
# ---------------------------------------------------------------------------

class TestSupportFeedback:
    @pytest.mark.parametrize("tipo", ["suggestion", "bug", "other"])
    def test_accepts_each_valid_type(self, client, tipo):
        with patch("app.routers.supportRoute.send_feedback_email", return_value=True):
            response = client.post(
                "/support/feedback", data={"type": tipo, "message": "minha mensagem"}
            )
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_forwards_optional_email(self, client):
        with patch("app.routers.supportRoute.send_feedback_email") as send:
            client.post(
                "/support/feedback",
                data={"type": "bug", "message": "erro", "email": "eu@example.com"},
            )
        assert send.call_args.kwargs["user_email"] == "eu@example.com"

    def test_invalid_type_returns_400(self, client):
        response = client.post(
            "/support/feedback", data={"type": "reclamacao", "message": "texto"}
        )
        assert response.status_code == 400

    def test_missing_message_returns_422(self, client):
        response = client.post("/support/feedback", data={"type": "bug"})
        assert response.status_code == 422

    def test_email_service_error_propagates_status(self, client):
        with patch("app.routers.supportRoute.send_feedback_email") as send:
            send.side_effect = EmailServiceError("SMTP fora do ar", status_code=503)
            response = client.post(
                "/support/feedback", data={"type": "bug", "message": "texto"}
            )
        assert response.status_code == 503

    def test_unexpected_error_returns_500(self, client):
        with patch("app.routers.supportRoute.send_feedback_email") as send:
            send.side_effect = RuntimeError("inesperado")
            response = client.post(
                "/support/feedback", data={"type": "bug", "message": "texto"}
            )
        assert response.status_code == 500

    def test_requires_authentication(self, client_no_auth):
        response = client_no_auth.post(
            "/support/feedback", data={"type": "bug", "message": "texto"}
        )
        assert response.status_code == 401

    def test_any_authenticated_profile_is_allowed(self, client_airline):
        """Feedback não é exclusivo do file_editor."""
        with patch("app.routers.supportRoute.send_feedback_email", return_value=True):
            response = client_airline.post(
                "/support/feedback", data={"type": "bug", "message": "texto"}
            )
        assert response.status_code == 200

    def test_blocked_while_password_change_pending(self, client_must_change):
        response = client_must_change.post(
            "/support/feedback", data={"type": "bug", "message": "texto"}
        )
        assert response.status_code == 403
