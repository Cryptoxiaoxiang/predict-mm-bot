from pathlib import Path


def test_dashboard_links_to_owner_x_profile() -> None:
    html = (
        Path(__file__).parents[1] / "predict_mm" / "web_static" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'href="https://x.com/cryptoxiaoxiang"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html
