"""Combine src/template.html, the Leaflet styles and the data file into
index.html (the page GitHub Pages serves)."""
from config import BUILD, ROOT, SRC


def main():
    t = (SRC / 'template.html').read_text()
    css = (SRC / 'leaflet.css').read_text()
    data = (BUILD / 'bikenet_data.json').read_text().replace('</', '<\\/')
    assert '/*__LEAFLETCSS__*/' in t and '__DATA__' in t, 'template placeholders missing'
    html = t.replace('/*__LEAFLETCSS__*/', css).replace('__DATA__', data)
    (ROOT / 'index.html').write_text(html)
    print('wrote index.html', len(html), 'bytes')


if __name__ == '__main__':
    main()
