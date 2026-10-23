# Per-module kernel packager (spike).
#
# Splits a nixpkgs kernel build into:
#
#   linux-core.drv         vmlinux + built-ins + modules_prepare/dev tree +
#                          base Module.symvers (vmlinux exports) + a `src`
#                          output with the full *patched* source tree
#   linux-module-<name>.d  COMPILE-ONLY per in-tree module: stage-1 Kbuild
#                          artifacts (.o, composite .o, .mod, .cmd files) in
#                          the module's canonical dir layout, plus namespaced
#                          accumulated outputs of its dependency modules
#   linux.drv (aggregate)  union of all module outputs seeded into a build
#                          tree, then resumed with `make modules
#                          modules_install`: modpost runs exactly once over
#                          everything, per-module ld / signing (kmodigest) /
#                          depmod / modules.cer merge all happen here
#   linux-image            the existing nixpkgs rebuild-image (insert-sys-cert
#                          with the merged modules.cer) over the aggregate
#
# Design notes and verification status: see NOTES.md in this directory.
{
  lib,
  stdenv,
  callPackage,
  writeText,
  kernel,
  kernelModuleMakeFlags ? kernel.commonMakeFlags ++ [
    "DEPMOD=true"
    "INSTALL_MOD_STRIP=1"
  ],
  # Optional subset for incremental/smoke builds: only these modules get
  # per-module drvs. The aggregate remains functionally complete - its
  # resumed `make modules` compiles whatever stage-1 artifacts are missing.
  onlyModules ? null,
  # Escape hatch: extra source dirs some modules need beyond their own
  # (mapper cannot see all header-dir relationships). attrset name -> [dir].
  extraSourceDirs ? { },
}:

