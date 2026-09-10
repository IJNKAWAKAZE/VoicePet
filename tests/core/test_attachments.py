from core.attachments import inspect_attachment


def test_inspect_attachment_returns_metadata_without_loading_content(tmp_path):
    path = tmp_path / "large.txt"
    content = b"voicepet" * 100
    path.write_bytes(content)

    attachment = inspect_attachment(path)

    assert attachment.path == str(path.resolve())
    assert attachment.name == "large.txt"
    assert attachment.media_type == "text/plain"
    assert attachment.size == len(content)
    assert len(attachment.sha256) == 64
    assert attachment.sha256 == inspect_attachment(path).sha256


def test_inspect_attachment_accepts_file_url_and_metadata_changes_update_digest(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("one", encoding="utf-8")
    first = inspect_attachment(path)
    second = inspect_attachment(path.as_uri())
    assert second.path == first.path
    assert second.sha256 == first.sha256
    path.write_text("two-updated", encoding="utf-8")
    assert inspect_attachment(path).sha256 != first.sha256
