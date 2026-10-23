# Per-module kernel packager: module-level "complete build" tier.
#
# Produces a drop-in external-module artifact for one in-tree module:
# the full .ko (linked, kmodigest-signed via kernelModuleMakeFlags'
# cmd_sign override, compressed by modinst) plus its .ko.cer, ready for
# pkgs.aggregateModules (which merges per-module certs into modules.cer).
#
# The tier RESUMES the compile-only drv's stage-1 artifacts instead of
# recompiling: the module's sources and stage-1 outputs are placed at the
# exact sandbox paths the compile drv used, so the recorded command lines
# match and Kbuild finds every object up to date. Only modpost (for this
# module), the modfinal link and modinst (sign/compress/install) run here.
#
# Where the symbol data comes from (honesty note): modpost consumes
#   1. the core's Module.symvers (vmlinux + built-in exports), and
#   2. KBUILD_EXTRA_SYMBOLS entries generated here by running the core's
#      modpost over the dependency modules' composite .o files (from the
#      compile drv's accumulated outputs). With CONFIG_MODVERSIONS unset
#      these entries carry names/namespaces but no real CRCs - enough for
#      symbol resolution and warning hygiene. Modules whose dependency edges
#      the mapper missed (documented limitation) still build: modpost only
#      warns, the undefined symbols are resolved at insmod time, and the
#      authoritative Module.symvers is generated once in the final drv.
{
  lib,
  stdenv,
  kmod,
  xz,
  kernel,
  core,
  compileDrv,
  info,
  kernelModuleMakeFlags,
}:

let
  inherit (kernel) modDirVersion version;

  # The kernelPackages' kernelModuleMakeFlags carry KBUILD_OUTPUT=<kernel.dev>;
  # we build against the core drv's build tree instead, so drop it.
  makeFlags' = builtins.filter (f: !(lib.hasPrefix "KBUILD_OUTPUT=" f)) kernelModuleMakeFlags;
  quotedFlags = lib.concatMapStringsSep " " lib.escapeShellArg makeFlags';
in

stdenv.mkDerivation {
  pname = "linux-module-${info.name}";
  inherit version;

  dontUnpack = true;

  outputs = [ "out" ];

  nativeBuildInputs = kernel.moduleBuildDependencies ++ [ kmod xz ];

  env = {
    coreDev = "${core.dev}";
    coreSource = "${core.source}";
    compileDrv = "${compileDrv}";
  };

  buildPhase = ''
    runHook preBuild

    coreBuild="$coreDev/lib/modules/${modDirVersion}/build"

    # Recreate the compile drv's exact sandbox layout so the recorded
    # command lines match and nothing is recompiled.
    mkdir -p "modsrc/$(dirname "${info.dir}")"
    cp -a --reflink=auto "$compileDrv/self/." "modsrc/${info.dir}/"
    # store paths are read-only; make writable so later copies can
    # overwrite and modpost/modfinal can write into the tree
    chmod -R u+w modsrc

    # extra source dirs the mapper detected (relative-include targets);
    # needed if make decides to recompile anything
    while IFS= read -r line; do
      case "$line" in
        extraDirs=*) extraDirs="''${line#extraDirs=}" ;;
      esac
    done < "$compileDrv/manifest.txt"
    for d in $extraDirs; do
      mkdir -p "modsrc/$d"
      cp -a --reflink=auto "$coreSource/$d/." "modsrc/$d/"
      chmod -R u+w "modsrc/$d"
    done

    mkdir -p modsrc/a/b

    # KBUILD_EXTRA_SYMBOLS: run the core's modpost over the dependency
    # modules' composite .o files to dump their exported symbols.
    if [ -s "$compileDrv/dep-manifest.txt" ]; then
      objlist=()
      while IFS=$'\t' read -r depName depPath; do
        [ -n "$depName" ] || continue
        obj="$depPath/self/$depName.o"
        if [ -f "$obj" ]; then
          objlist+=("$obj")
        else
          echo "warning: dep $depName has no composite object at $obj" >&2
        fi
      done < "$compileDrv/dep-manifest.txt"
      if [ ''${#objlist[@]} -gt 0 ]; then
        # -M enables the symvers dump, -o names it (-M is a boolean flag in
        # Kbuild, not a filename option); -i feeds the core's vmlinux
        # symvers to cut unresolved-symbol noise
        "$coreBuild/scripts/mod/modpost" -w -M -o extra.symvers \
          -i "$coreBuild/Module.symvers" "''${objlist[@]}" \
          || echo "warning: dep symvers extraction failed; continuing without it" >&2
      fi
    fi

    extraArgs=()
    if [ -f extra.symvers ]; then
      extraArgs+=("KBUILD_EXTRA_SYMBOLS=$PWD/extra.symvers")
    fi

    # modules_install implies modules: modpost (this module only) + modfinal
    # link + modinst (kmodigest sign, compression, install, depmod maps).
    # Single-module FULL build via Kbuild's single-ko flow (make M=dir
    # <name>.ko): it writes a one-entry modules.order in the make cwd,
    # runs modpost (once, for exactly this module) and modfinal, then
    # removes the temporary file. KBUILD_MODPOST_WARN=1: a module whose
    # deps are not in the provided symvers gets warnings instead of
    # errors - the undefined symbols are resolved at insmod time against
    # the aggregated tree, which is the honest semantics for a drop-in
    # artifact.
    make -C "$coreBuild" -j"$NIX_BUILD_CORES" \
      ${quotedFlags} \
      "KCFLAGS=-I$PWD/modsrc/a/b" \
      "KBUILD_MODPOST_WARN=1" \
      "M=$PWD/modsrc/${info.dir}" \
      "''${extraArgs[@]}" \
      "${info.name}.ko"

    # Install exactly this module: modinst (sign/compress/install) reads
    # MODORDER from the make cwd, so point it at a one-entry list.
    echo "${info.name}.o" > "$coreBuild/modules.order"
    make -C "$coreBuild" -j"$NIX_BUILD_CORES" \
      ${quotedFlags} \
      "M=$PWD/modsrc/${info.dir}" \
      "INSTALL_MOD_PATH=$out" \
      modules_install
    rm -f "$coreBuild/modules.order"

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    # The build covers every =m module in the Kbuild dir; keep only the
    # requested module's drop-in files (.ko*, including its .ko.cer).
    find "$out/lib/modules/${modDirVersion}/kernel" -type f \
      ! -name '${info.name}.ko*' -delete
    find "$out/lib/modules/${modDirVersion}/kernel" -type d -empty -delete
    # depmod maps at the tree root collide with the kernel modules tree in
    # aggregateModules; the aggregator regenerates them itself.
    rm -f "$out/lib/modules/${modDirVersion}"/modules.*
    runHook postInstall
  '';

  passthru = {
    inherit (info) dir;
    inherit compileDrv core;
  };

  meta = {
    description = "drop-in complete build of the in-tree kernel module ${info.name} (kernel ${version})";
    platforms = [ kernel.stdenv.hostPlatform.system ];
  };
}
