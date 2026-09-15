from pathlib import Path

from PIL import ImageFont

from app.utils import utils


def test_resolve_subtitle_font_falls_back_to_system_cjk_font():
    path = utils.resolve_subtitle_font("STHeitiMedium.ttc")

    assert Path(path).is_file()
    ImageFont.truetype(path, 30)


def test_list_subtitle_font_names_includes_default_alias():
    names = utils.list_subtitle_font_names()

    assert names
    assert "STHeitiMedium.ttc" in names
