"""Stage the built add-on in one existing Kodi repository release channel."""
import argparse
import copy
import hashlib
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import zipfile

from package_addon import ROOT, build


def stage(repository, release):
    repository = repository.resolve(strict=True)
    channel = repository / release
    if not channel.is_dir() or channel.resolve().parent != repository:
        raise ValueError('Select an existing release directory directly inside the repository')
    package = build(ROOT / 'dist')
    manifest = ET.parse(ROOT / 'addon.xml').getroot()
    identity = manifest.attrib['id']
    source = channel / identity
    if source.exists() and ET.parse(source / 'addon.xml').getroot().attrib.get('id') != identity:
        raise ValueError('Existing source folder belongs to a different add-on')
    with zipfile.ZipFile(package) as archive:
        for entry in archive.infolist():
            path = Path(entry.filename)
            if path.is_absolute() or '..' in path.parts or path.parts[0] != identity:
                raise ValueError('Unsafe package member')
        archive.extractall(channel)
    published = channel / 'zips' / identity
    published.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package, published / package.name)
    shutil.copy2(source / 'addon.xml', published / 'addon.xml')
    metadata = manifest.find("extension[@point='xbmc.addon.metadata']")
    for asset in metadata.findall('./assets/*') if metadata is not None else []:
        relative = Path(asset.text or '')
        if not asset.text or relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe metadata asset')
        asset_source = source / relative
        if asset_source.is_file():
            dest = published / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset_source, dest)
    index = channel / 'zips/addons.xml'
    tree = ET.parse(index)
    root = tree.getroot()
    before = {e.attrib['id']: ET.tostring(e) for e in root}
    for item in list(root):
        if item.attrib.get('id') == identity:
            root.remove(item)
    root.append(copy.deepcopy(manifest))
    root[:] = sorted(root, key=lambda e: e.attrib['id'])
    tree.write(index, encoding='utf-8', xml_declaration=True)
    digest = hashlib.md5(index.read_bytes()).hexdigest()
    index.with_suffix('.xml.md5').write_text(digest, encoding='ascii')
    result = ET.parse(index).getroot()
    assert all(ET.tostring(result.find(f"addon[@id='{key}']")) == value
               for key, value in before.items() if key != identity)
    assert result.find(f"addon[@id='{identity}']").attrib['version'] == manifest.attrib['version']
    assert hashlib.sha256((published / package.name).read_bytes()).digest() == hashlib.sha256(package.read_bytes()).digest()
    print(f'Staged {identity} in {channel}; index and MD5 verified; unrelated entries preserved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', type=Path)
    parser.add_argument('--release', default='omega', choices=('matrix', 'nexus', 'omega', 'piers'))
    args = parser.parse_args()
    stage(args.repository, args.release)
