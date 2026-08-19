"""
Testes das funções de processamento de videoService/audioService.

moviepy e Whisper são mockados: o objetivo é fixar o contrato dessas funções
(inclusive a API do moviepy 2.x — `subclipped`, não mais `subclip`) e garantir
que os recursos são sempre fechados, mesmo em erro.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.audioService import (
    AudioServiceError,
    cut_audio,
    get_whisper_model,
    transcribe,
)
from app.services.videoService import VideoServiceError, cut_video


def _clip(duration=60.0, has_audio=True):
    clip = MagicMock()
    clip.duration = duration
    clip.audio = MagicMock() if has_audio else None
    clip.subclipped.return_value = MagicMock()
    return clip


class TestCutVideo:
    def test_returns_output_path(self, tmp_path):
        out = str(tmp_path / "saida.mp4")
        with patch("app.services.videoService.VideoFileClip", return_value=_clip()):
            assert cut_video("entrada.mp4", 0, 10, out) == out

    def test_uses_moviepy_2_subclipped_api(self, tmp_path):
        """moviepy 2.x renomeou subclip -> subclipped; usar o antigo quebra em runtime."""
        clip = _clip()
        with patch("app.services.videoService.VideoFileClip", return_value=clip):
            cut_video("entrada.mp4", 5, 15, str(tmp_path / "s.mp4"))
        clip.subclipped.assert_called_once_with(5, 15)

    def test_writes_with_h264_and_aac(self, tmp_path):
        clip = _clip()
        with patch("app.services.videoService.VideoFileClip", return_value=clip):
            cut_video("entrada.mp4", 0, 10, str(tmp_path / "s.mp4"))

        kwargs = clip.subclipped.return_value.write_videofile.call_args.kwargs
        assert kwargs["codec"] == "libx264"
        assert kwargs["audio_codec"] == "aac"

    def test_clamps_end_to_clip_duration(self, tmp_path):
        clip = _clip(duration=30.0)
        with patch("app.services.videoService.VideoFileClip", return_value=clip):
            cut_video("entrada.mp4", 0, 999, str(tmp_path / "s.mp4"))
        clip.subclipped.assert_called_once_with(0, 30.0)

    def test_start_beyond_duration_raises(self, tmp_path):
        with patch("app.services.videoService.VideoFileClip", return_value=_clip(30.0)):
            with pytest.raises(VideoServiceError) as exc:
                cut_video("entrada.mp4", 45, 50, str(tmp_path / "s.mp4"))
        assert "excede a duração" in exc.value.message

    def test_processing_failure_becomes_500(self, tmp_path):
        clip = _clip()
        clip.subclipped.return_value.write_videofile.side_effect = OSError("ffmpeg sumiu")
        with patch("app.services.videoService.VideoFileClip", return_value=clip):
            with pytest.raises(VideoServiceError) as exc:
                cut_video("entrada.mp4", 0, 10, str(tmp_path / "s.mp4"))
        assert exc.value.status_code == 500

    def test_closes_clips_on_success(self, tmp_path):
        clip = _clip()
        with patch("app.services.videoService.VideoFileClip", return_value=clip):
            cut_video("entrada.mp4", 0, 10, str(tmp_path / "s.mp4"))
        clip.close.assert_called_once()
        clip.subclipped.return_value.close.assert_called_once()

    def test_closes_clip_on_failure(self, tmp_path):
        """Vazar handle de arquivo trava o /tmp do container."""
        clip = _clip()
        clip.subclipped.return_value.write_videofile.side_effect = OSError("boom")
        with patch("app.services.videoService.VideoFileClip", return_value=clip):
            with pytest.raises(VideoServiceError):
                cut_video("entrada.mp4", 0, 10, str(tmp_path / "s.mp4"))
        clip.close.assert_called_once()


class TestCutAudio:
    def test_returns_output_path(self, tmp_path):
        out = str(tmp_path / "saida.mp3")
        with patch("app.services.audioService.AudioFileClip", return_value=_clip()):
            assert cut_audio("entrada.mp3", 0, 10, out) == out

    def test_uses_moviepy_2_subclipped_api(self, tmp_path):
        clip = _clip()
        with patch("app.services.audioService.AudioFileClip", return_value=clip):
            cut_audio("entrada.mp3", 2, 8, str(tmp_path / "s.mp3"))
        clip.subclipped.assert_called_once_with(2, 8)

    def test_clamps_end_to_clip_duration(self, tmp_path):
        clip = _clip(duration=20.0)
        with patch("app.services.audioService.AudioFileClip", return_value=clip):
            cut_audio("entrada.mp3", 0, 999, str(tmp_path / "s.mp3"))
        clip.subclipped.assert_called_once_with(0, 20.0)

    def test_start_beyond_duration_raises(self, tmp_path):
        with patch("app.services.audioService.AudioFileClip", return_value=_clip(20.0)):
            with pytest.raises(AudioServiceError):
                cut_audio("entrada.mp3", 30, 40, str(tmp_path / "s.mp3"))

    def test_processing_failure_becomes_500(self, tmp_path):
        clip = _clip()
        clip.subclipped.return_value.write_audiofile.side_effect = OSError("boom")
        with patch("app.services.audioService.AudioFileClip", return_value=clip):
            with pytest.raises(AudioServiceError) as exc:
                cut_audio("entrada.mp3", 0, 10, str(tmp_path / "s.mp3"))
        assert exc.value.status_code == 500

    def test_closes_clips(self, tmp_path):
        clip = _clip()
        with patch("app.services.audioService.AudioFileClip", return_value=clip):
            cut_audio("entrada.mp3", 0, 10, str(tmp_path / "s.mp3"))
        clip.close.assert_called_once()


def _whisper_result(text="olá mundo"):
    return {
        "text": f"  {text}  ",
        "segments": [
            {"start": 0.001, "end": 1.4999, "text": " olá "},
            {"start": 1.5, "end": 3.0, "text": " mundo "},
        ],
        "language": "pt",
    }


class TestTranscribe:
    @pytest.fixture
    def model(self):
        """
        Mocka o Whisper e também o AudioFileClip: sem isso, o fallback de
        duração abriria um ffmpeg de verdade em cima do arquivo falso.
        """
        mock = MagicMock()
        mock.transcribe.return_value = _whisper_result()
        with patch(
            "app.services.audioService.get_whisper_model", return_value=mock
        ), patch(
            "app.services.audioService.AudioFileClip",
            side_effect=OSError("formato não suportado"),
        ):
            yield mock

    def test_returns_stripped_text(self, model, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"fake")
        assert transcribe(str(audio))["text"] == "olá mundo"

    def test_rounds_and_strips_segments(self, model, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"fake")

        segments = transcribe(str(audio))["segments"]
        assert segments[0] == {"start": 0.0, "end": 1.5, "text": "olá"}
        assert segments[1]["text"] == "mundo"

    def test_reports_detected_language(self, model, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"fake")
        assert transcribe(str(audio))["language"] == "pt"

    def test_forwards_language_option(self, model, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"fake")
        transcribe(str(audio), language="en")
        assert model.transcribe.call_args.kwargs == {"language": "en"}

    def test_omits_language_option_when_not_given(self, model, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"fake")
        transcribe(str(audio))
        assert model.transcribe.call_args.kwargs == {}

    def test_video_input_extracts_audio_track(self, model, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"fake")
        clip = _clip(duration=12.0)

        with patch("app.services.audioService.VideoFileClip", return_value=clip):
            result = transcribe(str(video))

        clip.audio.write_audiofile.assert_called_once()
        assert result["duration"] == 12.0

    def test_video_without_audio_track_raises(self, model, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"fake")

        with patch(
            "app.services.audioService.VideoFileClip",
            return_value=_clip(has_audio=False),
        ):
            with pytest.raises(AudioServiceError) as exc:
                transcribe(str(video))
        assert "trilha de áudio" in exc.value.message

    def test_video_clip_is_closed(self, model, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"fake")
        clip = _clip()

        with patch("app.services.audioService.VideoFileClip", return_value=clip):
            transcribe(str(video))
        clip.close.assert_called_once()

    def test_whisper_failure_becomes_500(self, model, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"fake")
        model.transcribe.side_effect = RuntimeError("modelo não carregou")

        with pytest.raises(AudioServiceError) as exc:
            transcribe(str(audio))
        assert exc.value.status_code == 500

    def test_duration_is_none_when_undetectable(self, model, tmp_path):
        """MP3 não abre com o módulo wave — a duração pode simplesmente faltar."""
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"nao-e-audio-valido")
        assert transcribe(str(audio))["duration"] is None

    def test_duration_read_from_wav_header(self, model, tmp_path):
        """Para WAV a duração sai do header, sem precisar de ffmpeg."""
        import wave

        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 16000)  # 2 segundos

        assert transcribe(str(audio))["duration"] == 2.0


class TestGetWhisperModel:
    def test_imports_whisper_lazily_and_caches(self):
        """
        O import de whisper arrasta o torch; ele não pode acontecer no boot.
        A segunda chamada precisa reaproveitar o modelo já carregado.
        """
        import app.services.audioService as audio_service

        original = audio_service._whisper_model
        audio_service._whisper_model = None
        fake_whisper = MagicMock()
        fake_whisper.load_model.return_value = "modelo-carregado"

        try:
            with patch.dict("sys.modules", {"whisper": fake_whisper}):
                assert get_whisper_model() == "modelo-carregado"
                assert get_whisper_model() == "modelo-carregado"
            fake_whisper.load_model.assert_called_once_with("base")
        finally:
            audio_service._whisper_model = original
