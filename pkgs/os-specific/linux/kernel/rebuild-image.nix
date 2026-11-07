{
  lib,
  kernel,
}:

{
  target ? kernel.target,
  modulesTree ? null,
}:

let
  inherit (kernel) stdenv;
in

stdenv.mkDerivation {
  pname = "linux-image";
  inherit (kernel)
    version
    __structuredAttrs
    enableParallelBuilding
    hardeningDisable
    installFlags
    depsBuildBuild
    nativeBuildInputs
    makeFlags
    stripDebugList
    ;

  src = "${kernel.dev}/lib/modules/${kernel.modDirVersion}/build";

  buildFlags = [
    target

    # The kernel image targets depend on `vmlinux`, which is a phony
    # target pulling in all the kernel sources and intermediate build
    # outputs. We preserved the necessary files in the original kernel
    # build and don’t want to do the whole thing again, so tell Make to
    # assume that it doesn’t need rebuilding.
    #
    # We need to override `MAKE` here because Make deliberately doesn’t
    # propagate `--assume-old=` to sub‐Makes, and ignores it in
    # `MAKEFLAGS`.
    "MAKE=$(MAKE_COMMAND) --assume-old=vmlinux${lib.optionalString stdenv.hostPlatform.isx86 " --assume-old=vmlinux.unstripped"}"
  ];

  installTargets = [ (kernel.installTargetFor target) ];

  moduleCertificates =
    if modulesTree != null then
      "${modulesTree}/lib/modules/${kernel.modDirVersion}/modules.cer"
    else
      null;

  dontConfigure = true;

  preBuild = ''
    if [[ -v moduleCertificates ]]; then
      scripts/insert-sys-cert -b vmlinux -c "$moduleCertificates"
    fi
    # The relocs rule (arch/x86/boot/compressed/Makefile:130) reads
    # vmlinux.unstripped - the real link output (LD vmlinux.unstripped
    # in the aggregate log), which the stock postInstall does not keep:
    # it is in CLEAN_FILES, so `make clean` removes it and the keepPaths
    # rescue list carries only vmlinux. The unpacked tree is writable
    # (stdenv chmods u+w), so link the kept vmlinux under the unstripped
    # name - --assume-old=vmlinux.unstripped then treats it as up to
    # date and the RELOCS step can read it (retry-22: 'Cannot open
    # vmlinux.unstripped'). Hard link first (no extra space); fall back
    # to a copy across filesystems.
    ln vmlinux vmlinux.unstripped || cp -a vmlinux vmlinux.unstripped
  '';

  env = kernel.env // {
    buildRoot = ".";
  };

  meta = kernel.meta // {
    description = kernel.meta.description + " (image)";
  };
}
