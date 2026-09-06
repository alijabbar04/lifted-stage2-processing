#!/usr/bin/env python3
r"""
Reproducible builder for the Stage2 Processing Windows installer.

    python build_installer.py

Phases
------
A. PyInstaller-build the payload exe ("Stage2 Processing.exe") and the tiny
   credential helper ("stage2_credhelper.exe").
B. (Stage2_Installer.iss is a static file in this folder.)
C. Compile the private, credential-bearing Inno Setup installer with iscc.exe
   (installing Inno Setup via winget if it is missing), producing
   Output\Stage2_Processing_Preconfigured_Setup.exe.

The Anthropic API key
---------------------
Read from the ANTHROPIC_API_KEY environment variable, else from a
`.installer_secrets` file in this folder (a single line, or KEY=VALUE, or
ANTHROPIC_API_KEY=VALUE). The build FAILS with a clear message if no key is
found. The key is written to a temporary `apikey.dat` that Inno embeds as a
compiled-in resource; build_installer.py deletes `apikey.dat` again as soon as
iscc finishes. The key is never written into the .iss file or the payload exe,
and never committed (see .gitignore).

Inputs / overrides
------------------
    --pyw PATH   Stage2_Processing.pyw   (default: this folder, else the known
                                          project location)
    --ico PATH   stage2.ico              (default: this folder, else Downloads)
    --skip-exe   reuse an existing dist\ build (faster re-compiles of the .iss)
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Fallbacks used only if the file isn't found next to this script. In this
# repository both live in src/ (this script sits in build/installer/), so the
# fallbacks are repo-relative - no developer-specific absolute paths.
REPO = HERE.parent.parent
KNOWN_PYW = REPO / "src" / "Stage2_Processing.pyw"
KNOWN_ICO = REPO / "src" / "stage2.ico"

# Pinned fallback if the live LibreOffice mirror can't be reached at build time.
LO_FALLBACK_VER = "26.2.4"
LO_BASE = "https://download.documentfoundation.org/libreoffice/stable"


def die(msg: str, code: int = 1):
    print(f"\n[BUILD ERROR] {msg}\n", file=sys.stderr)
    sys.exit(code)


def info(msg: str):
    print(f"[build] {msg}")


# --------------------------------------------------------------------------- #
#  Inputs
# --------------------------------------------------------------------------- #
def read_api_key() -> str:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        info("API key: from ANTHROPIC_API_KEY environment variable.")
        return key
    secrets = HERE / ".installer_secrets"
    if secrets.exists():
        for raw in secrets.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip().upper() in ("ANTHROPIC_API_KEY", "API_KEY", "KEY"):
                    key = v.strip().strip('"').strip("'")
                    break
            else:
                key = line.strip('"').strip("'")
                break
        if key:
            info("API key: from .installer_secrets file.")
            return key
    die("No Anthropic API key found.\n"
        "        Set it before building, either:\n"
        "          - set ANTHROPIC_API_KEY=sk-ant-...   (environment variable), or\n"
        "          - create a '.installer_secrets' file in this folder containing\n"
        "            a single line:  ANTHROPIC_API_KEY=sk-ant-...\n"
        "        (.installer_secrets is git-ignored and never bundled as-is.)")


def resolve_input(name: str, override: str, known: Path) -> Path:
    if override:
        p = Path(override)
        if not p.exists():
            die(f"{name}: '{p}' does not exist.")
        return p
    local = HERE / name
    if local.exists():
        return local
    if known.exists():
        return known
    die(f"{name}: not found next to build_installer.py or at {known}. "
        f"Pass its path with --{name.split('.')[0].split('_')[0].lower()}.")


# --------------------------------------------------------------------------- #
#  LibreOffice: resolve the current stable x64 MSI URL at build time
# --------------------------------------------------------------------------- #
def _get(url: str, timeout=25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def resolve_libreoffice() -> tuple[str, str]:
    """Return (msi_url, msi_filename) for the latest stable x64 MSI, resolved
    live from the Document Foundation mirror; falls back to a pinned version."""
    try:
        listing = _get(LO_BASE + "/")
        vers = sorted(
            set(re.findall(r'href="(\d+\.\d+\.\d+)/"', listing)),
            key=lambda s: tuple(int(x) for x in s.split(".")),
        )
        if not vers:
            raise RuntimeError("no version dirs found")
        ver = vers[-1]
        sub = _get(f"{LO_BASE}/{ver}/win/x86_64/")
        m = re.findall(r'(LibreOffice_[\d.]+_Win_x86-64\.msi)', sub)
        if not m:
            raise RuntimeError("no msi found in version dir")
        name = sorted(set(m))[-1]
        url = f"{LO_BASE}/{ver}/win/x86_64/{name}"
        info(f"LibreOffice: resolved latest stable {ver} -> {name}")
        return url, name
    except Exception as e:
        ver = LO_FALLBACK_VER
        name = f"LibreOffice_{ver}_Win_x86-64.msi"
        url = f"{LO_BASE}/{ver}/win/x86_64/{name}"
        info(f"LibreOffice: live lookup failed ({e}); using pinned {ver}.")
        return url, name


# --------------------------------------------------------------------------- #
#  Tools
# --------------------------------------------------------------------------- #
def run(cmd: list, **kw):
    info("run: " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kw)


def find_iscc() -> str | None:
    la = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    if la:
        candidates.append(str(Path(la) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    for p in candidates:
        if Path(p).exists():
            return p
    return shutil.which("iscc")


def ensure_iscc() -> str:
    iscc = find_iscc()
    if iscc:
        info(f"Inno Setup found: {iscc}")
        return iscc
    info("Inno Setup not found — installing via winget…")
    try:
        run(["winget", "install", "--id", "JRSoftware.InnoSetup", "-e",
             "--silent", "--accept-package-agreements",
             "--accept-source-agreements"])
    except Exception as e:
        die(f"Could not install Inno Setup via winget ({e}).\n"
            f"        Install it manually from https://jrsoftware.org/isdl.php "
            f"and re-run.")
    iscc = find_iscc()
    if not iscc:
        die("Inno Setup installed but ISCC.exe still not found. "
            "Re-run this script (a new shell may be needed to pick up PATH).")
    info(f"Inno Setup installed: {iscc}")
    return iscc


# --------------------------------------------------------------------------- #
#  Phases
# --------------------------------------------------------------------------- #
def phase_a_build_exes(pyw: Path, ico: Path, skip: bool):
    dist = HERE / "dist"
    app_exe = dist / "Stage2 Processing.exe"
    cred_exe = dist / "stage2_credhelper.exe"
    if skip and app_exe.exists() and cred_exe.exists():
        info("Phase A skipped (--skip-exe): reusing existing dist\\ build.")
        return app_exe, cred_exe

    # Match the public build's pinned freezer and bundled workflow assets.
    run([sys.executable, "-m", "pip", "install", "pyinstaller==6.22.1"])

    common = [sys.executable, "-m", "PyInstaller", "--noconfirm",
              "--distpath", str(dist), "--workpath", str(HERE / "build"),
              "--specpath", str(HERE)]

    info("Phase A: building the payload exe (Stage2 Processing.exe)…")
    run(common + ["--onefile", "--windowed", "--icon", str(ico),
                  "--runtime-tmpdir", r"%LOCALAPPDATA%\Lifted\Stage2Runtime",
                  "--hidden-import", "onnxruntime",
                  "--add-data", str(REPO / "assets" / "orientation") + ";assets/orientation",
                  "--add-data", str(REPO / "docs" / "ai-review") + ";docs/ai-review",
                  "--add-data", str(REPO / "docs" / "USER_GUIDE.pdf") + ";docs",
                  "--add-data", str(ico) + ";.",
                  "--name", "Stage2 Processing", str(pyw)])
    if not app_exe.exists():
        die(f"PyInstaller did not produce {app_exe}")

    info("Phase A: building the credential helper (stage2_credhelper.exe)…")
    run(common + ["--onefile", "--console", "--name", "stage2_credhelper",
                  "--collect-all", "keyring", str(HERE / "credhelper.py")])
    if not cred_exe.exists():
        die(f"PyInstaller did not produce {cred_exe}")

    info(f"Phase A OK: {app_exe.name}  +  {cred_exe.name}")
    return app_exe, cred_exe


def phase_c_compile(iscc: str, ico: Path, api_key: str, lo_url: str,
                    lo_msi: str):
    # place the icon next to the .iss so SetupIconFile / [Files] resolve it
    local_ico = HERE / "stage2.ico"
    if ico.resolve() != local_ico.resolve():
        shutil.copyfile(ico, local_ico)

    # embed the key ONLY as a temporary resource file that Inno compiles in
    keyfile = HERE / "apikey.dat"
    keyfile.write_text(api_key, encoding="utf-8")
    try:
        info("Phase C: compiling the installer with Inno Setup…")
        run([iscc,
             f"/DLoUrl={lo_url}",
             f"/DLoMsi={lo_msi}",
             str(HERE / "Stage2_Installer.iss")])
    finally:
        # scrub the plaintext key file from the build machine immediately
        try:
            keyfile.unlink()
        except Exception:
            pass

    out = HERE / "Output" / "Stage2_Processing_Preconfigured_Setup.exe"
    if not out.exists():
        die(f"Inno Setup did not produce {out}")
    info(f"Phase C OK: {out}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Build the Stage2 installer.")
    ap.add_argument("--pyw", default="", help="path to Stage2_Processing.pyw")
    ap.add_argument("--ico", default="", help="path to stage2.ico")
    ap.add_argument("--skip-exe", action="store_true",
                    help="reuse an existing dist\\ build")
    args = ap.parse_args()

    print("=" * 70)
    print(" Stage2 Processing — installer build")
    print("=" * 70)

    api_key = read_api_key()                       # fails clearly if missing
    pyw = resolve_input("Stage2_Processing.pyw", args.pyw, KNOWN_PYW)
    ico = resolve_input("stage2.ico", args.ico, KNOWN_ICO)
    info(f"payload source : {pyw}")
    info(f"icon           : {ico}")

    lo_url, lo_msi = resolve_libreoffice()
    iscc = ensure_iscc()                           # early, so failures surface
    phase_a_build_exes(pyw, ico, args.skip_exe)
    out = phase_c_compile(iscc, ico, api_key, lo_url, lo_msi)

    print("\n" + "=" * 70)
    print(" BUILD COMPLETE")
    print("=" * 70)
    print(f" Installer: {out}")
    print(f" LibreOffice MSI (installed on first run): {lo_msi}")
    print("   silent command: msiexec /i \"<msi>\" /qn /norestart")
    print(" Double-click the installer to install Stage 2 on a colleague's PC.")
    print("=" * 70)


if __name__ == "__main__":
    main()
