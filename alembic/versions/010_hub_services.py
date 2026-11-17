"""catálogo de serviços do trem.FRONT

  - hub_services: um serviço do servidor (nome, descrição, endereço, logo, ordem), ligado à
    permissão hub:<slug> que diz quem o vê
  - cria a aplicação hub se ainda não existe, e um serviço para cada permissão hub:* que já
    existia (as concessões continuam valendo)
  - os cinco serviços de hoje vêm com nome, descrição e logo; o endereço fica vazio até o
    admin preencher, e até lá o catálogo não os mostra

Revision ID: 010
Revises: 009
Create Date: 2026-11-17 00:00:00.000000

"""
import hashlib
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOGOS = Path(__file__).resolve().parent.parent / "seed" / "hub"

SERVICES = [
    ("immich", "Immich", "Fotos e vídeos: backup do celular, álbuns, busca por pessoas e lugares."),
    ("radarr", "Radarr", "Filmes: acompanha lançamentos e busca automaticamente na qualidade escolhida."),
    ("sonarr", "Sonarr", "Séries: acompanha temporadas e baixa episódios novos assim que saem."),
    ("prowlarr", "Prowlarr", "Indexadores: gerencia as fontes de busca usadas pelo Radarr e pelo Sonarr."),
    ("qbittorrent", "qBittorrent", "Downloads: fila, velocidade e andamento dos torrents."),
]


def upgrade() -> None:
    op.create_table(
        "hub_services",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("permission_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("position", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("logo", sa.LargeBinary, nullable=True),
        sa.Column("logo_type", sa.String(50), nullable=True),
        sa.Column("logo_version", sa.String(16), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.execute("""
        INSERT INTO applications (slug, name, description)
        VALUES ('hub', 'trem.FRONT', 'Catálogo de serviços: hub:<serviço> mostra o serviço à conta')
        ON CONFLICT (slug) DO NOTHING
    """)
    bind = op.get_bind()
    for slug, name, _ in SERVICES:
        bind.execute(sa.text("""
            INSERT INTO permissions (name, description, application_id)
            SELECT :slug, :name, id FROM applications WHERE slug = 'hub'
            ON CONFLICT ON CONSTRAINT uq_permissions_application_id_name DO NOTHING
        """), {"slug": slug, "name": name})

    # Every hub permission becomes a service, the known ones with their details.
    for position, (slug, name, description) in enumerate(SERVICES):
        logo = (LOGOS / f"{slug}.svg").read_bytes()
        bind.execute(sa.text("""
            INSERT INTO hub_services (permission_id, name, description, position, logo, logo_type, logo_version)
            SELECT p.id, :name, :description, :position, :logo, 'image/svg+xml', :version
            FROM permissions p JOIN applications a ON a.id = p.application_id
            WHERE a.slug = 'hub' AND p.name = :slug
        """), {"slug": slug, "name": name, "description": description, "position": position,
               "logo": logo, "version": hashlib.sha256(logo).hexdigest()[:16]})
    bind.execute(sa.text("""
        INSERT INTO hub_services (permission_id, name, description, position)
        SELECT p.id, p.name, p.description, :position + row_number() OVER (ORDER BY p.name)
        FROM permissions p JOIN applications a ON a.id = p.application_id
        WHERE a.slug = 'hub' AND NOT EXISTS (SELECT 1 FROM hub_services s WHERE s.permission_id = p.id)
    """), {"position": len(SERVICES) - 1})


def downgrade() -> None:
    """Keeps the hub application, its permissions and grants: they predate the catalog table."""
    op.drop_table("hub_services")
