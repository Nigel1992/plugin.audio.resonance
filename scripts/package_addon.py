"""Build a reproducible Kodi install ZIP using only distributable add-on files."""
import argparse
import hashlib
from pathlib import Path
import stat
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ('addon.xml', 'plugin.py', 'service.py', 'LICENSE.txt', 'README.md', 'CHANGELOG.txt')


def build(output):
    manifest = ET.parse(ROOT / 'addon.xml').getroot()
    identity, version = manifest.attrib['id'], manifest.attrib['version']
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'{identity}-{version}.zip'
    files = [ROOT / name for name in ROOT_FILES]
    files += sorted(p for p in (ROOT / 'resources').rglob('*') if p.is_file()
                    and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo'))
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f'{identity}/{relative}', (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if relative.startswith('resources/lib/deps/spotty/') and path.name.startswith('spotty') else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP integrity check failed')
        assert f'{identity}/LICENSE.txt' in archive.namelist()
        assert ET.fromstring(archive.read(f'{identity}/addon.xml')).attrib['version'] == version
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{digest}  {target.name}\n', encoding='ascii')
    print(f'{target}\nSHA256 {digest}\n{target.stat().st_size:,} bytes; {len(files)} files')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    build(parser.parse_args().output.resolve())
