from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_ui_loads_nothing_from_the_network():
    for f in [WEB / "index.html", *WEB.glob("css/*.css"), *WEB.glob("js/*.js")]:
        text = f.read_text()
        assert "fonts.googleapis" not in text and "fonts.gstatic" not in text, f.name
        assert "<script src=\"http" not in text and "@import url(http" not in text, f.name


def test_fonts_are_vendored():
    css = (WEB / "css/fonts.css").read_text()
    for fam in ("Inter", "JetBrains Mono"):
        assert f"font-family: '{fam}'" in css
    for url in [l.split("url(")[1].split(")")[0].strip("'\"") for l in css.splitlines() if "url(" in l]:
        assert (WEB / "css" / url).resolve().exists(), url
