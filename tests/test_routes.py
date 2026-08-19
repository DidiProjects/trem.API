import io
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.dependencies import get_current_user
from app.infrastructure.database.connection import get_db
from tests.conftest import make_mock_user, mock_get_db


# ---------------------------------------------------------------------------
# PDF Routes
# ---------------------------------------------------------------------------

class TestPdfRoutesSplit:
    def test_split_pdf_success(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/split",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            data={"pages": "1"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"

    def test_split_pdf_invalid_file(self, client):
        response = client.post(
            "/pdf/split",
            files={"file": ("test.txt", io.BytesIO(b"not a pdf"), "text/plain")},
            data={"pages": "1"},
        )
        assert response.status_code == 400
        assert "pdf" in response.json()["detail"].lower()


class TestPdfRoutesExtractPages:
    def test_extract_pages_success(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/extract-pages",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"


class TestPdfRoutesMerge:
    def test_merge_pdfs_success(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/merge",
            files=[
                ("files", ("doc1.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")),
                ("files", ("doc2.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")),
            ],
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"

    def test_merge_single_file_fails(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/merge",
            files=[("files", ("doc1.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf"))],
        )
        assert response.status_code == 400
        assert "at least 2" in response.json()["detail"]


class TestPdfRoutesPassword:
    def test_add_password_success(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/add-password",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            data={"user_password": "senha123"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"

    def test_remove_password_success(self, client, protected_pdf_bytes):
        response = client.post(
            "/pdf/remove-password",
            files={"file": ("protected.pdf", io.BytesIO(protected_pdf_bytes), "application/pdf")},
            data={"password": "user123"},
        )
        assert response.status_code == 200

    def test_remove_password_wrong_password(self, client, protected_pdf_bytes):
        response = client.post(
            "/pdf/remove-password",
            files={"file": ("protected.pdf", io.BytesIO(protected_pdf_bytes), "application/pdf")},
            data={"password": "wrong"},
        )
        assert response.status_code == 400


class TestPdfRoutesInfo:
    def test_info_success(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/info",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test.pdf"
        assert data["pages"] == 3
        assert "metadata" in data


class TestPdfRoutesConvertToImage:
    def test_convert_to_png(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/convert-to-image",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            data={"format": "png", "dpi": "150", "pages": "1"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_convert_to_jpeg(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/convert-to-image",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            data={"format": "jpeg", "dpi": "150", "pages": "1"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_convert_multiple_pages_returns_zip(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/convert-to-image",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            data={"format": "png", "dpi": "150"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"

    def test_convert_invalid_dpi(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/convert-to-image",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            data={"format": "png", "dpi": "1000"},
        )
        assert response.status_code == 400
        assert "DPI" in response.json()["detail"]


class TestPdfRoutesConvertToOfx:
    def test_convert_to_ofx_no_transactions(self, client, sample_pdf_bytes):
        response = client.post(
            "/pdf/convert-to-ofx",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            data={"bank_id": "032", "account_id": "123456", "account_type": "CHECKING"},
        )
        assert response.status_code == 400
        assert "extract transactions" in response.json()["detail"]


class TestPdfRoutesExtractText:
    def test_extract_text_success(self, client, sample_pdf_with_text):
        response = client.post(
            "/pdf/extract-text",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_with_text), "application/pdf")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test.pdf"
        assert data["total_pages"] == 1
        assert "pages" in data


# ---------------------------------------------------------------------------
# Auth: Bearer JWT
# ---------------------------------------------------------------------------

class TestPdfRoutesAuth:
    def test_no_token_returns_401(self, client_no_auth, sample_pdf_bytes):
        response = client_no_auth.post(
            "/pdf/info",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
        )
        assert response.status_code == 401

    def test_invalid_bearer_returns_401(self, client_no_auth, sample_pdf_bytes):
        response = client_no_auth.post(
            "/pdf/info",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
            headers={"Authorization": "Bearer token.invalido.aqui"},
        )
        assert response.status_code == 401

    def test_must_change_password_blocked_returns_403(self, client_must_change, sample_pdf_bytes):
        """Usuário com must_change_password=True não pode acessar endpoints de arquivo."""
        response = client_must_change.post(
            "/pdf/info",
            files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
        )
        assert response.status_code == 403

    def test_wrong_profile_returns_403(self, sample_pdf_bytes):
        """Usuário com perfil airline_company não pode acessar endpoints de arquivo."""
        async def _airline_user():
            return make_mock_user("airline_company")

        app.dependency_overrides[get_current_user] = _airline_user
        app.dependency_overrides[get_db] = mock_get_db
        try:
            with TestClient(app) as c:
                response = c.post(
                    "/pdf/info",
                    files={"file": ("test.pdf", io.BytesIO(sample_pdf_bytes), "application/pdf")},
                )
            assert response.status_code == 403
        finally:
            app.dependency_overrides.clear()
