"""SQLite persistence for explicitly archived knowledge-base content."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from runtime_config import BASE_DIR, RUNTIME_CONFIG
from temp_assets import document_dir, read_json


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _database_url() -> str:
    raw = str(RUNTIME_CONFIG.storage.get("database_url") or "sqlite:///data/marker_web.sqlite3")
    if raw.startswith("sqlite:///") and not raw.startswith("sqlite:////"):
        relative = raw[len("sqlite:///") :]
        return "sqlite:///" + str((BASE_DIR / relative).resolve()).replace("\\", "/")
    return raw


class Base(DeclarativeBase):
    pass


class KnowledgeArticle(Base):
    __tablename__ = "knowledge_articles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    pdf_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_name: Mapped[str] = mapped_column(String(512))
    markdown: Mapped[str] = mapped_column(Text)
    marker_version: Mapped[str] = mapped_column(String(32))
    marker_config_hash: Mapped[str] = mapped_column(String(64))
    marker_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    images: Mapped[list["KnowledgeImage"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    figures: Mapped[list["KnowledgeFigure"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    report: Mapped["AnalysisReport | None"] = relationship(
        back_populates="article", cascade="all, delete-orphan", uselist=False
    )


class KnowledgeImage(Base):
    __tablename__ = "knowledge_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    article_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_articles.id", ondelete="CASCADE"), index=True
    )
    marker_name: Mapped[str] = mapped_column(String(512))
    temp_asset_id: Mapped[str] = mapped_column(String(36), default="")
    mime_type: Mapped[str] = mapped_column(String(128))
    extension: Mapped[str] = mapped_column(String(16))
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    source: Mapped[str] = mapped_column(String(64), default="marker")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    article: Mapped[KnowledgeArticle] = relationship(back_populates="images")
    figures: Mapped[list["KnowledgeFigure"]] = relationship(back_populates="image")


class KnowledgeFigure(Base):
    __tablename__ = "knowledge_figures"
    __table_args__ = (UniqueConstraint("article_id", "figure_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    article_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_articles.id", ondelete="CASCADE"), index=True
    )
    figure_key: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(256), default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_json: Mapped[str] = mapped_column(Text, default="null")
    position: Mapped[int] = mapped_column(Integer, default=0)
    marker_block_id: Mapped[str] = mapped_column(String(512), default="")
    node_type: Mapped[str] = mapped_column(String(64), default="Figure")
    image_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_images.id", ondelete="SET NULL"), nullable=True
    )

    article: Mapped[KnowledgeArticle] = relationship(back_populates="figures")
    image: Mapped[KnowledgeImage | None] = relationship(back_populates="figures")


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    article_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_articles.id", ondelete="CASCADE"), unique=True, index=True
    )
    source_report_id: Mapped[str] = mapped_column(String(256), default="")
    title: Mapped[str] = mapped_column(String(512))
    parts_json: Mapped[str] = mapped_column(Text)
    overall_model_label: Mapped[str] = mapped_column(String(256), default="")
    total_elapsed: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    article: Mapped[KnowledgeArticle] = relationship(back_populates="report")
    folder_links: Mapped[list["KnowledgeFolderReport"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class KnowledgeFolder(Base):
    __tablename__ = "knowledge_folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    report_links: Mapped[list["KnowledgeFolderReport"]] = relationship(
        back_populates="folder", cascade="all, delete-orphan"
    )


class KnowledgeFolderReport(Base):
    __tablename__ = "knowledge_folder_reports"

    folder_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_folders.id", ondelete="CASCADE"), primary_key=True
    )
    report_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_reports.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    folder: Mapped[KnowledgeFolder] = relationship(back_populates="report_links")
    report: Mapped[AnalysisReport] = relationship(back_populates="folder_links")


ENGINE = create_engine(
    _database_url(),
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    if not _database_url().startswith("sqlite:"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False, future=True)


class ArchiveConflict(RuntimeError):
    def __init__(self, report_id: str):
        super().__init__("该文章已存在分析报告")
        self.report_id = report_id


def init_database() -> None:
    db_path = Path(_database_url().replace("sqlite:///", "")) if _database_url().startswith("sqlite:///") else None
    if db_path:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    from sqlalchemy import inspect
    from alembic import command
    from alembic.config import Config

    alembic_config = Config(str(BASE_DIR / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", _database_url())
    inspector = inspect(ENGINE)
    existing_schema = inspector.has_table("knowledge_articles")
    versioned = inspector.has_table("alembic_version")
    if existing_schema and not versioned:
        # Upgrade an early local database created before Alembic was introduced.
        command.stamp(alembic_config, "head")
    else:
        command.upgrade(alembic_config, "head")


def _part_payload(report: dict[str, Any], part: str) -> dict[str, Any]:
    state = report.get("parts", {}).get(part) if isinstance(report.get("parts"), dict) else None
    if isinstance(state, dict):
        return {
            "content": str(state.get("content") or report.get(part) or ""),
            "status": str(state.get("status") or ""),
            "model": str(state.get("model") or report.get("model") or ""),
            "model_label": str(state.get("model_label") or report.get("model_label") or ""),
            "elapsed": int(state.get("elapsed") or 0),
            "generated_at": str(state.get("generated_at") or report.get("generated_at") or ""),
            "run_id": str(state.get("run_id") or report.get("run_id") or ""),
            "error": str(state.get("error") or ""),
        }
    return {
        "content": str(report.get(part) or ""),
        "status": "completed" if str(report.get(part) or "").strip() else "not_selected",
        "model": str(report.get("model") or ""),
        "model_label": str(report.get("model_label") or ""),
        "elapsed": int(report.get("elapsed") or 0),
        "generated_at": str(report.get("generated_at") or ""),
        "run_id": str(report.get("run_id") or ""),
        "error": "",
    }


def _merge_parts(old_parts: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old_parts or {})
    for part in ("part1", "part2", "part3"):
        incoming = _part_payload(report, part)
        # Failed/cancelled/not-selected sections must not erase a previous success.
        if incoming["status"] == "completed":
            merged[part] = incoming
        elif part not in merged:
            merged[part] = incoming
    return merged


def _replace_article_assets(session, article: KnowledgeArticle, doc_dir: Path, manifest: dict[str, Any]) -> dict[str, KnowledgeImage]:
    article.images.clear()
    article.figures.clear()
    session.flush()
    image_by_temp_id: dict[str, KnowledgeImage] = {}
    image_by_relative_path: dict[str, KnowledgeImage] = {}
    for asset in manifest.get("assets", []):
        relative = str(asset.get("relative_path") or "")
        path = (doc_dir / relative).resolve()
        if doc_dir.resolve() not in path.parents or not path.exists():
            raise FileNotFoundError(f"归档图片不存在：{relative}")
        content = path.read_bytes()
        image = KnowledgeImage(
            marker_name=str(asset.get("marker_name") or path.name),
            temp_asset_id=str(asset.get("asset_id") or ""),
            mime_type=str(asset.get("mime_type") or "application/octet-stream"),
            extension=str(asset.get("extension") or path.suffix.lstrip(".")),
            width=int(asset.get("width") or 0),
            height=int(asset.get("height") or 0),
            byte_size=len(content),
            sha256=str(asset.get("sha256") or ""),
            content=content,
            source=str(asset.get("source") or "marker"),
        )
        article.images.append(image)
        if asset.get("asset_id"):
            image_by_temp_id[str(asset["asset_id"])] = image
        if asset.get("relative_path"):
            image_by_relative_path[str(asset["relative_path"]).replace("\\", "/")] = image
    session.flush()
    figure_path = doc_dir / "figure_index.json"
    figure_index = read_json(figure_path) if figure_path.exists() else manifest.get("figure_index", [])
    for item in figure_index or []:
        temp_id = str(item.get("temp_asset_id") or "")
        article.figures.append(KnowledgeFigure(
            figure_key=str(item.get("id") or item.get("figure_key") or uuid.uuid4().hex),
            label=str(item.get("label") or ""),
            caption=str(item.get("caption") or ""),
            page_number=int(item["page"]) if item.get("page") is not None else None,
            bbox_json=json.dumps(item.get("bbox"), ensure_ascii=False),
            position=int(item.get("position") or 0),
            marker_block_id=str(item.get("marker_block_id") or ""),
            node_type=str(item.get("node_type") or "Figure"),
            image=image_by_temp_id.get(temp_id),
        ))
    return image_by_relative_path


def archive_document(
    document_id: str,
    folder_ids: list[str],
    report: dict[str, Any],
    *,
    update_existing: bool = False,
) -> dict[str, Any]:
    """Archive one temporary document and one three-part report atomically."""
    doc_dir = document_dir(document_id)
    manifest = read_json(doc_dir / "manifest.json")
    markdown = (doc_dir / "document.md").read_text(encoding="utf-8")
    marker_meta_path = doc_dir / "marker_meta.json"
    marker_meta = read_json(marker_meta_path) if marker_meta_path.exists() else {}
    if "data:image/" in markdown.lower():
        raise ValueError("Markdown 中仍含 Base64 图片，拒绝归档")
    structure_path = doc_dir / "structure.json"
    if structure_path.exists() and "data:image/" in structure_path.read_text(encoding="utf-8").lower():
        raise ValueError("structure.json 中仍含 Base64 图片，拒绝归档")

    with SessionLocal.begin() as session:
        folders = list(session.scalars(select(KnowledgeFolder).where(KnowledgeFolder.id.in_(folder_ids))))
        if len(folders) != len(set(folder_ids)):
            raise ValueError("包含不存在的知识库文件夹")
        article = session.scalar(
            select(KnowledgeArticle).where(KnowledgeArticle.pdf_sha256 == manifest["pdf_sha256"])
        )
        if article is None:
            article = KnowledgeArticle(
                pdf_sha256=manifest["pdf_sha256"],
                source_name=manifest["source_name"],
                markdown=markdown,
                marker_version=manifest.get("marker_version", ""),
                marker_config_hash=manifest.get("marker_config_hash", ""),
                marker_metadata_json=json.dumps(marker_meta, ensure_ascii=False),
            )
            session.add(article)
            session.flush()
        elif article.report is not None and not update_existing:
            raise ArchiveConflict(article.report.id)

        article.source_name = manifest["source_name"]
        article.markdown = markdown
        article.marker_version = manifest.get("marker_version", "")
        article.marker_config_hash = manifest.get("marker_config_hash", "")
        article.marker_metadata_json = json.dumps(marker_meta, ensure_ascii=False)
        article.updated_at = _utcnow()
        archived_images = _replace_article_assets(session, article, doc_dir, manifest)
        archived_markdown = markdown
        for relative_path, image in archived_images.items():
            archived_markdown = archived_markdown.replace(
                relative_path,
                f"/api/knowledge/assets/{image.id}",
            )
        article.markdown = archived_markdown

        if article.report is None:
            parts = {part: _part_payload(report, part) for part in ("part1", "part2", "part3")}
            analysis = AnalysisReport(
                article=article,
                source_report_id=str(report.get("source_report_id") or report.get("sourceReportId") or ""),
                title=str(report.get("name") or report.get("title") or article.source_name),
                parts_json=json.dumps(parts, ensure_ascii=False),
                overall_model_label=str(report.get("model_label") or report.get("modelLabel") or ""),
                total_elapsed=int(report.get("elapsed") or 0),
                generated_at=str(report.get("generated_at") or report.get("generatedAt") or ""),
            )
            session.add(analysis)
            session.flush()
        else:
            analysis = article.report
            old_parts = json.loads(analysis.parts_json or "{}")
            analysis.parts_json = json.dumps(_merge_parts(old_parts, report), ensure_ascii=False)
            analysis.title = str(report.get("name") or report.get("title") or analysis.title)
            analysis.overall_model_label = str(
                report.get("model_label") or report.get("modelLabel") or analysis.overall_model_label
            )
            analysis.total_elapsed = int(report.get("elapsed") or analysis.total_elapsed)
            analysis.generated_at = str(
                report.get("generated_at") or report.get("generatedAt") or analysis.generated_at
            )
            analysis.updated_at = _utcnow()

        analysis.folder_links.clear()
        session.flush()
        for folder in folders:
            analysis.folder_links.append(KnowledgeFolderReport(folder=folder))
        session.flush()
        report_id = analysis.id
    return get_report(report_id)


def _report_dict(report: AnalysisReport) -> dict[str, Any]:
    parts = json.loads(report.parts_json or "{}")
    folder_ids = [link.folder_id for link in report.folder_links]
    figure_index = [{
        "id": figure.figure_key,
        "label": figure.label,
        "caption": figure.caption,
        "page": figure.page_number,
        "bbox": json.loads(figure.bbox_json or "null"),
        "position": figure.position,
        "marker_block_id": figure.marker_block_id,
        "node_type": figure.node_type,
        "image_id": figure.image_id,
        "image_url": f"/api/knowledge/assets/{figure.image_id}" if figure.image_id else "",
    } for figure in report.article.figures]
    return {
        "id": report.id,
        "sourceReportId": report.source_report_id,
        "documentId": report.article.id,
        "name": report.title,
        "modelLabel": report.overall_model_label,
        "generatedAt": report.generated_at,
        "elapsed": report.total_elapsed,
        "part1": (parts.get("part1") or {}).get("content", ""),
        "part2": (parts.get("part2") or {}).get("content", ""),
        "part3": (parts.get("part3") or {}).get("content", ""),
        "parts": parts,
        "figureIndex": figure_index,
        "folderIds": folder_ids,
        "createdAt": int(report.created_at.timestamp() * 1000),
        "updatedAt": int(report.updated_at.timestamp() * 1000),
    }


def get_report(report_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        report = session.get(AnalysisReport, report_id)
        if report is None:
            raise KeyError("报告不存在")
        # Relationships are loaded while the session is alive.
        return _report_dict(report)


def knowledge_snapshot() -> dict[str, Any]:
    with SessionLocal() as session:
        folders = list(session.scalars(select(KnowledgeFolder).order_by(KnowledgeFolder.created_at)))
        reports = list(session.scalars(select(AnalysisReport).order_by(AnalysisReport.updated_at.desc())))
        return {
            "folders": [{
                "id": folder.id,
                "name": folder.name,
                "createdAt": int(folder.created_at.timestamp() * 1000),
                "updatedAt": int(folder.updated_at.timestamp() * 1000),
            } for folder in folders],
            "reports": [_report_dict(report) for report in reports],
            "links": [{
                "folderId": link.folder_id,
                "reportId": link.report_id,
                "createdAt": int(link.created_at.timestamp() * 1000),
            } for report in reports for link in report.folder_links],
        }


def create_folder(name: str) -> dict[str, Any]:
    normalized = " ".join(str(name or "").split())
    if not normalized:
        raise ValueError("文件夹名称不能为空")
    with SessionLocal.begin() as session:
        exists = session.scalar(select(KnowledgeFolder).where(func.lower(KnowledgeFolder.name) == normalized.lower()))
        if exists:
            raise ValueError("已存在同名归档文件夹")
        folder = KnowledgeFolder(name=normalized)
        session.add(folder)
        session.flush()
        return {"id": folder.id, "name": folder.name}


def update_folder(folder_id: str, name: str) -> dict[str, Any]:
    normalized = " ".join(str(name or "").split())
    if not normalized:
        raise ValueError("文件夹名称不能为空")
    with SessionLocal.begin() as session:
        folder = session.get(KnowledgeFolder, folder_id)
        if folder is None:
            raise KeyError("文件夹不存在")
        duplicate = session.scalar(
            select(KnowledgeFolder).where(
                func.lower(KnowledgeFolder.name) == normalized.lower(),
                KnowledgeFolder.id != folder_id,
            )
        )
        if duplicate:
            raise ValueError("已存在同名归档文件夹")
        folder.name = normalized
        folder.updated_at = _utcnow()
        return {"id": folder.id, "name": folder.name}


def _delete_orphan_report(session, report: AnalysisReport) -> None:
    if report.folder_links:
        return
    article = report.article
    session.delete(report)
    session.flush()
    # One article has at most one report; once that report has no folder
    # membership, the archived article snapshot and all assets are orphaned.
    session.delete(article)


def delete_folder(folder_id: str) -> None:
    with SessionLocal.begin() as session:
        folder = session.get(KnowledgeFolder, folder_id)
        if folder is None:
            raise KeyError("文件夹不存在")
        reports = [link.report for link in list(folder.report_links)]
        session.delete(folder)
        session.flush()
        for report in reports:
            _delete_orphan_report(session, report)


def remove_report_from_folder(folder_id: str, report_id: str) -> None:
    with SessionLocal.begin() as session:
        link = session.get(KnowledgeFolderReport, {"folder_id": folder_id, "report_id": report_id})
        if link is None:
            raise KeyError("归档关系不存在")
        report = link.report
        session.delete(link)
        session.flush()
        _delete_orphan_report(session, report)


def knowledge_asset(asset_id: str) -> tuple[bytes, str, str]:
    with SessionLocal() as session:
        image = session.get(KnowledgeImage, asset_id)
        if image is None:
            raise KeyError("图片不存在")
        return image.content, image.mime_type, image.extension
