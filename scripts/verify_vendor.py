# Created by d.kotina on 25.11.2026
"""Проверяет полный список SHA зафиксированного public codec до Linux проверки и mirror runtime."""

import hashlib
from pathlib import Path

from scripts.wikimedia.common import read_json, require

# MARK: - Vendored Codec Integrity


def main():
    """Сверяет только известные scripts/schemas из локального manifest, не читая произвольные внешние пути."""
    root = Path(__file__).resolve().parent.parent
    manifest = read_json(root / 'vendor-sources.json')
    for name, expected in manifest['files'].items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts
                and relative.parts[0] in {'scripts', 'schemas'}, 'invalid_vendor_manifest_path')
        target = root / relative
        require(not target.is_symlink() and target.is_file()
                and hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'vendor_codec_hash_mismatch')
    print('Verified ' + str(len(manifest['files'])) + ' public codec/schema files')


if __name__ == '__main__':
    main()
