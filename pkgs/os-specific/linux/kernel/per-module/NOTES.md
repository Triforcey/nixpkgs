# Per-module kernel packager — spike notes

Branch: `per-module-kernel-packager` (off upstream/master `60e4bb641c9d`), commit-local only, not pushed, not PR'd.

## Goal

A kernel packager where every in-tree module is its own derivation, for
per-module caching and patching:

```
linux-core.drv:          vmlinux + built-ins + modules_prepare/dev tree +
                         base Module.symvers (vmlinux exports) + full patched source
linux-module-<name>.drv: COMPILE-ONLY — sources + prepared tree → own stage-1
                         artifacts (.o / composite .o / <mod>.mod / .cmd files)
linux.drv (aggregate):   union of accumulated outputs → modpost ONCE over
                         everything → per-module ld → kmodigest signing →
                         depmod → modules.cer merge (all upstream machinery)
linux-image:             existing nixpkgs rebuild-image (insert-sys-cert with
                         the merged modules.cer) over the aggregate
```

Plus the module-level tier (`completeModule`): per-module *complete* builds
(drop-in `.ko` + `.ko.cer` like the out-of-tree pattern) that resume the
compile-only drv's artifacts instead of recompiling.

## Files

- `mapper.py` — eval-time source analysis. Walks Kbuild from the root
  (`Makefile` + root `Kbuild` + every dir reachable via dir tokens in
  `obj-*` and generic `*-y` vars, including `core-y`/`drivers-y`/`libs-y`
  wired by the arch Makefile), expands `obj-$(CONFIG_FOO)` against the
  shipped `.config`, collects the =m module set with object lists, and
  derives module→module edges from a conservative EXPORT_SYMBOL scan,
  pruned to a DAG at map time (default.nix interpolates dep store paths
  into the compile drvs' env; a cyclic graph recurses forever at eval).
- `mapper.nix` — the map derivation (IFD'd at eval time).
- `default.nix` — core / module fixpoint / aggregate / image wiring.
- `complete-module.nix` — module-level complete-build tier.

## Key design decisions (as agreed) and how they landed

1. **Mapper derives edges from source, conservative.** Over-approximation is
   safe (spurious rebuild edge); under-approximation is the dangerous mode.
   Unresolvable conditionals collect BOTH branches; unresolvable variables in
   `obj-` LHS contribute tokens to both `obj-y` and `obj-m`; absent CONFIG
   symbols expand to the empty string exactly like auto.conf does (this is
   Kbuild's real behaviour, not an approximation).
2. **Fixpoint-by-name.** `modules = builtins.fix (self: listToAttrs …)`; each
   module references deps by name; Nix dedups identical drvs.
3. **Recursive accumulation is load-bearing.** Each module drv's output is
   `self/` (own stage-1 artifacts + sources, in the module's canonical dir
   layout) plus `deps/<n>` symlinks to its deps' full outputs
   (namespaced ⇒ union conflict-free) plus `dep-manifest.txt`
   (`name<TAB>storepath`). The module-level tier needs only ONE input (the
   compile drv) to see all transitive dep artifacts.
4. **No linking below the final drv.** The module drv runs the Kbuild
   *single-object target* (`make M=<dir> <module>.o`), which executes only
   the stage-1 build phase: compile + composite `ld -r` + `<mod>.mod`
   generation. No modpost, no `.ko` link, no signing. Validated locally
   (see below). The final aggregate is the only place where modpost runs
   (once, globally, via `make modules` over the seeded tree), where
   kmodigest signs (modinst `cmd_sign`), where depmod runs and where
   `modules.cer` is merged (upstream `build.nix` postInstall, untouched).
5. **Signing rule.** Compile-only drvs do not sign. The final drv signs via
   the existing `cmd_sign=kmodigest$(space)$@` make-flag machinery during
   `modules_install`. The tier signs its own drop-in .ko the same way.

## How the final aggregate resume works

The aggregate drv is `kernel.overrideAttrs` whose `preBuild` seeds
`$buildRoot` with:

1. the core drv's prepared build tree (vmlinux, `Module.symvers`, scripts
   incl. `modpost`/`fixdep`, `module.lds`, `include/config/auto.conf`,
   generated headers — everything nixpkgs' `kernel.dev` retains), and
2. every module drv's `self/` at its canonical in-tree path.

Then `make modules modules_install` runs with `KBUILD_NOCMDDEP=1`.
Grounded in kernel 7.3's `scripts/Kbuild.include`:

```
cmd-check = $(if $(strip $(savedcmd_$@)),,1)   # NOCMDDEP: only "missing .cmd" triggers
if-changed-cond = $(newer-prereqs)$(cmd-check)$(check-FORCE)
```

Under Nix epoch timestamps `newer-prereqs` is empty, so the remaining
rebuild triggers are *missing artifacts only* — make then builds them in
the aggregate (correct, just not per-module cached). This makes the
aggregate degrade gracefully: even if per-module drvs are missing from the
store, the resumed `make modules` compiles the gaps, so the result is
always a complete, correct kernel.

The kernel image is built by the untouched upstream `rebuild-image.nix`
(`insert-sys-cert -b vmlinux -c modules.cer`, `--assume-old=vmlinux`).

## The module-level tier

Resumes the compile drv: recreates the exact sandbox layout
(`/build/modsrc/<dir>` + the same `make -C` invocation) so recorded command
lines match and nothing recompiles, generates `KBUILD_EXTRA_SYMBOLS` by
running the core's modpost over dep composite objects, then runs
`make M=<dir> modules_install` (modpost for this module + modfinal link +
modinst sign/compress/install). Honesty note: with `CONFIG_MODVERSIONS`
unset the extra symvers entries carry names but no CRCs; modules with
mapper-missed deps still build (modpost warns; undefined symbols resolve at
insmod; the authoritative symvers is the final drv's).

## Verified so far

- **Mapper vs ground truth**: against kernel 7.3 arm64 + the NixOS config
  whose real build produced a 7920-line `modules.order`, the mapper finds
  7915 unique module names — 6 missing, 1 extra:
  - 2 missing (`ch7006`, `sil164`) — source drift: the local 7.3 tarball
    no longer contains `drivers/gpu/drm/i2c`; the anchor was built from a
    slightly different 7.3 source.
  - 4 missing (`iommu_{amdv1,riscv64,vtdss,x86_64}`) — these modules are
    generated at make time via `$(eval $(foreach …))` in
    `drivers/iommu/generic_pt/fmt/Makefile`; a source-level walker cannot
    see them. The final drv still builds them (resume semantics).
  - 1 extra (`sparx5_serdes`) — same drift class, over-approximation = safe.
  - Mapper runtime: ~2.6 s CPU for the whole tree (this is why the map is a
    derivation rather than pure-Nix Makefile parsing; IFD documents itself).
- **Mapper through Nix (kernel 6.18.58, x86_64, nixos default config)**:
  the map derivation builds on nixbuild in ~90 s (dominated by source
  unpack; the scan itself is ~3 s CPU) and yields **7290 modules**
  (`stats` in `modules.json`), arch `x86_64`, with 822 warnings recorded.
- **Dependency-edge quality**: the EXPORT_SYMBOL scan initially produced
  module-wide spurious edges from name-colliding exports (e.g. `ib_core`
  exports a symbol literally named `func` — a macro artifact — and every
  module containing the identifier `func` then depended on `ib_core`).
  A noise filter drops symbols referenced by more than
  `max(10, n_modules/50)` modules and reports them as `noisySymbols`
  (10 on 6.18.58, 8 on 7.3). After filtering: `9p → [9pnet, netfs]`,
  `9pnet → [netfs]`, `mac80211 → [cfg80211, libarc4]` — real chains.
- **Eval wiring**: `nix-instantiate --eval` of the packager succeeds through
  the IFD chain (linux-config → map → 7290-entry fixpoint attrset
  creation). Practical IFD note: `nix-instantiate --eval` cannot
  instantiate a *new* map `.drv` mid-eval (`path … is not valid`); build
  the map drv explicitly first (`nix-build map-drv.nix`) and later evals
  resolve from the store.
- **Stage-1 mechanics** (local, kernel 7.3 arm64 dev tree): the
  compile-only invocation
  `make -C <kernel.dev build tree> M=<writable modsrc> KCFLAGS=-I<pwd>/modsrc/a/b msm.o`
  produced the full stage-1 artifact set (`LD [M] msm.o` composite 58 MB,
  `msm.mod`, per-object `.o` + `.*.cmd` files) with **no** stage-2
  artifacts (no `.mod.c`, no `.ko`) and **no writes to the read-only store
  objtree**. Two source-layout traps were discovered and solved here:
  - sibling headers included relatively (`../../../drm_crtc_internal.h`)
    → `extraSourceDirs` copies needed dirs at canonical paths under
    `modsrc/`;
  - tracepoint headers resolve relative to the source root
    (`include/trace/define_trace.h` → `../../<module dir>/…`) and the
    pruned `kernel.dev` source lacks them → the two-level probe dir
    `modsrc/a/b` makes `-I$PWD/modsrc/a/b` resolve them (`X/../../ ==
    modsrc`).
- **Derivation instantiation**: the core drv, 5 subset compile-only drvs
  (`onlyModules = [ 9p 9pnet netfs nls_cp437 8250_pci ]` — flag since
  removed, see finding 11), 2 module-level tiers, the aggregate and the
  image all instantiate.
- Two derivation-construction bugs were caught exactly where the design
  says they would be: (a) naming the core's source-snapshot output `src`
  shadows the `src` attribute (mkDerivation exposes outputs as env vars;
  the build tried to "unpack" the empty output) — renamed to `source`;
  (b) `rebuild-image.nix` is curried (`{ lib, kernel }` → `{ target,
  modulesTree }`) and must be called with its own argset.
- `nix-instantiate --parse` passes for all new expressions.

## Kernel-6.18-specific findings (smoke-test round trip)

The smoke test against `pkgs.linux` (6.18.58, x86_64) surfaced a series of
real integration bugs, each fixed with a commit:

1. **Mapper source-dir detection**: after unpack, the tree root contains
   `Documentation/` (which ships a Makefile); requiring both `Makefile`
   and `Kbuild` fixed an empty map. (`ef2e0527`)
2. **`lib.fix`**: `builtins.fix` does not exist. (`a1100ad`)
3. **Output named `src` shadows the `src` attribute** (mkDerivation exposes
   outputs as env vars) - renamed to `source`. (`0e57662`)
4. **`rebuild-image.nix` is curried**; `callPackage` must be given its own
   argset. (`f41d6dc`)
5. **postInstall cwd**: genericBuild cd's into sourceRoot before install,
   so the source snapshot must use absolute paths. (`b199244`)
6. **Store paths are read-only**: every copied tree needs `chmod -R u+w`,
   and copies that can overwrite earlier ones need it between copies.
   (`f153931`, `bc3ed4b`)
7. **Relative includes escaping the module dir** (8250 modules including
   `../serial_mctrl_gpio.h`): the mapper walks include chains
   breadth-first over `.c/.h/.S/.rs` files and emits per-module
   `extraDirs`; only includes whose target file exists in the source tree
   are recorded (quoted `asm/...` includes resolved via -I would produce
   phantom directories). Copies use the `SRC/.` contents form because
   module dirs live *inside* their extra dirs. (`5f3256d`, `14ca492`,
   `5ce25c2`)
8. **Tier**: Kbuild's native single-module full build is
   `make M=<dir> <module>.ko` (the `single_modules` flow writes a
   one-entry `modules.order`, runs modpost for exactly that module and
   modfinal); install then points modinst's MODORDER at a one-entry list.
   `KBUILD_MODPOST_WARN=1` is honest for drop-in modules (unresolved
   symbols resolve at insmod). (`90b6c99`, `c163019`)
9. **Core must retain the module-linking interface**: kernel 6.18
   generates `scripts/module.lds` only under `modules_prepare` (not run
   by the core-only build) and the vmlinux-only modpost writes
   `vmlinux.symvers` instead of `Module.symvers`. Without `module.lds`,
   make rejects the `%.ko` pattern rule and fails with
   `No rule to make target '<module>.ko'` - a wonderful error message for
   a missing linker script. The core now builds `scripts/module.lds`
   explicitly and stashes/restores both files. (`340db0d`, `ded7283`)
10. **Full-mode fixpoint recursion**: removing the subset and forcing every
   compile drv (the aggregate's `moduleManifest` input) hit
   `error: infinite recursion` at `env.depManifest` - the full dep graph
   is not a DAG: 31 name-level self-edges (duplicate registrations of one
   module name) plus 13 real SCCs (e.g. `nfsv3` ↔ `nfsv4`, `usbhid` ↔ `hid`)
   in the 7290-module 6.18 map. The 5-module smoke subset was acyclic by
   luck. Fix: `Mapper.prune_cycles` projects the edges onto module NAMES
   (the fixpoint's actual key granularity), drops name self-edges, and
   breaks SCCs deterministically by registration order. Dropped edges
   only narrow the tier's `KBUILD_EXTRA_SYMBOLS` (already best-effort);
   the aggregate and image are unaffected.
11. **Subset mode removed**: `onlyModules` saved nothing in any cache state
   (the aggregate recompiles uncovered modules internally), and the
   subset's module manifest *changed the aggregate's drv identity*,
   fragmenting the one drv that matters for cache reuse. Full mode is the
   only mode; cheap smoke testing is done by building individual module
   drvs / the tier, not by a subset flag.
12. **aarch64**: the image drv's installPhase hardcoded
   `arch/x86/boot/bzImage`; parameterized to
   `arch/${srcArch}/boot/${kernel.target}`, where srcArch maps the make
   ARCH value (hostPlatform.linuxArch: "x86_64", "arm64") to the
   build-tree directory form ("x86", "arm64") - the same SRCARCH subst
   the kernel's top Makefile applies. On x86_64 the interpolated string
   is byte-identical (drv identities unchanged); aarch64 builds
   (e.g. the x1e80100 7.3 kernel) get `arch/arm64/boot/Image`.
   insert-sys-cert is arch-independent (it writes the
   `system_extra_cert` / `system_certificate_list_size` ELF symbols from
   `certs/common.c`, built on every arch), and rebuild-image's only
   x86-ism (`--assume-old=vmlinux.unstripped`) is already isx86-gated.
13. **aarch64 consumer surface of the assembled kernel** (fertile-forge
   build failure): NixOS out-of-tree builders and the deviceTree module
   consume attrs off `config.boot.kernelPackages.kernel` that the glue's
   assembled runCommand must forward explicitly:
   - `msm-kernel-module` does `inherit (kernel) src version postPatch
     nativeBuildInputs` — mkDerivation exposes raw drv-arg attrs on the
     stock kernel drv; runCommand does not. The glue forwards
     `postPatch`/`nativeBuildInputs` through its passthru (version/src
     were already covered by the baseKernel.passthru spread).
   - `hardware.deviceTree.dtbSource` defaults to
     `${kernelPackage}/dtbs` with kernelPackage = the assembled kernel;
     the stock kernel satisfies it via `dtbs_install` into `$out/dtbs`.
     The core's `installTargets` had dropped `dtbs_install` along with
     the intentional `modules_install` drop — restored under
     `lib.optionals kernel.buildDTBs` (x86: buildDTBs false → identical
     installTargets → byte-identical core identity), and the assembled
     drv symlinks the core's dtbs output into `$out/dtbs`.
   - The dtbo overlay include path
     (`${getDev kernelPackage}/lib/modules/<ver>/source/scripts/dtc/include-prefixes`)
     resolves against the core's dev tree — verified present in the
     built 6.18.59 core dev output.
   - `system.modulesTree` uses `(lib.getOutput "modules" kernel)`, which
     falls back to `kernel` itself when the drv has no `modules` output
     — the assembled runCommand needs no `modules` output.
   - The stock kernel's `vmlinux` (msm copies `$kernel_dev/vmlinux`) and
     `Module.symvers` are present at the core dev root / build tree —
     verified in the built core.
   - `config.system.boot.loader.kernelFile` ("Image") equals
     `kernel.target` on arm64, so the assembled `$out/${target}` symlink
     satisfies the system-builder kernel image check.

## Smoke chain verification (kernel 6.18.58, x86_64, nixbuild.net)

The chain ran end-to-end against `pkgs.linux` (6.18.58) with the subset
`onlyModules = [ 9p 9pnet netfs nls_cp437 8250_pci ]`:

1. **Core drv** built (~3.5 min build phase on nixbuild big-parallel;
   ~10 min wall including install/fixup and the 2.2 GB copy-back). The dev
   build tree retains the full module-linking interface (modpost, fixdep,
   module.lds, Module.symvers) and the `source` output carries the full
   patched tree.
2. **Compile-only module drvs** (5/5 built, ~1-2 min each): stage-1
   artifacts only - composite `.o`, constituents, `.mod`, `.*.cmd` - with
   zero stage-2 outputs (no `.ko`, no `.mod.c`) and namespaced dep
   accumulation.
3. **Module-level tiers** (2/2 built, ~1-2 min each): the full modinst
   flow ran - STRIP, kmodigest SIGN, XZ - producing drop-in artifacts
   `updates/<name>.ko.xz` + `<name>.ko.cer` (260-byte kmodigest cert,
   ready for `aggregateModules` cert merging).
4. **Aggregate drv** (~35-40 min: built-ins rebuild + resumed compile of
   the ~7285 non-subset modules + global modpost + per-module ld +
   modinst + cert merge + depmod): the modules output contains 7294
   compressed modules, `modules.order` (7294 lines), depmod maps
   (`modules.dep`, `modules.alias`), and `modules.cer` = 1 896 440 bytes
   (all per-module certs merged, per-module files removed - the upstream
   invariant).
5. **Image drv** (~4 min): `scripts/insert-sys-cert` logged
   `Inserted the contents of .../modules.cer into ffffffff83b1dcc8` - the
   merged cert bundle embedded into the stamped vmlinux, bzImage relinked
   (14.6 MB) and System.map installed.

### Aggregate resume efficiency finding

The aggregate's resumed build recompiled *everything*, not just the
non-subset modules. Two causes, both fixable (deferred - correctness is
unaffected; the result is a complete, correct kernel):

1. Per-dir `built-in.a` archives (and their `.cmd` files) are pruned from
   the core dev build tree, and 6.18's `make modules` descends into all
   dirs with `need-builtin=1`, so missing archives force a built-in
   rebuild. Fix: preserve per-dir `built-in.a` + `.cmd` in the core dev
   tree (same stash/restore pattern as module.lds).
2. The aggregate's `configurePhase` runs `make oldconfig`, which
   regenerates `include/config/auto.conf` and the generated headers with
   *current* timestamps - newer than the epoch-timestamped stage-1
   artifacts, so `newer-prereqs` triggers a full rebuild even under
   `KBUILD_NOCMDDEP=1`. Fix: seed the build tree before configurePhase,
   or refresh artifact mtimes after configuration.

With both fixed, the resumed aggregate should compile only the missing
modules - making a partial cache state a true incremental build.

## Known deviations from the agreed design

- The module map is produced by a cheap derivation (Python) instead of
  pure-Nix eval, then IFD'd. Rationale: ~8000 modules over thousands of
  Makefiles; 2.6 s in Python vs minutes of Nix eval. The *edges still come
  from the source at mapping time*; only the execution vehicle differs.
- The compile-only drvs no longer consume the core drv's patched source
  output. Option A (below) replaced that design: module drvs seed the
  unpatched upstream source and apply their own applicable patch subset,
  so patch edits do not re-hash the whole tier.

## Option A: patch localization (kernel 7.3 / aarch64 round) — SUPERSEDED by v2 below

The original Option A kept a "structural core": the stock kernel
re-derived with the structural patch subset and a FULL build (vmlinux,
built-ins, dev tree). The operator's absolute rule — no full tree build,
ever — makes that design void. What survives of Option A is the per-unit
patch-set computation; what changed is everything about where those
patches get applied and who compiles what.

Before A, host patches were baked into the core drv's patched source and
every module drv depended on that output, so ANY patch edit re-hashed all
~7941 module drvs plus core plus aggregate. The mapper now classifies
each host patch and computes per-module applicability:

- **structural** — feeds the core build: touches anything outside =m
  module dirs (include/, scripts/, devicetree bindings, defconfig), a
  build file anywhere, or the source of a built-in (obj-y) object inside
  a module dir. The glue re-derives the core from the stock
  kernelPackages with ONLY structural host patches (randstructSeed and
  features threaded through to match the glue's baseKernel; the stock
  kernel's own patches re-apply via the override merge and are excluded
  from the structural list to avoid double application).
- **leaf** — touches only =m module-dir sources. Applied inside each
  affected module's compile drv against the UNPATCHED upstream source
  (a cheap `unpackedSource` drv whose identity is the source tarball
  only). A leaf patch edit now re-hashes core not at all and only the
  modules whose dir/closure the patch touches.

Applicability is file/dir based: a module gets a patch when a touched
path is one of its sources, sits in its transitive include closure, lies
under its dir, or lies in one of its extraDirs. Creator dependencies
(patch B modifies a file patch A creates) are resolved into the same set
transitively, in patch-list order. Patches are staged into the sandbox
by the buildPhase (raw path interpolation registers them as inputs;
they must NOT go into nativeBuildInputs — stdenv sources single-file
inputs as setup hooks, executing the patch text). Hunks targeting paths
outside modsrc (include/, scripts/) fail harmlessly: those changes reach
the module through the core dev tree, which carries the structural
patches. The authoritative Module.symvers stays the aggregate's global
modpost.

The aggregate still derives from the fully patched kernel (its own
sandbox applies all patches), so leftover compiles of patched-in modules
are unchanged. Patch files are copied into `patches/` and applied with
`patch --forward -p1 -d modsrc`; the staged filenames and the manifest
are 1-based and must stay in sync (concatImapStrings is 1-based — this
bit us once).

Classification for the x1e80100 7.3 patchset: 10 classified patches, 2
structural (rpmh devicetree binding = outside module dirs; rpmh
regulator driver = built-in object source), 8 leaf, 434 modules carry a
non-empty set (the shared-dir rule over-approximates: every module in
drivers/regulator gets the rpmh patch — safe, just coarser).

## Known limitations

## Patch localization v2: zero full-tree-build architecture (2026-11-05)

Operator's absolute rule: **no full tree build, ever.** Everything that
compiles happens in per-unit derivations; the final drv only links.

Flow (as agreed):
1. a source-only drv (`linux-<ver>-patched-source`) applies the stock
   kernel's own patches plus EVERY host patch; the mapper walks THAT tree,
   so patch-created modules/files/Kconfig entries are visible to the map.
   It is never an input to any compile drv.
2. the map computes, per unit (=m module AND built-in obj-y directory —
   built-ins are first-class units, registered as `builtin@<dir>`): dir,
   objects, include closure, dep edges, and the unit's own patch subset
   (creator dependencies resolved transitively; ancestor-dir Kconfig
   patches reach every unit under them).
3. every unit drv applies only its subset; the aggregate links.

Classification (mapper `analyze_patches` v2):
- **prepare-relevant** — ALSO applied in the shared prepare tree: root
  Makefile/Kbuild, everything under scripts/, and the transitive include
  closure of the prepare-time generated-header entry files
  (`arch/<arch>/kernel/asm-offsets.c`, `kernel/bounds.c`). These feed
every compilation by construction, so a change there legitimately
  re-hashes all units (that is semantically correct, not a full tree
  build - each unit rebuilds its own objects).
- **localized** — everything else. Kconfig patches are NOT
  prepare-relevant: the prepare tree's config deliberately drops symbols
  whose Kconfig it does not carry, and affected units re-run syncconfig
  inside their own copy (`copyTree`).
- `copyTree` marks units whose subset cannot land in the cheap modsrc
  scratch: a non-prepare patch touching paths outside the unit's
  dir/extraDirs (kbuild's include search order makes a modsrc overlay
  lose to srctree for angle includes), any Kconfig patch in the set, or
  a unit whose dir/build files the patches create. copyTree units
  reflink-copy the prepared build+source trees and apply their set there
  (srctree = the copy). Built-in units ALWAYS run in a copy (in-tree
  object targets need a writable objtree).
- built-in units are tier-inert: they carry no dep edges (their keys
  contain "@" and resolve at eval, so treating them like inert
  `name@dir` twins would leave builtin<->module cycles alive — this bit
  the first map build; the fix strips builtin deps before the SCC walk).

Core (prepare-only): the stock kernel re-derived with ONLY the
prepare-relevant host patches, reduced to `modules_prepare` (+ `dtbs` on
aarch64 — DTS compiles, not kernel objects) via overrideAttrs. Outputs:
`dev` = retained build tree (scripts, generated headers, module.lds),
`source` = full patched source snapshot, `out` = dtbs (aarch64) / marker.
No vmlinux, no Module.symvers, no kernel objects.

Aggregate: derives from the FULLY patched kernel (its own sandbox applies
every patch and generates the complete config — this is why units whose
Kconfig patches were not carried by the prepare tree still resolve). The
unit manifest is an INPUT listing every unit drv, so nix builds all
units before the aggregate starts. preBuild seeds all stage-1 artifacts
at canonical objtree paths; buildFlags = `[vmlinux]` — the built-in.a
files are ar'ed from the seeded .o files and vmlinux is LINKED, not
built. postBuild renames vmlinux.symvers → Module.symvers and runs
`make modules` (the one global modpost over the seeded artifacts, then
per-module ld / kmodigest / depmod via modules_install). pre/postInstall
stash Module.symvers across the stock postInstall clean (not in its
keepPaths) so the aggregate's `dev` is the retained real-kernel build
tree (vmlinux + Module.symvers + full source) that out-of-tree modules
(msm & co.) build against — the glue's assembled `dev` now points there.

Verified on the stock 6.18.59 x86 config (fresh store, native):
map builds green with 7548 units (259 built-ins, 0 copyTree, 0 patches),
name-level Tarjan SCC check 0 multi-node SCCs, and the full eval surface
(moduleCount / core / sample unit / aggregate) constructs clean through
the fixpoint. Green reference: map
`lj1r5j4n1x70zzpjyr41vn3kwcv6sj3n-linux-module-map-6.18.59`, core
`fd25gfmbdqpybdwgphjkyv06i1caa059-linux-6.18.59`, aggregate
`qh2nahiq4ar3l65bd0hkw8hkshv31q6v-linux-6.18.59`.

## Unit sandbox kconfig re-probes: rust env (fixed 2026-11-06)

Every unit's silent syncconfig re-evaluates the Kconfig probes in the UNIT
sandbox. `scripts/rust_is_available.sh` needs `RUSTC`/`BINDGEN`/`CC` in
the exported env (kernel Makefile tool defaults + the unit's quotedFlags
cover these) AND the core stdlib source via `RUST_LIB_SRC` - the nixpkgs
rustc sysroot fallback path does not exist, and only the core build had
the env (build.nix). Probe failure means `RUST_IS_AVAILABLE=n`, so
CONFIG_RUST (plus BLK_DEV_RUST_NULL, CPUFREQ_DT_RUST,
DRM_PANIC_*_QR_CODE) drop out of the regenerated
`include/generated/rustc_cfg`, and the rust objects then rebuild with a
command that gates `#![feature(compiler_builtins)]` behind an
allow-features list lacking it: E0725+E0658 on
rust/compiler_builtins.rs. (The executed command differs from the
recorded core command - failed commands never rewrite the stale .cmd, so
diffing .cmd files shows them identical; compare the regenerated
rustc_cfg instead: 16 rust cfg lines missing.)

Fix: units whose `sources` include `.rs` inherit `RUST_LIB_SRC` and a
`KRUSTFLAGS` mirror from `core.env` (KRUSTFLAGS computed against
`core.dev` = the exact path the core's placeholder-dev expanded to, so
the recomputed rustc command stays byte-identical to the core build's
recorded one). 6.18.59 x86 map: 4 units gated (builtin@rust,
builtin@drivers/gpu/drm, rnull_mod, rcpufreq_dt); every other unit drv
keeps its byte-identical identity.

General rule: ANY Kconfig `$(success,...)` probe is env-sensitive at
unit re-sync time - the unit env must reproduce every probe-relevant env
var the core build had, or the silent syncconfig rewrites auto.conf /
rustc_cfg differently and the seeded artifacts diverge from the recorded
commands.

## Included Makefile.* fragments: splice + escaped object dirs (fixed 2026-11-06)

Kbuild modules can source their object lists from fragments in OTHER
directories: arch/x86/kvm/Makefile does `include
$(srctree)/virt/kvm/Makefile.kvm`, whose `kvm-y := $(KVM)/kvm_main.o ...`
(with `KVM ?= ../../../virt/kvm`) supplies the module's core objects.
The include filter accepted only Makefile/Kbuild/*.mk basenames, so
Makefile.kvm was silently dropped: kvm's map row had 20 in-dir objects
and none of the virt/kvm ones, the unit's M= build demanded
`../../../virt/kvm/kvm_main.o` from modsrc and died with
"No rule to make target" (retry 10's single root failure).

Two-part fix in mapper.py:

1. `Makefile.*` fragments are now processed AND spliced into the
   INCLUDING dir's parse state (`process_file(..., splice_into=d)`):
   make's include is textual - variables and *-y lists are shared with
   the parent instance, and $(src)/$(obj) stay the parent's. The spliced
   fragment skips harvest/root-env promotion (the parent's tail handles
   the merged state); dedup keys carry the parent identity so a second
   includer still gets its own splice. Infra fragments
   (scripts/Makefile.compiler & co) define helper "functions" referenced
   only WITH arguments, which eval_ref treats as unresolvable
   regardless - splicing them is inert.
2. extraDirs gains the containing dirs of sources that escape the
   module dir (resolve_sources normpath-collapses ../ tokens), so the
   unit's modsrc staging carries virt/kvm and the M= build's relative
   ../../../ references resolve to copied files.

Verification gates for any future include-splice change: map blast
radius diff (old vs new modules.json rows), offline name-level Tarjan
SCC check over the new map's deps, invariance eval (core + unaffected
units byte-identical), then a kvm-family unit smoke build.

## Out-of-dir rule prerequisites and recipe words: rootRelPairs (fixed 2026-11-06)

Module-unit single-module builds run kbuild's M= machinery, whose
sub-make CWD is the MODULE dir. Tokens the Makefile expects to resolve
against the real build's kernel-root CWD die with "No rule to make
target" there. Two observed shapes:

- kernel-root-relative rule prerequisites (scsi_mod:
  `$(obj)/scsi_devinfo_tbl.c: include/scsi/scsi_devinfo.h` - a
  sed-generated source); drivers/scsi/Makefile is the only Makefile in
  the tree with this pattern - 52 units share its Makefile and all
  need the staging.
- bare-target rules and recipe words written with `../` paths that
  pathwalk into modsrc root (nfsd: `nfs4xdr_gen.c:`
  `../../Documentation/sunrpc/xdr/nfs4_1.x` plus the
  `../../tools/net/sunrpc/xdrgen/xdrgen` recipe tool and the
  `../../include/linux/sunrpc/xdrgen/nfs4_1.h` phony prereq). fs/nfsd
  is the only module Makefile with this pattern.

Fix: the mapper scans module Makefiles - rule-regex tokens plus a
whole-file word scan (isfile gates keep prose noise out;
Makefile/Kbuild/Kconfig are excluded so the self/ copies are not
shadowed) - and records stageable out-of-dir files as `rootRelPairs`
entries `token::normalized`: the token is the path as written,
resolved from the module dir; normalized names the in-tree file for
the symlink target. The unit drv stages file symlinks at
`modsrc/<dir>/<token>` (for `../` tokens mkdir/ln pathwalk into
modsrc root) targeting `$coreDev/lib/modules/<ver>/source/<normalized>`
(the fab6872375aa correction; `$coreBuild` was already .../build),
so the CWD-relative lookup hits the retained source copy. Symlinking
executables preserves the exec bit (xdrgen's nix-patched python
shebang works).

## Known limitations (v2)

- `$(eval $(foreach …))`-generated modules are invisible to the mapper
  (4 modules in the anchor config). The final drv still builds them.
- Modules whose Kbuild files use constructs the walker cannot resolve are
  recorded in `warnings.txt` rather than guessed.
- Dep edges from the EXPORT_SYMBOL scan are name-based and conservative;
  asm-only exports and macro-indirected calls can be missed (tier-only
  impact; final unaffected).
- The aggregate's dev output re-runs upstream's postInstall pruning, so
  the resumed build tree is not retained there; the per-module artifacts
  remain in the module drvs.
- Full mode only. A subset mode (`onlyModules`) existed for cheap smoke
  tests, but it saved nothing in any cache state (the aggregate recompiles
  uncovered modules internally) and its manifest changed the aggregate's
  drv identity, so it was removed (finding 10/11 above).

## Usage

```nix
let
  pkgs = import <nixpkgs> { };          # this branch
  pm = pkgs.callPackage ./pkgs/os-specific/linux/kernel/per-module {
    kernel = pkgs.linux;                # any kernel package
    kernelModuleMakeFlags = pkgs.linuxPackages.kernelModuleMakeFlags;
  };
in
{
  core = pm.core;
  module_8250 = pm.modules."8250";
  module_8250_dropin = pm.completeModule "8250";  # .ko + .ko.cer tier
  kernel_with_modules = pm.aggregate;             # modules tree + merged certs
  image = pm.image;                               # insert-sys-cert image
}
```