let
  inherit (kernel) modDirVersion version;

  mapDrv = callPackage ./mapper.nix { inherit kernel; };
  moduleMap = builtins.fromJSON (builtins.readFile "${mapDrv}/modules.json");

  selected =
    if onlyModules == null then moduleMap.modules
    else builtins.filter (m: lib.elem m.name onlyModules) moduleMap.modules;

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

  core = kernel.overrideAttrs (old: {
    outputs = [ "out" "dev" "source" ];
    buildFlags =
      [ kernel.target "scripts_gdb" ]
      ++ lib.optionals kernel.buildDTBs [ "dtbs" ];
    postBuild = ''
      # kernel 6.18+ generates scripts/module.lds only under
      # modules_prepare (not run by the core-only build); downstream
      # modpost/modfinal need it in the retained build tree. All prepare
      # steps are up to date here, so this only generates the lds.
      make "''${makeFlags[@]}" modules_prepare
    '';
    installTargets = [ (kernel.installTargetFor kernel.target) ];
    preInstall = (old.preInstall or "") + ''
      # Stash files that this kernel's `make clean` (run by postInstall)
      # removes but downstream modpost/modfinal need:
      #   scripts/module.lds - prerequisite of the %.ko rule; when missing,
      #     make rejects the pattern rule with "No rule to make target
      #     '<module>.ko'"
      #   Module.symvers     - the core's vmlinux exports
      cp "$buildRoot/scripts/module.lds" "$NIX_BUILD_TOP/module.lds.stash" 2>/dev/null || true
      # without KBUILD_MODULES the vmlinux-only modpost writes
      # vmlinux.symvers; downstream expects Module.symvers
      if [ -f "$buildRoot/Module.symvers" ]; then
        cp "$buildRoot/Module.symvers" "$NIX_BUILD_TOP/Module.symvers.stash"
      elif [ -f "$buildRoot/vmlinux.symvers" ]; then
        cp "$buildRoot/vmlinux.symvers" "$NIX_BUILD_TOP/Module.symvers.stash"
      fi
      # Snapshot the full patched source before postInstall prunes the dev
      # tree (drivers are deleted there). Per-module compile drvs compile
      # against this tree, so they are consistent with the core by
      # construction and never need to re-apply kernel patches.
      # NB: the output is named "source", not "src" - an output named src
      # would shadow the src attribute (env var) and break unpacking.
      # NB2: use the absolute path; after unpack genericBuild has cd'd into
      # sourceRoot, so $PWD is already .../source.
      mkdir -p "${placeholder "source"}"
      cp -a --reflink=auto "$dev/lib/modules/${modDirVersion}/source/." "${placeholder "source"}/"
    '';
    postInstall = (old.postInstall or "") + ''
      # Restore the stashed module-linking interface files into the
      # retained build tree.
      if [ -f "$NIX_BUILD_TOP/module.lds.stash" ]; then
        mkdir -p "$dev/lib/modules/${modDirVersion}/build/scripts"
        cp "$NIX_BUILD_TOP/module.lds.stash" "$dev/lib/modules/${modDirVersion}/build/scripts/module.lds"
      fi
      if [ -f "$NIX_BUILD_TOP/Module.symvers.stash" ]; then
        cp "$NIX_BUILD_TOP/Module.symvers.stash" "$dev/lib/modules/${modDirVersion}/build/Module.symvers"
      fi
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
      target = "${m.name}.o";
    in
    stdenv.mkDerivation {
      pname = "linux-module-${m.name}";
      inherit version;

      # No src unpack: sources come from the core's patched source output.
      dontUnpack = true;

      outputs = [ "out" ];

      nativeBuildInputs = kernel.moduleBuildDependencies;

      # Dependency modules as sandbox inputs. Their outputs are only needed
      # by consumers (the module-level tier reads accumulated dep objects);
      # the compile itself needs no dep artifacts (principle 4: no linking,
      # no symbol resolution below the final drv).
      env = {
        coreDev = "${core.dev}";
        coreSource = "${core.source}";
        depsList = lib.concatMapStringsSep " " (p: "${p.drv}") presentDeps;
        depManifest = lib.concatStringsSep "\n" (
          map (p: "${p.name}\t${p.drv}") presentDeps
        );
      };

      buildPhase =
        let
          copyExtra = lib.concatStringsSep "\n" (
            map (d: ''
              mkdir -p "modsrc/${d}"
              cp -a --reflink=auto "$coreSource/${d}/." "modsrc/${d}/"
              chmod -R u+w "modsrc/${d}"
            '') extraDirs
          );
        in
        ''
          runHook preBuild

          coreBuild="$coreDev/lib/modules/${modDirVersion}/build"

          # stage 1 for exactly this module: sources in the canonical
          # in-tree layout, output written next to them by `make M=`
          mkdir -p "modsrc/${m.dir}"
          # contents form: the destination may already exist (module dirs
          # nested inside extraDirs); cp -a SRC DST would nest otherwise.
          # Store paths are read-only: chmod after every copy so later
          # copies can overwrite and the build can write its artifacts.
          cp -a --reflink=auto "$coreSource/${m.dir}/." "modsrc/${m.dir}/"
          chmod -R u+w modsrc
          ${copyExtra}
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
          mkdir -p modsrc/a/b
          make -C "$coreBuild" -j"$NIX_BUILD_CORES" \
            ${quotedFlags} \
            "KCFLAGS=-I$PWD/modsrc/a/b" \
            "M=$PWD/modsrc/${m.dir}" \
            ${lib.escapeShellArg target}

          runHook postBuild
        '';

      installPhase = ''
        runHook preInstall

        # Ship the module dir's stage-1 artifacts plus its sources (sources
        # keep the layout byte-identical for consumers that resume the
        # build, e.g. the module-level tier). Stage-2 outputs must not
        # exist with the object-only target, but filter defensively.
        mkdir -p "$out/self"
        cp -a --reflink=auto "modsrc/${m.dir}/." "$out/self/"
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
      map (m: lib.nameValuePair m.name (mkCompileModule self m)) selected
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
    buildFlags = [ "modules" ];
    installTargets = [ "modules_install" ];
    env = old.env or { } // {
      coreDev = "${core.dev}";
      moduleManifest = "${moduleManifest}";
    };
    preBuild = (old.preBuild or "") + ''
      # Seed the build tree with the core's prepared tree (vmlinux,
      # Module.symvers, scripts, generated headers) and every module's
      # stage-1 artifacts at their canonical in-tree paths. Kbuild then
      # finds all objects up to date and runs straight through modpost (once,
      # globally), the per-module links, modinst signing and depmod.
      #
      # KBUILD_NOCMDDEP=1 drops the saved-command-line comparison; under
      # Nix epoch timestamps nothing is "newer than" anything else, so the
      # remaining rebuild triggers are only missing artifacts - which make
      # then builds here (correct, just not per-module cached).
      cp -a "$coreDev/lib/modules/${modDirVersion}/build/." "$buildRoot/"

      while IFS=$'\t' read -r modOut modDir modName; do
        [ -n "$modOut" ] || continue
        mkdir -p "$buildRoot/$modDir"
        cp -a "$modOut/self/." "$buildRoot/$modDir/"
      done < "$moduleManifest"

      # store paths are read-only; the resumed build must be able to
      # write .cmd files, generated artifacts, etc. next to the seeds
      chmod -R u+w "$buildRoot"

      export KBUILD_NOCMDDEP=1
    '';
  });

  # Final kernel image: reuses upstream's rebuild-image machinery
  # (insert-sys-cert with the aggregate's merged modules.cer).
  image = (callPackage ../rebuild-image.nix { kernel = aggregate; }) {
    modulesTree = aggregate;
  };

  # Module-level tier: per-module COMPLETE build (drop-in .ko + .ko.cer),
  # resuming the compile-only drv's stage-1 artifacts.
  completeModule =
    name:
    let
      info = builtins.head (builtins.filter (m: m.name == name) selected);
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
    moduleCount = builtins.length moduleMap.modules;
  };
in
passthru
