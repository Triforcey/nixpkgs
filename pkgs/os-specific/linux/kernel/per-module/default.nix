# Per-module kernel packager (spike).
#
# ABSOLUTE RULE: no full tree build, ever. Everything that compiles happens
# in per-unit derivations - =m modules AND built-in (obj-y) directories -
# and the final drv only links:
#
#   linux-patched-source    source-only drv: stock patches + every host
#                           patch applied. The MAP's input - the walk sees
#                           patch-created modules/files. Never an input to
#                           any compile drv.
#   linux-module-map.drv    mapper over the patched source: per-unit dir,
#                           objects, include closures, dependency edges, and
#                           per-unit patch subsets (prepare-relevant
#                           classification + copyTree detection).
#   linux-core.drv          prepare-ONLY: modules_prepare (+ dtbs on
#                           aarch64) - zero kernel object compilation. The
#                           shared baseline every unit compiles against.
#   linux-module-<name>.d   COMPILE-ONLY per-unit stage-1 drv: seeds its
#                           sources from the unpatched upstream tree (or a
#                           reflink copy of the prepared build+source trees
#                           when its patch set reaches outside the cheap
#                           scratch) and applies its own applicable patch
#                           subset.
#   linux.drv (aggregate)   seeds every unit's stage-1 artifacts into its
#                           own build tree, links vmlinux (built-in.a +
#                           final link) and runs the ONE global modpass /
#                           per-module ld / kmodigest signing / depmod over
#                           everything. Unit artifacts are drv inputs, so
#                           nix builds them all before the aggregate.
#   linux-image             existing nixpkgs rebuild-image (insert-sys-cert
#                           with the merged modules.cer) over the aggregate
#
# Design notes and verification status: see NOTES.md in this directory.
{
  lib,
  stdenv,
  stdenvNoCC,
  callPackage,
  patch,
  writeText,
  kernel,
  flex,
  bison,
  zlib,
  # Stock kernelPackages set (pre-host-patches). Needed to re-derive the
  # core kernel with ONLY prepare-relevant host patches (patch localization
  # v2). null keeps the legacy behavior (core = the fully patched kernel).
  kernelPackages ? null,
  # Kernel-build customizations threaded from the NixOS boot machinery
  # (must match what the glue applied to `kernel`).
  randstructSeed ? "",
  features ? { },
  # Host-level patches (attrsets {name, patch} or plain paths). The mapper
  # classifies them prepare-relevant vs localized and computes per-unit
  # applicability.
  kernelPatches ? [ ],
  # The stock kernel's own patches - re-applied automatically by the
  # override merge when coreKernel is re-derived; passed so the patched
  # source/map can stage them and the classifier can recognize them
  # (basePatches that reach unit sets are harmless: --forward skips
  # already-applied hunks).
  basePatches ? [ ],
  kernelModuleMakeFlags ? kernel.commonMakeFlags ++ [
    "DEPMOD=true"
    "INSTALL_MOD_STRIP=1"
  ],
  # Escape hatch: extra source dirs some modules need beyond their own
  # (mapper cannot see all header-dir relationships). attrset name -> [dir].
  extraSourceDirs ? { },
}:

