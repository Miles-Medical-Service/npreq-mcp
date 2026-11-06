#!/usr/bin/env python3
"""Release check: the tag, pyproject.toml, server.json, the plugin manifest and the server all carry one version,
and server.json names this package. Run by the release workflow before it builds: python3 tools/check_release.py v0.1.0
(with no tag, it checks only that the files agree). Exit 0 when they do; otherwise each mismatch is printed."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def problems(tag=None, root=ROOT):
    out = []
    pyproject = (root / 'pyproject.toml').read_text(encoding='utf-8')
    py_version = re.search(r'^version = "([^"]+)"$', pyproject, re.M)
    py_name = re.search(r'^name = "([^"]+)"$', pyproject, re.M)
    server_src = (root / 'npreq_mcp.py').read_text(encoding='utf-8')
    srv_version = re.search(r"^VERSION = '([^']+)'$", server_src, re.M)
    server = json.loads((root / 'server.json').read_text(encoding='utf-8'))
    plugin = json.loads((root / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    readme = (root / 'README.md').read_text(encoding='utf-8')
    if not (py_version and py_name and srv_version):
        return ['pyproject.toml or npreq_mcp.py: version or name not found']
    version = py_version.group(1)
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        out.append(f'pyproject version {version!r} is not X.Y.Z')
    pkgs = [p for p in server.get('packages', []) if p.get('registryType') == 'pypi']
    for label, value in (('npreq_mcp.VERSION', srv_version.group(1)), ('server.json version', server.get('version')),
                         ('server.json pypi package version', pkgs[0].get('version') if pkgs else None),
                         ('plugin.json version', plugin.get('version'))):
        if value != version:
            out.append(f'{label} is {value!r}, pyproject says {version!r}')
    if len(pkgs) != 1 or pkgs[0].get('identifier') != py_name.group(1):
        out.append(f'server.json needs one pypi package with identifier {py_name.group(1)!r}')
    if f'<!-- mcp-name: {server.get("name")} -->' not in readme:
        out.append(f'README.md lacks the line <!-- mcp-name: {server.get("name")} --> (PyPI ownership proof)')
    if tag is not None and tag != f'v{version}':
        out.append(f'tag {tag!r} is not v{version}')
    return out


def main(argv):
    found = problems(argv[0] if argv else None)
    for p in found:
        print('release check:', p)
    if not found:
        print('release check: ok')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
