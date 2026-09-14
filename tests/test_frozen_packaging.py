import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frozen_app_uses_per_user_runtime_directory():
    spec = (ROOT / "build" / "stage2.spec").read_text(encoding="utf-8")
    assert "runtime_tmpdir=r'%LOCALAPPDATA%\\Lifted\\Stage2Runtime'" in spec


def test_build_uses_security_fixed_pyinstaller_and_real_gui_smoke_test():
    build = (ROOT / "build" / "build.ps1").read_text(encoding="utf-8")
    assert "pyinstaller==6.22.1" in build.lower()
    assert "smoke_test_frozen_app.ps1" in build


def test_portable_build_bundles_guide_rules_and_real_icon():
    spec = (ROOT / "build" / "stage2.spec").read_text(encoding="utf-8")
    assert "'docs', 'ai-review'" in spec
    assert "'docs', 'USER_GUIDE.pdf'" in spec
    assert "(os.path.join(SRC, 'stage2.ico'), '.')" in spec
    for name in ("REVIEW_RULES.md", "LEARNING_RULES.md", "NAMING_RULES.md", "WORKFLOW_GUIDE.md", "REVIEW_RECORDS.md"):
        assert (ROOT / "docs" / "ai-review" / name).is_file()


def test_release_versions_and_guide_locations_are_consistent():
    import ast
    tree = ast.parse((ROOT / "src" / "Stage2_Processing.pyw").read_text(encoding="utf-8-sig"))
    constants = {node.targets[0].id: node.value.value for node in tree.body
                 if isinstance(node, ast.Assign) and len(node.targets) == 1
                 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
    version = constants["APP_VERSION"]
    for name in ("Stage2_Public_Installer.iss", "Stage2_Installer.iss"):
        assert f'#define MyAppVersion "{version}"' in (ROOT / "build" / "installer" / name).read_text(encoding="utf-8")
    smoke = (ROOT / "tools" / "smoke_test_frozen_app.ps1").read_text(encoding="utf-8")
    assert f'$ExpectedVersion = "{version}"' in smoke
    assert f'$ExpectedBuild = "{constants["APP_BUILD"]}"' in smoke
    # install.ps1 deliberately does NOT pin a version any more: it resolves the
    # latest published release at run time so the README and this repo never go
    # stale when a new version ships. So assert the ABSENCE of a pin - the
    # opposite of what this test used to require - plus the mechanism that
    # replaced it. GitHub's releases/latest endpoint is the part that
    # guarantees drafts and pre-releases are never selected.
    bootstrap = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "releases/latest" in bootstrap
    assert not re.search(r"^\s*\$Tag\s*=\s*['\"]v?\d", bootstrap, re.M), \
        "install.ps1 must not hard-code a release tag"
    installer = (ROOT / "build" / "installer" / "Stage2_Public_Installer.iss").read_text(encoding="utf-8")
    assert 'DestDir: "{app}\\Guides"' in installer


def test_release_checksum_uses_public_download_filename():
    manifest = (ROOT / "build" / "generate_release_checksums.ps1").read_text(encoding="utf-8")
    bootstrap = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert '"Stage2_Processing.exe" = Join-Path $RepoRoot "dist\\Stage 2 - Processing.exe"' in manifest
    # Asset names must match the names GitHub actually serves on the release.
    # Quoting style is not the contract, so match on the assignment itself.
    assert re.search(r"\$AppAsset\s*=\s*['\"]Stage2_Processing\.exe['\"]", bootstrap)
    assert re.search(r"\$GuideAsset\s*=\s*['\"]Stage2_Guide_AI_Processing\.pdf['\"]", bootstrap)
    assert re.search(r"\$SumsAsset\s*=\s*['\"]SHA256SUMS\.txt['\"]", bootstrap)
    # The common verifier validates both public assets by their download names.
    assert '[regex]::Escape($AssetName)' in bootstrap
    assert '-FilePath $downloadedApp -AssetName $AppAsset' in bootstrap
    assert '-FilePath $downloadedPdf -AssetName $GuideAsset' in bootstrap
