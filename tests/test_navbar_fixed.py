from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')


def test_navbar_est_reellement_fixe():
    assert '--navbar-height: 63px' in CSS
    assert 'position: fixed' in CSS
    assert 'inset: 0 0 auto 0' in CSS
    assert 'z-index: 1200' in CSS


def test_contenu_est_decale_sous_la_navbar():
    assert 'padding-top: var(--navbar-height)' in CSS
    assert 'scroll-padding-top: calc(var(--navbar-height) + 12px)' in CSS


def test_tiroir_mobile_reste_au_dessus_de_la_navbar():
    assert 'z-index: 1250' in CSS  # overlay
    assert 'z-index: 1310' in CSS  # tiroir


def test_impression_supprime_le_decalage_superieur():
    assert 'body { background: #fff; padding-top: 0; }' in CSS
