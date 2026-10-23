# Per-module kernel packager: module map derivation.
#
# Runs mapper.py over the kernel source and the final .config (via IFD) and
# produces a JSON module map: module name -> Kbuild dir, object list, source
# files and conservative module->module dependency edges.
#
# Why a derivation instead of pure-Nix Makefile parsing: a full NixOS config
# yields ~8000 modules spread over thousands of Makefiles; a Python walker
# does the whole tree in ~3 s, while pure-Nix parsing would cost minutes of
# eval and a lot of memory. The map is imported at eval time (IFD).
{
  lib,
  stdenvNoCC,
  python3,
  kernel,
}:

stdenvNoCC.mkDerivation {
  name = "linux-module-map-${kernel.version}";

  src = kernel.src;
  configfile = kernel.configfile;

  nativeBuildInputs = [ python3 ];

  env.arch = kernel.stdenv.hostPlatform.linuxArch;

  buildPhase = ''
    runHook preBuild

    srcdir=$(find . -maxdepth 1 -mindepth 1 -type d | head -n1)
    test -f "$srcdir/Makefile" || { echo "no kernel source found"; exit 1; }

    python3 ${./mapper.py} "$srcdir" "$configfile" "$arch" modules.json warnings.txt

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm644 modules.json $out/modules.json
    install -Dm644 warnings.txt $out/warnings.txt
    runHook postInstall
  '';

  meta.platforms = [ kernel.stdenv.hostPlatform.system ];
}
