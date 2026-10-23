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
  derives module→module edges from a conservative EXPORT_SYMBOL scan.
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
aggregate degrade gracefully: a subset build (`onlyModules`) still produces
a complete, correct kernel; the per-module caching applies to the subset.

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
    see them. Conservative mitigation available: list them via
    `onlyModules`-adjacent extension or accept the final drv building them
    (it does — see resume semantics).
  - 1 extra (`sparx5_serdes`) — same drift class, over-approximation = safe.
  - Mapper runtime: ~2.6 s CPU for the whole tree (this is why the map is a
    derivation rather than pure-Nix Makefile parsing; IFD documents itself).
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
- `nix-instantiate --parse` passes for all new expressions.

## Not yet verified / deferred (smoke test in progress)

- Core drv build (~30–60 min on nixbuild, running detached).
- Targeted compile-only builds of real modules through Nix.
- Module-level tier (`completeModule`) end-to-end incl. `.ko.cer`.
- Aggregate (subset via `onlyModules`) + image with merged certs.
- Full-set eval/instantiation of ~8000 module drvs (eval memory/time).

## Known deviations from the agreed design

- The module map is produced by a cheap derivation (Python) instead of
  pure-Nix eval, then IFD'd. Rationale: ~8000 modules over thousands of
  Makefiles; 2.6 s in Python vs minutes of Nix eval. The *edges still come
  from the source at mapping time*; only the execution vehicle differs.
- The compile-only drv needs a `src` output on the core drv (full patched
  source) so module sources are patch-consistent with the core by
  construction and module drvs never re-apply kernel patches. Cost: one
  ~1.4 GB store path per kernel, shared by all module drvs.
- `onlyModules` subset mode exists so the aggregate can be smoke-tested
  cheaply; the result remains a complete kernel (resume semantics fill in
  the rest).

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

## Usage

```nix
let
  pkgs = import <nixpkgs> { };          # this branch
  pm = pkgs.callPackage ./pkgs/os-specific/linux/kernel/per-module {
    kernel = pkgs.linux;                # any kernel package
    kernelModuleMakeFlags = pkgs.linuxPackages.kernelModuleMakeFlags;
    # onlyModules = [ "msm" "8250" ];   # optional subset for fast iteration
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
