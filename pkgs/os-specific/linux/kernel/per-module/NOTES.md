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
- The compile-only drv needs a `src` output on the core drv (full patched
  source) so module sources are patch-consistent with the core by
  construction and module drvs never re-apply kernel patches. Cost: one
  ~1.4 GB store path per kernel, shared by all module drvs.

## Known limitations

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
