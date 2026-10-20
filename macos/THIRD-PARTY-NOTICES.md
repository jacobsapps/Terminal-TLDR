# Bundled runtime notices

The app bundles Python and the iTerm2 Python API. It does not bundle Codex,
Claude, any login state, or any API credentials.

- Python is distributed under the Python Software Foundation licence and the
  notices included in `Licenses/Python.txt`.
- The iTerm2 Python API 2.20 declares GPLv2 in its package metadata. Its licence
  is included in `Licenses/iTerm2.txt`. Corresponding Python sources are included
  in the runtime. Upstream: https://github.com/gnachman/iTerm2
- Protobuf and websockets retain their upstream licences in `Licenses/`.
- PyInstaller's bootloader is covered by its GPL licence with the bootloader
  exception, included in `Licenses/PyInstaller.txt`.

The app currently has no public release. Review the iTerm2 API's GPL terms
before choosing the project's distribution licence. The repository's privacy
and public-release review is separate from local builds.
