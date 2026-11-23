"""Render packaged templates. Callers escape text and form values before insertion."""

from functools import lru_cache
from pathlib import Path
from string import Template

from .text import safe

HERE = Path(__file__).parent
PAGES = (
    ('scan', '审片', '/desk'),
    ('intake', '母带与规则', '/desk/intake'),
    ('compare', '版次比对', '/desk/compare'),
    ('reinjects', '回灌预检', '/desk/reinjects'),
    ('seals', '冻结包', '/desk/seals'),
)
ASSETS = {'shell.css', 'review.css', 'media.css', 'intake.js'}


@lru_cache
def read_template(name):
    return Template((HERE / 'views' / f'{name}.html').read_text(encoding='utf-8'))


def fragment(view, **data):
    return read_template(view).substitute(data)


def page(title, active, view, **data):
    nav = ''.join(
        f'<a href="{link}"{" aria-current=page" if key == active else ""}>{label}</a>'
        for key, label, link in PAGES
    )
    return fragment('shell', title=safe(title), nav=nav, main=fragment(view, **data)).encode()