let
  inherit (kernel) modDirVersion version;

  # The arch/ directory in the kernel build tree (the top Makefile's
  # SRCARCH). Differs from the make ARCH value (kernelArch =
  # hostPlatform.linuxArch, e.g. "x86_64") on x86: make ARCH=x86_64 builds
  # into arch/x86/. aarch64's linuxArch ("arm64") is already the directory
  # name. Same subst the kernel Makefile applies via
  # `SRCARCH := $(subst x86_64,x86,$(SRCARCH))`.
  srcArch =
    {
      x86_64 = "x86";
      i386 = "x86";
      i686 = "x86";
    }
    .${kernel.stdenv.hostPlatform.linuxArch}
    or kernel.stdenv.hostPlatform.linuxArch;

  # Full mode only: every =m module in the map gets its own compile drv.
  # There is no subset mode - a subset saves nothing (the aggregate
  # recompiles uncovered modules internally) and the subset manifest changes
  # the aggregate's drv identity, fragmenting the one drv that matters for
  # cache reuse. See NOTES.md.
  # Host-level patches, normalized for the mapper and the module drvs.
  # normalized patch entry with the RAW value kept: config-only entries
  # (no patch/file) normalize to null and are dropped where consumed.
  # toString'd paths lose context - the raw PATH VALUE is what every
  # drv-attr interpolation must use (path values always register inputs).
  normPatch =
    p:
    let
      raw = if lib.isAttrs p then (p.patch or p.file or null) else p;
    in
    if raw == null then null else { name = p.name or (baseNameOf raw); file = toString raw; raw = raw; };
  hostPatches = lib.filter (p: p != null) (map normPatch kernelPatches);
  patchFile = builtins.listToAttrs (
    map (p: lib.nameValuePair p.name p.file) hostPatches
  );

  # Sandbox-safe staged filename component (shared with mapper.nix's
  # staging convention).
  safeName =
    n: lib.stringAsChars (c: if builtins.match "[A-Za-z0-9._-]" c == null then "_" else c) n;

  # Fully patched source tree: the stock kernel's own patches plus every
  # host patch, applied at the tree root. THE MAP'S INPUT ONLY - it is
  # never an input to any compile drv (per-unit drvs apply their own
  # subsets to the unpatched upstream tree or to a reflink copy of the
  # prepare trees), so a patch edit re-hashes the map drv (a cheap walk)
  # plus the affected units - never the world.
  patchedSource =
    let
      stageLines = lib.concatImapStrings (
        i: p: ''
          cp ${patchRaw.${p.name}} "patches/${toString i}-${safeName p.name}.patch"
        ''
      ) usablePatches;
    in
    stdenvNoCC.mkDerivation {
      pname = "linux-${version}-patched-source";
      inherit version;
      src = kernel.src;
      dontBuild = true;
      dontFixup = true;
      nativeBuildInputs = [ patch ];
      env.patchOrder = lib.concatStringsSep " " (
        lib.imap1 (
          i: p: "patches/${toString i}-${safeName p.name}.patch"
        ) usablePatches
      );
      installPhase = ''
        runHook preInstall
        mkdir -p patches
        ${stageLines}
        # locate the unpacked tree (genericBuild cds into the source root,
        # so the relative $sourceRoot may no longer resolve from here)
        srct=""
        if [ -d "$sourceRoot" ]; then srct="$sourceRoot";
        elif [ -d "./$sourceRoot" ]; then srct="./$sourceRoot";
        elif [ -f "Makefile" ] && [ -f "Kbuild" ]; then srct="$PWD"; fi
        [ -n "$srct" ] || { echo "cannot locate kernel source tree" >&2; exit 1; }
        # apply strictly: a patch that fails here is a packaging error
        for p in $patchOrder; do
          patch --forward -p1 -d "$srct" < "$p"
        done
        mkdir -p "$out"
        cp -a --reflink=auto "$srct/." "$out/"
        runHook postInstall
      '';
    };

  mapDrv = callPackage ./mapper.nix {
    inherit kernel;
    # the PATCHED source: the map must see patch-created modules/files
    src = patchedSource;
    # the unpatched upstream tree: copyTree detection
    origSrc = unpackedSource;
    # RAW patch values (nix paths, not toString'd strings) - mapper.nix
    # needs the path context to register the files as derivation inputs.
    # MERGED list, stock patches first (application order) so per-unit sets
    # can carry base-patch hunks that touch unit dirs (the modsrc seed is
    # unpatched upstream).
    patches = lib.unique usablePatches;
  };
  moduleMap = builtins.fromJSON (builtins.readFile "${mapDrv}/modules.json");

  # normalized patch entries with RAW values (paths keep context), stock
  # patches first (application order); config-only entries dropped
  usablePatches = lib.filter (p: p != null) (
    lib.unique (map normPatch (basePatches ++ kernelPatches))
  );

  # name -> raw patch value (real path value with context, for drv inputs)
  patchRaw = builtins.listToAttrs (
    map (p: lib.nameValuePair p.name p.raw) usablePatches
  );

  # Patch localization v2: prepare-relevant host patches (root build files,
  # scripts/, prepare-time generated-header closure) also feed the prepare
  # tree via the override merge; every other patch reaches units ONLY
  # through their own per-unit subsets, applied inside the unit's compile
  # drv. basePatches are the stock kernel's own - they are part of kernel
  # identity, so unit sets that include them are harmless (the copy case
  # skips already-applied hunks via `patch --forward`; the common case
  # applies them to the unpatched modsrc exactly once).
  prepareNames = map (p: p.name) (
    lib.filter (p: p.prepare or false) (moduleMap.patches or [ ])
  );
  baseNames = map (p: p.name) (
    lib.filter (p: p != null) (map normPatch basePatches)
  );
  prepareHostPatches = lib.filter (
    p: lib.elem p.name prepareNames && !lib.elem p.name baseNames
  ) hostPatches;

  # The prepare-only core: the stock kernel re-derived with ONLY the
  # prepare-relevant host patches (its own patches merge in via override),
  # reduced to modules_prepare - zero kernel object compilation. A host
  # patch edit re-hashes the prepare tree only when the mapper classified
  # it prepare-relevant; everything else localizes to the affected units.
  coreKernel =
    if kernelPackages == null then
      kernel
    else
      kernelPackages.kernel.override (orig: {
        inherit randstructSeed;
        kernelPatches = (orig.kernelPatches or [ ]) ++ prepareHostPatches;
        features = lib.recursiveUpdate (orig.features or { }) features;
      });

  # Unpatched upstream source tree: what every module compile drv seeds its
  # modsrc from. Identity depends on the source tarball only - patch edits
  # do not touch this drv, and all modules share one copy of the tree.
  unpackedSource = stdenvNoCC.mkDerivation {
    pname = "linux-${version}-unpatched-source";
    inherit version;
    src = kernel.src;
    dontBuild = true;
    dontFixup = true;
    installPhase = ''
      runHook preInstall
      # locate the tree: genericBuild cds into the unpacked source root, so
      # the relative $sourceRoot may no longer resolve from here
      srct=""
      if [ -d "$sourceRoot" ]; then srct="$sourceRoot";
      elif [ -d "./$sourceRoot" ]; then srct="./$sourceRoot";
      elif [ -f "Makefile" ] && [ -f "Kbuild" ]; then srct="$PWD"; fi
      [ -n "$srct" ] || { echo "cannot locate kernel source tree" >&2; exit 1; }
      mkdir -p "$out"
      cp -a --reflink=auto "$srct/." "$out/"
      runHook postInstall
    '';
  };

  # Make flags for the compile-only module drvs. Same toolchain wiring as the
  # core build so recorded command lines stay comparable across the two
  # stages. `O=` and the output-specific install flags are deliberately not
  # carried over; the module builds run against the core's prepared build
  # tree via `make -C`.
  moduleMakeFlags =
    kernel.commonMakeFlags
    ++ [
      "KBUILD_BUILD_TIMESTAMP=@$(SOURCE_DATE_EPOCH)"
      # same make-variable hygiene as the core build
      "--eval=undefine modules"
      "--eval=undefine src"
    ];

  # Prepare-only core: zero kernel object compilation (ABSOLUTE RULE - no
  # full tree build, ever). modules_prepare compiles the host build tools
  # and generates the interface every unit compiles against: scripts/,
  # include/generated (autoconf, asm-offsets), scripts/module.lds. The
  # device trees on aarch64 are DTS compiles, not kernel objects. vmlinux,
  # the built-in .o files and Module.symvers are produced by the
  # aggregate's link step over the seeded per-unit artifacts.
  core = coreKernel.overrideAttrs (old: {
    outputs = [ "out" "dev" "source" ];
    buildFlags =
      [ "modules_prepare" ]
      ++ lib.optionals kernel.buildDTBs [ "dtbs" ];
    # NB: modules_prepare itself builds the host tools the per-object
    # compiles need (objtool, resolve_btfids) - they end up in the retained
    # tree below, so unit sandboxes never descend into the tools chain.
    # no boot image here (that is the aggregate's link step), so the stock
    # installTargets' image install is replaced wholesale below; the dtbs
    # install on aarch64 lands in $out/dtbs (INSTALL_DTBS_PATH)
    installTargets = lib.optionals kernel.buildDTBs [ "dtbs_install" ];
    preInstall = (old.preInstall or "") + ''
      # Snapshot the full patched source before the dev-tree handling.
      # Copy-case units seed their private trees from here.
      # NB: the output is named "source", not "src" - an output named src
      # would shadow the src attribute (env var) and break unpacking.
      # NB2: the source unpacks straight into dev/lib/modules/<ver>/source
      # (build.nix preUnpack/postUnpack), so it is complete at preInstall.
      mkdir -p "${placeholder "source"}"
      cp -a --reflink=auto "$dev/lib/modules/${modDirVersion}/source/." "${placeholder "source"}/"
    '';
    installPhase = ''
      runHook preInstall
      ${lib.optionalString kernel.buildDTBs ''
        make "''${makeFlags[@]}" dtbs_install
      ''}
      mkdir -p "$out"
      touch "$out/.prepare-only"
      runHook postInstall
    '';
    # Retain the build tree UNCLEANED. The stock postInstall runs `make
    # clean` and keeps only a keepPaths list - that deletes the generated
    # files (scripts/kconfig sources, include/{config,generated}) that the
    # out-of-tree-style per-unit make invocations would otherwise
    # regenerate with host tools (flex, bison) the unit sandboxes do not
    # carry. The modules_prepare output is exactly what the units need.
    postInstall = ''
      # Retain the build tree UNCLEANED. The stock postInstall runs `make
      # clean` and keeps only a keepPaths list - that deletes the generated
      # files (scripts/kconfig sources, include/{config,generated}) that the
      # out-of-tree-style per-unit make invocations would otherwise
      # regenerate with host tools (flex, bison) the unit sandboxes do not
      # carry. The modules_prepare output is exactly what the units need.
      mkdir -p "$dev/lib/modules/${modDirVersion}"
      mv "$buildRoot" "$dev/lib/modules/${modDirVersion}/build"
      # regenerate the outputmakefile shim at the retained paths
      buildRoot="$dev/lib/modules/${modDirVersion}/build"
      make "''${makeFlags[@]}" outputmakefile
      # dereference the .config symlink into a real file: its target (the
      # configfile drv) is NOT an input of the unit drv sandboxes, where the
      # dangling symlink makes make treat the config as missing and rerun
      # syncconfig - which prompts for patch-added symbols and rebuilds the
      # kconfig host tools. A real file carries the fully settled config.
      cp -L "$buildRoot/.config" "$NIX_BUILD_TOP/.config.real"
      mv -f "$NIX_BUILD_TOP/.config.real" "$buildRoot/.config"
      # keep the config state strictly newer than the dereferenced .config:
      # a unit-side make with a newer .config would rerun syncconfig, whose
      # prompts for patch-added symbols die on the sandbox's closed stdin
      sleep 1.1
      touch "$buildRoot/include/config/auto.conf" \
        "$buildRoot/include/config/auto.conf.cmd" \
        "$buildRoot/include/generated/autoconf.h"
    '';
  });

  mkCompileModule =
    self: m:
    let
      depPairs = map (
        dep:
        let
          drv = self.${dep} or null;
        in
        {
          name = lib.head (lib.splitString "@" dep);
          inherit drv;
        }
      ) m.deps;
      presentDeps = lib.filter (p: p.drv != null) depPairs;
      # Directories outside the module dir whose headers the module's
      # sources include via relative paths (mapper-detected), plus any
      # user-provided escape hatch. Over-approximation is safe.
      extraDirs = lib.unique (
        (m.extraDirs or [ ]) ++ extraSourceDirs.${m.name} or [ ]
      );
      quotedFlags = lib.concatMapStringsSep " " lib.escapeShellArg moduleMakeFlags;
      # make target for the stage-1 object build. For composite modules
      # (and single objects named <name>.o at the dir root) the kbuild
      # module-name target <name>.o works. For single-object modules whose
      # object carries a subdir prefix (ti: obj-... += icssg/icss_iep.o;
      # efi: stmm/tee_stmm_efi.o) the bare <name>.o goal has no rule (the
      # pattern rule's stem resolves against the dir root, where no .c
      # exists); the goal must carry the object path so the %.o pattern
      # rule resolves to the real source.
      target = if builtins.length m.objects == 1
        then builtins.head m.objects
        else "${m.name}.o";
    in
    stdenv.mkDerivation {
      pname = "linux-module-${m.name}";
      inherit version;

      # No src unpack: sources come from the unpatched upstream source drv,
      # and the module's own applicable patches are applied in buildPhase.
      dontUnpack = true;

      outputs = [ "out" ];

      # The kernel's FULL nativeBuildInputs (not just moduleBuildDependencies):
      # built-in and copy-case units run out-of-tree style make, whose prepare
      # chain carries FORCE-prerequisite rules for the host tools (objtool,
      # resolve_btfids, sign-file) - make remakes them EVERY time regardless
      # of mtimes, so the sandbox must carry their link deps (zlib, elfutils,
      # openssl). Plus patch (the tool) and flex/bison for kconfig regen.
      nativeBuildInputs = kernel.nativeBuildInputs ++ kernel.moduleBuildDependencies ++ [ patch flex bison zlib ];

      # Dependency modules as sandbox inputs. Their outputs are only needed
      # by consumers (the module-level tier reads accumulated dep objects);
      # the compile itself needs no dep artifacts (principle 4: no linking,
      # no symbol resolution below the final drv).
      env = {
        coreDev = "${core.dev}";
        # Patch localization v2: the unit seeds its sources from the
        # unpatched upstream tree (common case) or from a reflink copy of
        # the prepare trees (copy case) and applies its own applicable
        # patch subset. Nothing consumes a fully-patched tree.
        moduleSource = "${unpackedSource}";
        isBuiltin = if (m.builtin or false) then "1" else "";
        needsCopyTree = if (m.copyTree or false) then "1" else "";
        # built-in units compile in-tree object targets (srctree-relative,
        # no M=); the artifacts land in the objtree, which the copy case
        # makes writable
        builtinTargets = lib.concatStringsSep " " (
          map (o: "${m.dir}/${o}") (m.objects or [ ])
        );
        # staged patch filenames (relative to the build sandbox cwd; the
        # buildPhase copies each patchRaw file under these names before
        # applying them). concatImapStrings in the buildPhase is 1-based.
        modulePatches = lib.concatStringsSep " " (
          lib.imap1 (
            i: nm: "${toString i}-${safeName nm}.patch"
          ) (m.patches or [ ])
        );
        depsList = lib.concatMapStringsSep " " (p: "${p.drv}") presentDeps;
        depManifest = lib.concatStringsSep "\n" (
          map (p: "${p.name}\t${p.drv}") presentDeps
        );
      } // lib.optionalAttrs (lib.any (s: lib.hasSuffix ".rs" s) (m.sources or [ ])) {
        # The unit's silent syncconfig re-runs the Kconfig probes in THIS
        # sandbox, and scripts/rust_is_available.sh checks the 'core'
        # stdlib source via RUST_LIB_SRC (the sysroot fallback does not
        # exist in nixpkgs rustc). Without it RUST_IS_AVAILABLE goes n and
        # every rust cfg drops out of the regenerated
        # include/generated/rustc_cfg; the rust objects then rebuild
        # broken (E0725 on rust/compiler_builtins.rs). Source both vars
        # from the core drv's env, which the core build exported.
        RUST_LIB_SRC = (core.env or { }).RUST_LIB_SRC or "";
        # Mirror build.nix's KRUSTFLAGS so the recomputed rustc command
        # stays byte-identical to the core build's recorded one (resume
        # fidelity); against coreDev, whose path is exactly what the
        # core's placeholder-dev expanded to.
        KRUSTFLAGS = lib.optionalString (
          (core.env or { }).RUST_LIB_SRC or "" != ""
        ) (
          "--remap-path-prefix ${(core.env or { }).RUST_LIB_SRC}=/"
          + lib.optionalString (lib.versionOlder version "7.0")
            " --remap-path-prefix=${core.dev}/lib/modules/${modDirVersion}/source/= --remap-path-scope=macro"
        );
      };

      buildPhase =
        let
          copyExtra = lib.concatStringsSep "\n" (
            map (d: ''
              if [ -d "$moduleSource/${d}" ]; then
                mkdir -p "modsrc/${d}"
                cp -a --reflink=auto "$moduleSource/${d}/." "modsrc/${d}/"
                chmod -R u+w "modsrc/${d}"
              else
                # Over-approximated include root the unpatched tree does not
                # have (e.g. a dir a patch creates): the unit is copyTree in
                # that case, so the scratch fallback never runs without it.
                echo "per-module: extraDir ${d} absent from unpatched source; skipping" >&2
              fi
            '') extraDirs
          );
          # The module's own Makefile flags like -I$(srctree)/x keep naming
          # srctree (the pruned dev tree) during the M= build; the
          # mapper-detected dirs are copied into modsrc, and these KCFLAGS
          # -I entries make bare-name quoted includes resolve against the
          # modsrc copies.
          extraIncludeFlags = lib.concatMapStringsSep " " (
            d: "-I$PWD/modsrc/${d}"
          ) extraDirs;
          # Writable private trees for copy-case units: reflink copies of
          # the prepared build (objtree) and source (srctree) trees, with
          # the build tree's outputmakefile shim repointed at the copy.
          stagePrivateTree = ''
            cp -a --reflink=auto "$coreDev/lib/modules/${modDirVersion}/build/." "$PWD/build/"
            cp -a --reflink=auto "$coreDev/lib/modules/${modDirVersion}/source/." "$PWD/source/"
            chmod -R u+w build source
            sed -i \
              -e "s|^export KBUILD_OUTPUT.*|export KBUILD_OUTPUT = $PWD/build|" \
              -e "s|^include .*|include $PWD/source/Makefile|" \
              "$PWD/build/Makefile"
            # the .cmd files record the core's absolute paths; without
            # NOCMDDEP every command line differs from the saved one and the
            # out-of-tree make regenerates the full prepare chain.
            export KBUILD_NOCMDDEP=1
            # The store normalizes every file mtime to one SOURCE_DATE_EPOCH;
            # with a uniform tree GNU make reruns the whole prepare chain.
            # cp -a preserves the core's natural artifact ordering, so the
            # ONLY stamp needed pushes SOURCES two days into the past:
            # artifacts keep their real build times (strictly newer) and
            # NOTHING is in the future - future timestamps trip make's
            # clock-skew logic and force a prepare0 regen that needs bc.
            find "$PWD/build" -type f \( \
              -name '*.c' -o -name '*.h' -o -name '*.S' -o -name '*.l' \
              -o -name '*.y' -o -name '*.sh' -o -name '*.py' \
              -o -name '*.tbl' -o -name '*.pl' \
              -o -name 'Kconfig*' -o -name 'Makefile*' -o -name 'Kbuild' \) \
              -exec touch -d "@$((SOURCE_DATE_EPOCH - 172800))" {} +
            find "$PWD/source" -type f \
              -exec touch -d "@$((SOURCE_DATE_EPOCH - 172800))" {} +
          '';
          applyUnitPatches = ''
            applyUnitPatches() {
              for p in $modulePatches; do
                patch --forward -d "$1" -p1 < "$p" || true
              done
            }
          '';
        in
        ''
          runHook preBuild

          coreBuild="$coreDev/lib/modules/${modDirVersion}/build"

          # stage this unit's applicable patch files (raw path
          # interpolation registers each as a derivation input; see the
          # nativeBuildInputs note)
          mkdir -p patches
          ${lib.concatImapStrings (
            i: nm: ''
              cp ${patchRaw.${nm}} "patches/${toString i}-${safeName nm}.patch"
            ''
          ) (m.patches or [ ])}
          ${applyUnitPatches}

          if [ -n "$isBuiltin" ]; then
            # BUILT-IN unit: obj-y object targets build in-tree (no M=),
            # which needs a writable objtree - always a private copy of the
            # prepared trees. srctree = the (patched) source copy, so angle
            # includes resolve canonically; no KCFLAGS tricks needed.
            # BUILT-IN unit: obj-y object targets build in-tree (no M=),
            # which needs a writable objtree - always a private copy of the
            # prepared trees. srctree = the (patched) source copy, so angle
            # includes resolve canonically; no KCFLAGS tricks needed.
            ${stagePrivateTree}
            applyUnitPatches "$PWD/source"
            # -j1 is REQUIRED here, not a slowdown trade-off: the silent
            # syncconfig (the store normalizes all core mtimes to one epoch,
            # so .config and auto.conf compare equal) rewrites
            # include/config/auto.conf.cmd, which make treats as a changed
            # included makefile and re-executes itself; under -jN the old
            # make's in-flight object job races the re-executed make's job on
            # the same .d file and fixdep intermittently fails with
            # "No such file or directory". Sequential passes cannot race.
            make -C "$PWD/build" --no-print-directory -j1 \
              ${quotedFlags} \
              $builtinTargets
            selfSrc="$PWD/build/${m.dir}"
          elif [ -n "$needsCopyTree" ]; then
            # COPY-CASE module: the patch set reaches outside the modsrc
            # coverage (or needs its own syncconfig after Kconfig patches) -
            # srctree = the unit's own patched copy of the source tree
            ${stagePrivateTree}
            applyUnitPatches "$PWD/source"
            make -C "$PWD/build" --no-print-directory -j1 \
              ${quotedFlags} \
              "M=$PWD/source/${m.dir}" \
              ${lib.escapeShellArg target}
            selfSrc="$PWD/source/${m.dir}"
          else
            # COMMON case: stage 1 for exactly this module: sources in the
            # canonical in-tree layout, output written next to them by
            # `make M=`
            mkdir -p "modsrc/${m.dir}"
            # contents form: the destination may already exist (module dirs
            # nested inside extraDirs); cp -a SRC DST would nest otherwise.
            # Store paths are read-only: chmod after every copy so later
            # copies can overwrite and the build can write its artifacts.
            cp -a --reflink=auto "$moduleSource/${m.dir}/." "modsrc/${m.dir}/"
            chmod -R u+w modsrc
            ${copyExtra}
            chmod -R u+w modsrc
            applyUnitPatches "$PWD/modsrc"
            chmod -R u+w modsrc

            # compile only: the object target stops Kbuild right after the
            # stage-1 build phase (objects + composite link + <mod>.mod).
            # No modpost, no .ko link, no signing - all of that happens in the
            # final aggregate drv.
            #
            # KCFLAGS=-I<pwd>/modsrc/a/b: tracepoint headers resolve relative
            # to the source root (include/trace/define_trace.h includes
            # ../../<module dir>/<trace>.h) and the pruned store source does
            # not contain the module dir; a two-level probe dir makes the -I
            # search land on the modsrc tree (X/../../ == modsrc).
            # ownDirIncludeFirst: the module's trace header lives in the
            # module dir itself; in-tree the define_trace re-include
            # resolves via -I$(src) (per-file CFLAGS_<f>.o, e.g.
            # mac802154's CFLAGS_trace.o). The own-dir -I must precede the
            # extraDir entries so a sibling module's same-named trace
            # header cannot shadow the module's own system.
            # parentDirInclude: the module uses angle includes with a ../
            # escape (mtip32xx: <../drivers/ata/ahci.h>); gcc resolves
            # <...> only through -I roots, and the a/b probe only serves
            # TWO-level escapes (a/b/../../ == modsrc). The one-level
            # probe is modsrc/a: a/.. == modsrc for ANY module depth
            # (a parent-of-dir probe fails at depth != 2:
            # modsrc/drivers/block/mtip32xx/../.. == modsrc/drivers).
            # NOTE: comments must sit BEFORE the make line - inside the
            # backslash-continued command a '#' would comment out every
            # remaining argument, including KCFLAGS, M= and the target.
            mkdir -p modsrc/a/b
            make -C "$coreBuild" -j"$NIX_BUILD_CORES" \
              ${quotedFlags} \
              "KCFLAGS=${lib.optionalString (m.ownDirIncludeFirst or false) "-I$PWD/modsrc/${m.dir} "}${lib.optionalString (m.parentDirInclude or false) "-I$PWD/modsrc/a "}-I$PWD/modsrc/a/b ${extraIncludeFlags}" \
              "M=$PWD/modsrc/${m.dir}" \
              ${lib.escapeShellArg target}
            selfSrc="$PWD/modsrc/${m.dir}"
          fi

          runHook postBuild
        '';

      installPhase = ''
        runHook preInstall

        # Ship the built dir's stage-1 artifacts plus its sources (sources
        # keep the layout byte-identical for consumers that resume the
        # build, e.g. the module-level tier). Stage-2 outputs must not
        # exist with the object-only target, but filter defensively.
        # selfSrc was set in buildPhase: the objtree dir for built-in
        # units, the source copy for copy-case modules, modsrc otherwise.
        mkdir -p "$out/self"
        cp -a --reflink=auto "$selfSrc/." "$out/self/"
        # NB: keep modules.order - Kbuild generates per-directory
        # modules.order files in-tree, so it is both correct for the
        # aggregate resume and required by the tier's MODORDER.
        find "$out/self" -type f \( \
          -name '*.mod.c' -o -name '*.mod.o' -o -name '*.ko' -o \
          -name 'Module.symvers' \
        \) -delete

        # namespaced accumulation of dependencies (design principle 3)
        mkdir -p "$out/deps"
        i=0
        for dep in $depsList; do
          ln -sfn "$dep" "$out/deps/$i"
          i=$((i+1))
        done

        # dep name -> store path, consumed by the module-level tier to
        # assemble KBUILD_EXTRA_SYMBOLS
        printf '%s\n' "$depManifest" > "$out/dep-manifest.txt"

        # machine-readable summary for consumers
        {
          echo "name=${m.name}"
          echo "dir=${m.dir}"
          echo "objects=${lib.concatStringsSep " " m.objects}"
          echo "extraDirs=${lib.concatStringsSep " " extraDirs}"
        } > "$out/manifest.txt"

        runHook postInstall
      '';

      # keep the map data reachable for debugging
      passthru = {
        inherit (m) dir objects;
        inherit core mapDrv;
      };

      meta = {
        description = "compile-only build of the in-tree kernel module ${m.name} (kernel ${version})";
        platforms = [ kernel.stdenv.hostPlatform.system ];
      };
    };

  # fixpoint-by-name: each module references its dependencies by name and
  # expects the same mapper to produce them; Nix dedups identical drvs.
  modules = lib.fix (
    self: builtins.listToAttrs (
      map (m: lib.nameValuePair m.name (mkCompileModule self m)) moduleMap.modules
    )
  );

  moduleList = lib.attrValues modules;

  moduleManifest = writeText "linux-module-manifest" (
    lib.concatStringsSep "\n" (
      map (m: "${m}	${m.passthru.dir}	${m.pname or ""}") moduleList
    )
  );

  aggregate = kernel.overrideAttrs (old: {
    outputs = [ "out" "dev" "modules" ];
    # vmlinux FIRST: the vmlinux-only modpost writes vmlinux.symvers, which
    # the global modules modpost consumes as Module.symvers (postBuild).
    # Every object the vmlinux link needs is seeded from per-unit drvs, so
    # this is a link, not a build - built-in.a (ar) and the final ld only.
    buildFlags = [ "vmlinux" ];
    installTargets = [ "modules_install" ];
    env = old.env or { } // {
      moduleManifest = "${moduleManifest}";
    };
    preBuild = (old.preBuild or "") + ''
      # Seed every compile unit's stage-1 artifacts at their canonical
      # objtree paths. The unit manifest is an input that lists every unit
      # drv (modules AND built-in dirs), so nix builds all of them before
      # this drv: the aggregate NEVER compiles a full tree - only the
      # documented build-tree-bound leftovers (kheaders and kin) plus the
      # modpost/link/sign steps.
      while IFS=$'\t' read -r modOut modDir modName; do
        [ -n "$modOut" ] || continue
        mkdir -p "$buildRoot/$modDir"
        cp -a "$modOut/self/." "$buildRoot/$modDir/"
      done < "$moduleManifest"

      chmod -R u+w "$buildRoot"

      export KBUILD_NOCMDDEP=1
    '';
    postBuild = (old.postBuild or "") + ''
      # vmlinux-only modpost wrote vmlinux.symvers; the global modules
      # modpost expects it as Module.symvers (the same rename the old core
      # used for its stash).
      if [ -f "$buildRoot/vmlinux.symvers" ] && [ ! -f "$buildRoot/Module.symvers" ]; then
        cp "$buildRoot/vmlinux.symvers" "$buildRoot/Module.symvers"
      fi
      make "''${makeFlags[@]}" -j"$NIX_BUILD_CORES" modules
    '';
    preInstall = (old.preInstall or "") + ''
      # the stock postInstall's clean drops Module.symvers (it is not in
      # the keepPaths list) - out-of-tree modules need it
      cp "$buildRoot/Module.symvers" "$NIX_BUILD_TOP/Module.symvers.stash" 2>/dev/null || true
    '';
    postInstall = (old.postInstall or "") + ''
      if [ -f "$NIX_BUILD_TOP/Module.symvers.stash" ]; then
        mkdir -p "$dev/lib/modules/${modDirVersion}/build"
        cp "$NIX_BUILD_TOP/Module.symvers.stash" "$dev/lib/modules/${modDirVersion}/build/Module.symvers"
      fi
    '';
  });

  # Final kernel image: reuses upstream's rebuild-image machinery
  # (insert-sys-cert with the aggregate's merged modules.cer).
  image = ((callPackage ../rebuild-image.nix { kernel = aggregate; }) {
    # the modules tree lives in the aggregate's modules output; the
    # default output (out) is empty (the aggregate only runs
    # modules_install)
    modulesTree = aggregate.modules;
  }).overrideAttrs (old: {
    # the inherited installFlags point INSTALL_PATH at the aggregate's
    # out output - a read-only store path in this drv's sandbox. Install
    # the rebuilt image directly (the make install target only copies
    # KBUILD_IMAGE and System.map to INSTALL_PATH).
    # The built image lives at arch/<srcArch>/boot/<target> in the
    # resumed build tree. On x86_64 the interpolated string is
    # byte-identical to the previous hardcode (arch/x86/boot/bzImage), so
    # x86 drv identities are unchanged; aarch64 builds (e.g. the x1e80100
    # 7.3 kernel) get arch/arm64/boot/Image.
    installPhase = ''
      runHook preInstall
      mkdir -p $out
      install -Dm644 arch/${srcArch}/boot/${kernel.target} $out/${kernel.target}
      install -Dm644 System.map $out/System.map
      runHook postInstall
    '';
  });

  # Module-level tier: per-module COMPLETE build (drop-in .ko + .ko.cer),
  # resuming the compile-only drv's stage-1 artifacts.
  completeModule =
    name:
    let
      info = builtins.head (builtins.filter (m: m.name == name) moduleMap.modules);
    in
    callPackage ./complete-module.nix {
      inherit kernel core kernelModuleMakeFlags;
      compileDrv = modules.${name};
      inherit info;
    };

  passthru = {
    inherit
      core
      modules
      moduleList
      aggregate
      image
      completeModule
      moduleMap
      mapDrv
      ;
    # unit count: =m modules AND built-in directories - every unit is a drv
    moduleCount = builtins.length moduleMap.modules;
    builtinCount = builtins.length (
      lib.filter (m: m.builtin or false) moduleMap.modules
    );
    copyTreeCount = builtins.length (
      lib.filter (m: m.copyTree or false) moduleMap.modules
    );
    # patch localization diagnostics: the prepare-relevant host patches the
    # prepare tree carries, and the mapper's full classification
    preparePatches = prepareHostPatches;
    patchInfo = moduleMap.patches or [ ];
  };
in
passthru
