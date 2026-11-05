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
  # The PATCHED kernel source tree (source drv with the stock kernel's own
  # patches plus every host patch applied): the map is computed on post-patch
  # reality, because patches can create new modules/files/Kconfig entries.
  src,
  # The UNPATCHED upstream tree: copyTree detection checks whether a unit's
  # dir/build files even exist before the host patches.
  origSrc ? null,
  # Host-level patches (attrsets with .patch or plain paths) for
  # prepare-relevance classification and per-unit applicability.
  patches ? [ ],
}:

let
  # Accept raw nixpkgs-style patches ({name, patch}, patch = a real nix PATH
  # or store path), plain paths, or the packager's normalized {name, file,
  # raw} entries. Config-only entries (no patch/file) normalize to null and
  # are dropped below.
  normP =
    p:
    let
      raw = if lib.isAttrs p then (p.raw or (p.file or (p.patch or null))) else p;
    in
    if raw == null || raw == "" then
      null
    else {
      name = p.name or (baseNameOf raw);
      file = toString raw;
      # the RAW value (a nix path object or store path) - path values
      # interpolated into derivation attrs always register as inputs,
      # while toString'd strings carry no context in modern Nix
      raw = raw;
    };
  # Entries without a patch file (config-only entries like
  # ec-qcom-hamoa-enable) carry nothing to classify or apply.
  usable = lib.filter (p: p != null) (map normP patches);

  # Sandbox-safe staged filename per patch.
  safeName =
    n: lib.stringAsChars (c: if builtins.match "[A-Za-z0-9._-]" c == null then "_" else c) n;

  # The manifest the classifier receives: names plus RELATIVE staged paths
  # (files are copied into patches/ by the buildPhase before python runs).
  patchManifest = builtins.toJSON (
    # concatImapStrings (staging) is 1-based - match it exactly; paths are
    # relative to the build cwd, where stageLines copies them into patches/
    lib.imap1 (
      i: p: { name = p.name; file = "patches/${toString i}-${safeName p.name}.patch"; }
    ) usable
  );

  # Staging lines. The raw patch path is interpolated directly into the
  # build script, which registers it as a derivation input. Patch files
  # must NOT go into nativeBuildInputs: stdenv's activatePackage sources
  # every single-file input as a setup hook, executing the patch text.
  stageLines = lib.concatImapStrings (
    i: p: ''
      cp ${p.raw} "patches/${toString i}-${safeName p.name}.patch"
    ''
  ) usable;
in

stdenvNoCC.mkDerivation {
  name = "linux-module-map-${kernel.version}";

  inherit src;
  configfile = kernel.configfile;

  nativeBuildInputs = [ python3 ];

  env.arch = kernel.stdenv.hostPlatform.linuxArch;
  env.patchManifest = patchManifest;
  # for copyTree detection (units whose dirs the patches create)
  env.origSrc = if origSrc == null then "" else "${origSrc}";

  buildPhase = ''
    runHook preBuild

    # Stage the patch files into the sandbox (see stageLines: raw path
    # interpolation registers each as an input).
    mkdir -p patches
    ${stageLines}

    # Pick the unpacked kernel tree: a directory containing both Makefile
    # and Kbuild (Documentation also ships a Makefile, so the first dir
    # found is not good enough).
    srcdir=""
    for d in */ .; do
      if [ -f "$d/Makefile" ] && [ -f "$d/Kbuild" ]; then
        srcdir="$d"
        break
      fi
    done
    test -n "$srcdir" || { echo "no kernel source found"; exit 1; }
    echo "mapping kernel source in $srcdir"

    python3 ${./mapper.py} "$srcdir" "$configfile" "$arch" modules.json warnings.txt "$patchManifest" "$origSrc"

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
