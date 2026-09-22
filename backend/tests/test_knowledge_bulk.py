from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

import backend.knowledge_db as db


def test_missing_figure_image_recovers_from_ordered_marker_page_assets():
    figures = [
        SimpleNamespace(id="db-fig2", figure_key="fig2", page_number=3, image_id=None),
        SimpleNamespace(id="db-fig3", figure_key="fig3", page_number=3, image_id="repaired-fig3"),
    ]
    images = [
        SimpleNamespace(id="marker-fig3", source="marker_markdown", marker_name="_page_3_Figure_5.jpeg"),
        SimpleNamespace(id="marker-fig2", source="marker_markdown", marker_name="_page_3_Figure_2.jpeg"),
        SimpleNamespace(id="derived", source="figure_index", marker_name="fig3.png"),
    ]

    recovered = db._fallback_figure_image_ids(SimpleNamespace(figures=figures, images=images))

    assert recovered == {"db-fig2": "marker-fig2"}


def test_missing_figure_image_is_not_guessed_when_page_counts_disagree():
    figures = [SimpleNamespace(id="db-fig2", figure_key="fig2", page_number=3, image_id=None)]
    images = [
        SimpleNamespace(id="one", source="marker_markdown", marker_name="_page_3_Figure_2.jpeg"),
        SimpleNamespace(id="two", source="marker_markdown", marker_name="_page_3_Figure_5.jpeg"),
    ]

    assert db._fallback_figure_image_ids(SimpleNamespace(figures=figures, images=images)) == {}


def _seed_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.sqlite3'}", future=True)
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    session = sessions()
    folders = [db.KnowledgeFolder(name=name) for name in ("A", "B", "C")]
    reports = []
    for index in range(3):
        article = db.KnowledgeArticle(
            pdf_sha256=f"sha-{index}",
            source_name=f"paper-{index}.pdf",
            markdown="",
            marker_version="",
            marker_config_hash="",
        )
        reports.append(db.AnalysisReport(article=article, title=f"report-{index}", parts_json="{}"))
    folders[0].report_links.extend(db.KnowledgeFolderReport(report=report) for report in reports)
    session.add_all(folders)
    session.commit()
    session.close()
    return folders, reports, sessions


def test_bulk_move_and_copy_are_idempotent(tmp_path, monkeypatch):
    folders, reports, sessions = _seed_database(tmp_path, monkeypatch)

    result = db.manage_reports_in_folder(folders[0].id, [reports[0].id, reports[1].id], "copy", folders[1].id)
    assert result["affectedCount"] == 2
    db.manage_reports_in_folder(folders[0].id, [reports[0].id], "move", folders[2].id)
    db.manage_reports_in_folder(folders[0].id, [reports[1].id], "copy", folders[1].id)

    session = sessions()
    assert session.scalar(select(db.KnowledgeFolderReport).where(
        db.KnowledgeFolderReport.folder_id == folders[2].id,
        db.KnowledgeFolderReport.report_id == reports[0].id,
    )) is not None
    assert session.scalar(select(db.KnowledgeFolderReport).where(
        db.KnowledgeFolderReport.folder_id == folders[0].id,
        db.KnowledgeFolderReport.report_id == reports[0].id,
    )) is None
    assert len(session.scalars(select(db.KnowledgeFolderReport).where(
        db.KnowledgeFolderReport.folder_id == folders[1].id,
        db.KnowledgeFolderReport.report_id == reports[1].id,
    )).all()) == 1
    session.close()


def test_bulk_delete_removes_report_and_article_everywhere(tmp_path, monkeypatch):
    folders, reports, sessions = _seed_database(tmp_path, monkeypatch)
    db.manage_reports_in_folder(folders[0].id, [reports[0].id], "copy", folders[1].id)

    db.manage_reports_in_folder(folders[0].id, [reports[0].id], "delete")

    session = sessions()
    assert session.get(db.AnalysisReport, reports[0].id) is None
    assert session.scalar(select(db.KnowledgeArticle).where(db.KnowledgeArticle.pdf_sha256 == "sha-0")) is None
    assert session.scalar(select(db.KnowledgeFolderReport).where(
        db.KnowledgeFolderReport.report_id == reports[0].id,
    )) is None
    session.close()


def test_bulk_operation_validates_all_reports_before_mutating(tmp_path, monkeypatch):
    folders, reports, sessions = _seed_database(tmp_path, monkeypatch)

    try:
        db.manage_reports_in_folder(folders[0].id, [reports[0].id, "missing"], "move", folders[1].id)
    except KeyError:
        pass
    else:
        raise AssertionError("expected invalid report membership to fail")

    session = sessions()
    assert session.scalar(select(db.KnowledgeFolderReport).where(
        db.KnowledgeFolderReport.folder_id == folders[0].id,
        db.KnowledgeFolderReport.report_id == reports[0].id,
    )) is not None
    assert session.scalar(select(db.KnowledgeFolderReport).where(
        db.KnowledgeFolderReport.folder_id == folders[1].id,
        db.KnowledgeFolderReport.report_id == reports[0].id,
    )) is None
    session.close()
