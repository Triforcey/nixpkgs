# Per-module kernel packager: source mapper.
#
# Walks the kernel's Kbuild files (root Makefile + Kbuild, then every
# directory reachable via directory entries in `obj-*` and generic `*-y`
# variables, including core-y/drivers-y/libs-y wired by the arch Makefile),
# expands `obj-$(CONFIG_FOO)` against the shipped .config, collects the =m
# module set with their object lists, and derives module->module dependency
# edges from a conservative EXPORT_SYMBOL scan, pruned to a DAG (the
# per-module fixpoint interpolates dep store paths; cycles would recurse
# forever - see Mapper.prune_cycles).
#
# Design constraints (see NOTES.md):
#   * Over-approximation is safe (costs a spurious rebuild edge).
#   * Under-approximation is the dangerous failure mode; whenever a
#     construct cannot be resolved (unexpandable variables, unknown
#     conditionals), we take the union of both interpretations and record
#     a warning.
#
# Usage: python3 mapper.py <srcdir> <configfile> <arch> <out.json> <out.warnings>

import json
import os
import re
import sys
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor

IDENT = r"[A-Za-z0-9_]+"

ASSIGN_RE = re.compile(r"^([A-Za-z0-9_$(){\}\.\-/, \t]+)\s*(::?|\+|\?)?=\s*(.*)$")
COND_RE = re.compile(r"^(ifeq|ifneq|ifdef|ifndef)\b\s*(.*)$")
RULE_PREREQ_RE = re.compile(r'^\$\((?:obj|src)\)/[^\s:]+:\s*(\S+)', re.M)
EXPORT_RE = re.compile(r"\bEXPORT_SYMBOL(?:_[A-Za-z0-9_]*)?\s*\(\s*([A-Za-z_]\w*)")
TOKEN_RE = re.compile(r"[A-Za-z_]\w*")

MAX_DEPTH = 16


class Unresolved(Exception):
    pass


class Ctx:
    __slots__ = ("cfg", "env", "unset", "warnings", "where", "absent")

    def __init__(self, cfg, env, unset, warnings, where, absent):
        self.cfg = cfg
        self.env = env
        self.unset = unset
        self.warnings = warnings
        self.where = where
        self.absent = absent

    def warn(self, msg):
        self.warnings.append(f"{self.where}: {msg}")


# GNU make functions that occur in Kbuild obj contexts. Anything else is
# treated as unresolvable -> conservative handling at the call site.
FUNCS_1 = {"sort", "strip", "notdir", "basename", "dir", "words", "firstword", "lastword"}
FUNCS_2 = {"addprefix", "addsuffix", "filter", "filter-out"}
FUNCS_3 = {"subst", "patsubst"}


def expand(s, ctx, depth=0):
    """Expand $(...) references and make functions. Returns a string.
    Raises Unresolved when a needed variable is unknown."""
    if depth > MAX_DEPTH:
        ctx.warn("expansion too deep")
        raise Unresolved()
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            j = i + 2
            lvl = 1
            while j < n and lvl:
                if s[j] == "(":
                    lvl += 1
                elif s[j] == ")":
                    lvl -= 1
                j += 1
            if lvl:
                ctx.warn(f"unbalanced $() in '{s}'")
                raise Unresolved()
            inner = s[i + 2 : j - 1]
            out.append(eval_ref(inner, ctx, depth))
            i = j
        elif c == "$" and i + 1 < n and s[i + 1] == "{":
            j = i + 2
            lvl = 1
            while j < n and lvl:
                if s[j] == "{":
                    lvl += 1
                elif s[j] == "}":
                    lvl -= 1
                j += 1
            if lvl:
                ctx.warn(f"unbalanced ${{}} in '{s}'")
                raise Unresolved()
            out.append(eval_ref(s[i + 2 : j - 1], ctx, depth))
            i = j
        elif c == "$" and i + 1 < n and s[i + 1] == "$":
            out.append("$")
            i += 2
        elif c == "$":
            # $X single-char var: unused in Kbuild obj context
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def eval_ref(inner, ctx, depth):
    im = re.match(r"^if\s+(.*)$", inner, re.S)
    if im:
        # $(if cond,then[,else]): a non-empty (stripped) cond expands to
        # then, otherwise to else (default empty). Needed for the kernel's
        # obj-$(if $(CONFIG_KUNIT),y) += hooks.o idiom - hooks.o is built-in
        # whenever KUnit is enabled (y OR m) and the line is dead otherwise.
        args = split_args(im.group(1), ctx, depth, 3)
        if args[0].strip():
            return args[1] if len(args) > 1 else ""
        return args[2] if len(args) > 2 else ""
    m = re.match(r"^(%s)\s+(.*)$" % "|".join(sorted(FUNCS_1 | FUNCS_2 | FUNCS_3)), inner, re.S)
    if m:
        func, rest = m.group(1), m.group(2)
        args = split_args(rest, ctx, depth, 3 if func in FUNCS_3 else (2 if func in FUNCS_2 else 1))
        if func in FUNCS_3:
            if len(args) < 3:
                args = args + [""] * (3 - len(args))
            a, b, text = args
            if func == "subst":
                return text.replace(a, b)
            return patsubst(a, b, text)
        if func in FUNCS_2:
            if len(args) < 2:
                args = args + [""] * (2 - len(args))
            pat, text = args
            toks = text.split()
            if func == "addprefix":
                return " ".join(pat + t for t in toks)
            if func == "addsuffix":
                return " ".join(t + pat for t in toks)
            pats = pat.split()
            if func == "filter":
                res = [t for t in toks if any(match_pat(p, t) for p in pats)]
            else:
                res = [t for t in toks if not any(match_pat(p, t) for p in pats)]
            return " ".join(res)
        arg = args[0]
        if func == "sort":
            return " ".join(sorted(arg.split()))
        if func == "strip":
            return " ".join(arg.split())
        if func == "notdir":
            return " ".join(os.path.basename(t) for t in arg.split())
        if func == "basename":
            return " ".join(os.path.splitext(t)[0] for t in arg.split())
        if func == "dir":
            return " ".join((os.path.dirname(t) + "/") for t in arg.split())
        if func == "words":
            return str(len(arg.split()))
        if func == "firstword":
            w = arg.split()
            return w[0] if w else ""
        if func == "lastword":
            w = arg.split()
            return w[-1] if w else ""
        raise Unresolved()
    name = inner.strip()
    if name.startswith("CONFIG_"):
        if name in ctx.cfg:
            return ctx.cfg[name]
        if name in ctx.unset:
            # "is not set" symbols are NOT exported by auto.conf, so real
            # Kbuild's $(CONFIG_X) is EMPTY for them - not "n". Returning
            # "n" made truthiness contexts like $(if $(CONFIG_KUNIT),y)
            # evaluate TRUE and wrongly collected hooks.o into the builtin
            # unit. Empty is exact for obj- collection too (obj-n and obj-
            # are both dropped).
            return ""
        # Symbol absent from .config: kconfig does not export it at all, so
        # $(CONFIG_X) expands to the empty string in real Kbuild. Exact
        # behaviour, not an approximation.
        ctx.absent.add(name)
        return ""
    if name in ctx.env:
        val = ctx.env[name]
        return expand(val, ctx, depth + 1)
    ctx.warn(f"unresolvable variable '$({name})'")
    raise Unresolved()


def split_args(rest, ctx, depth, nargs):
    """Split function args on commas at the top level, expanding lazily so
    that arg-count mismatches still resolve the leading args."""
    parts = []
    buf = []
    i = 0
    lvl = 0
    while i < len(rest):
        c = rest[i]
        if c == "(":
            lvl += 1
        elif c == ")":
            lvl -= 1
        if c == "," and lvl == 0 and len(parts) < nargs - 1:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [expand(a, ctx, depth + 1) for a in parts]


def match_pat(pat, tok):
    if "%" not in pat:
        return pat == tok
    pre, _, post = pat.partition("%")
    return tok.startswith(pre) and tok.endswith(post) and len(tok) >= len(pre) + len(post)


def patsubst(pat, repl, text):
    out = []
    for t in text.split():
        if "%" in pat:
            pre, _, post = pat.partition("%")
            if t.startswith(pre) and t.endswith(post) and len(t) >= len(pre) + len(post):
                stem = t[len(pre) : len(t) - len(post)] if post else t[len(pre) :]
                rpre, _, rpost = repl.partition("%")
                out.append(rpre + stem + rpost)
                continue
        elif pat == t:
            out.append(repl)
            continue
        out.append(t)
    return " ".join(out)


def read_config(path):
    cfg = {}
    unset = set()
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=([ymn])$", line)
            if m:
                cfg[m.group(1)] = m.group(2)
                continue
            m = re.match(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$", line)
            if m:
                unset.add(m.group(1))
    # cfg = symbols with an explicit value (y/m from auto.conf-style files,
    # n from `CONFIG_FOO=n` lines if present); unset = `# CONFIG_FOO is not
    # set` symbols. auto.conf exports ONLY y/m symbols, so $(CONFIG_FOO) for
    # an unset symbol is EMPTY in real Kbuild - eval_ref returns "" for it
    # (a truthy "n" would make $(if $(CONFIG_FOO),y) evaluate TRUE).
    # `ifdef` evaluation lives in Dir.eval_cond, which mirrors real make
    # against auto.conf (y/m only).
    return cfg, unset


class Frame:
    __slots__ = ("resolvable", "then", "else_", "in_else", "taken")

    def __init__(self, resolvable, then, else_):
        self.resolvable = resolvable
        self.then = then
        self.else_ = else_
        self.in_else = False
        # True once some branch of this block evaluated active; later
        # `else if` branches must then stay inactive.
        self.taken = False

    def active(self):
        # Unresolvable condition -> collect BOTH branches (conservative).
        if not self.resolvable:
            return True
        return self.else_ if self.in_else else self.then


class Dir:
    """Per-directory parse state. Kbuild variables are per-directory."""

    def __init__(self, path):
        self.path = path
        self.env = {}
        self.obj_y = []
        self.obj_m = []
        self.obj_lists = {}  # stem -> {"y": [...], "m": [...]} from foo-y/foo-objs/foo-m
        self.frames = []

    def collect(self):
        return all(f.active() for f in self.frames)

    def eval_cond(self, line, cfg, unset, warnings, absent):
        """Evaluate a condition directive body. Returns (resolvable, result)."""
        m = COND_RE.match(line)
        if not m:
            warnings.append(f"{self.path}: cannot parse conditional '{line}'")
            return False, False
        kind, rest = m.group(1), m.group(2).strip()
        ctx = Ctx(cfg, self.env, unset, warnings, self.path, absent)
        if kind in ("ifdef", "ifndef"):
            name = rest.strip()
            em = re.match(r"^\$\((%s)\)$" % IDENT, name)
            if em:
                name = em.group(1)
            if not re.match(r"^%s$" % IDENT, name):
                return False, False
            if name.startswith("CONFIG_"):
                # Real kbuild evaluates `ifdef` against auto.conf, which
                # only exports symbols set to y or m; =n symbols are
                # absent, so `ifdef CONFIG_FOO` is FALSE for unset
                # (not-set) symbols. Evaluating against the full .config
                # (which has `# CONFIG_FOO is not set` lines) would wrongly
                # activate ifdef-guarded *-objs lines and make units
                # compile objects whose config gate is off (e.g.
                # cpci_hotplug_core.o colliding with the static-inline
                # stub in cpci_hotplug.h).
                defined = cfg.get(name) in ("y", "m")
            else:
                defined = name in self.env and bool(self.env[name])
            return True, defined
        pm = re.match(r"^\(\s*([^,]*?)\s*,\s*(.*?)\s*\)$", rest, re.S)
        if not pm:
            return False, False
        try:
            a = expand(pm.group(1), ctx).strip().strip('"').strip("'")
            b = expand(pm.group(2), ctx).strip().strip('"').strip("'")
            eq = a == b
        except Unresolved:
            return False, False
        return True, (eq if kind == "ifeq" else not eq)

    def push_cond(self, line, cfg, unset, warnings, absent):
        resolvable, result = self.eval_cond(line, cfg, unset, warnings, absent)
        if resolvable:
            self.frames.append(Frame(True, result, not result))
            self.frames[-1].taken = result
        else:
            warnings.append(f"{self.path}: unresolvable conditional '{line}' (taking both branches)")
            self.frames.append(Frame(False, True, True))
            self.frames[-1].taken = True

    def flip_else(self, line, cfg, unset, warnings, absent):
        """Handle `else` and `else if*` on the current frame. GNU make chains
        `else if` onto the SAME block; pushing a new frame would leak it past
        the matching endif."""
        if not self.frames:
            return
        f = self.frames[-1]
        f.in_else = True
        body = line[4:].strip()
        if not body:
            if f.resolvable and f.taken:
                f.else_ = False
            return
        resolvable, result = self.eval_cond(body, cfg, unset, warnings, absent)
        if resolvable:
            f.else_ = (not f.taken) and result
            f.taken = f.taken or result
        else:
            warnings.append(f"{self.path}: unresolvable conditional '{line}' (taking both branches)")
            f.resolvable = False
            f.else_ = True


def kbuild_file(d):
    """Kbuild wins over Makefile when both exist (same rule as Makefile.build)."""
    kb = os.path.join(d, "Kbuild")
    if os.path.exists(kb):
        return kb
    mf = os.path.join(d, "Makefile")
    if os.path.exists(mf):
        return mf
    return None


class Mapper:
    def __init__(self, srcdir, cfg, unset, arch, origsrc=None):
        self.srcdir = srcdir
        # the UNPATCHED upstream tree (if provided): copyTree detection checks
        # whether a unit's dir and build files even exist before host patches
        self.origsrc = origsrc
        self.cfg = cfg
        self.unset = unset
        self.arch = arch
        self.warnings = []
        self.visited = set()        # dirs already descended into by harvest
        self.processed = set()      # (dir, fname) files already parsed
        self.global_env = {}
        self.absent = set()
        self.modules = OrderedDict()  # key -> module dict
        self.module_order = []
        # Sources of built-in (obj-y) objects, used by patch classification
        # to recognize prepare-time source reach.
        self.builtin_objs = set()   # tree-relative object stem paths
        self.builtin_files = set()  # existing source paths of those objects
        self.patch_info = []        # per-patch classification (analyze_patches)

    def process_file(self, dirpath, fname, splice_into=None):
        real = dirpath if dirpath == "" else os.path.normpath(dirpath)
        path = os.path.join(self.srcdir, real, fname) if fname else os.path.join(self.srcdir, real)
        fkey = (real, fname) if splice_into is None else (real, fname, id(splice_into))
        if fkey in self.processed:
            return
        try:
            with open(path, "r", errors="replace") as f:
                text = f.read()
        except OSError:
            self.warnings.append(f"{real}: missing kbuild file {fname}")
            return
        self.processed.add(fkey)
        if splice_into is None:
            d = Dir(real)
            d.env["SRCARCH"] = self.arch
            d.env["ARCH"] = self.arch
            d.env["srctree"] = "."
            d.env["src"] = real
            d.env["obj"] = real
            d.env.update(self.global_env)
        else:
            # make's `include` splices the fragment into the INCLUDING
            # makefile's instance: variables and *-y lists are shared with
            # the parent's parse state (kvm: virt/kvm/Makefile.kvm defines
            # kvm-y += $(KVM)/kvm_main.o, consumed by arch/x86/kvm).
            # $(src)/$(obj) stay the parent's, exactly as in the real
            # make instance.
            d = splice_into

        # join backslash continuations into single logical lines
        lines = []
        cur = ""
        for raw in text.split("\n"):
            if raw.endswith("\\") and not raw.endswith("\\\\"):
                cur += raw[:-1] + " "
            else:
                cur += raw
                lines.append(cur)
                cur = ""
        if cur:
            lines.append(cur)

        for lineno, line in enumerate(lines, 1):
            where = f"{real}:{lineno}"
            hash_idx = line.find("#")
            if hash_idx >= 0:
                line = line[:hash_idx]
            line = line.strip()
            if not line:
                continue

            if line.startswith(("ifeq", "ifneq", "ifdef", "ifndef")):
                d.push_cond(line, self.cfg, self.unset, self.warnings, self.absent)
                continue
            if line == "else" or line.startswith("else "):
                d.flip_else(line, self.cfg, self.unset, self.warnings, self.absent)
                continue
            if line == "endif":
                if d.frames:
                    d.frames.pop()
                continue
            if not d.collect():
                continue

            # includes
            im = re.match(r"^(-?include)\s+(.*)$", line)
            if im:
                for inc in im.group(2).split():
                    try:
                        incx = expand(inc, Ctx(self.cfg, d.env, self.unset, self.warnings, where, self.absent))
                    except Unresolved:
                        self.warnings.append(f"{where}: unresolvable include '{inc}'")
                        continue
                    incx = incx.strip('"')
                    base = os.path.basename(incx)
                    # Makefile.<suffix> fragments (Makefile.kvm, lib/vdso's
                    # Makefile.include, ...) are real kbuild include
                    # targets; dropping them silently lost their obj lists
                    # (kvm died with "No rule to make target
                    # '../../../virt/kvm/kvm_main.o'"). Infra fragments
                    # (scripts/Makefile.compiler & co) define helper
                    # "functions" referenced only WITH arguments, which
                    # eval_ref treats as unresolvable regardless - splicing
                    # them is inert.
                    if (base in ("Makefile", "Kbuild") or base.endswith(".mk")
                            or base.startswith("Makefile.")):
                        incdir = os.path.dirname(incx)
                        if not incdir:
                            cands = [real]
                        elif incx.startswith("/"):
                            cands = [incdir]
                        else:
                            # make resolves includes against the make
                            # instance's cwd - the kernel root for every
                            # kbuild sub-make. $(srctree)-relative
                            # fragments (virt/kvm, lib/vdso) and
                            # $(src)-relative ones (arch/arm/mach-s3c)
                            # both occur; try the tree root first, then
                            # the including dir.
                            cands = [os.path.normpath(incdir),
                                     os.path.normpath(os.path.join(real, incdir))]
                        chosen = cands[0]
                        for c in cands:
                            if os.path.exists(os.path.join(self.srcdir, c, base)):
                                chosen = c
                                break
                        self.process_file(chosen, base, splice_into=d)
                continue

            # variable assignments (strip an optional leading `export`)
            line2 = re.sub(r"^export\s+", "", line)
            am = ASSIGN_RE.match(line2)
            if not am:
                continue
            lhs, op, rhs = am.group(1), am.group(2) or "", am.group(3)
            ctx = Ctx(self.cfg, d.env, self.unset, self.warnings, where, self.absent)
            try:
                lhsx = expand(lhs, ctx).strip()
                rhsx = expand(rhs, ctx)
            except Unresolved:
                # The LHS may still resolve on its own even when the RHS does
                # not (e.g. obj-$(CONFIG_X) with X absent from .config ->
                # "obj-", RHS referencing an undefined helper variable like
                # pxa2xx-obj-y). The suffix alone decides: obj-y/obj-m are
                # live targets; anything else ("n", "", arbitrary names) is
                # config-gated off in real Kbuild, so dropping the whole line
                # is exact, not an approximation. Blind y+m union here put
                # arch-gated objects (pxa2xx_base.o, cpci_hotplug_core.o)
                # into builtin units and broke their compiles.
                if re.match(r"^obj-", lhs.strip()):
                    try:
                        lhs_only = expand(lhs, Ctx(self.cfg, d.env, self.unset, self.warnings, where, self.absent)).strip()
                    except Unresolved:
                        lhs_only = None
                    suffix = lhs_only[4:] if lhs_only is not None else None
                    if suffix is not None and not (
                        suffix == "y" or suffix == "m"
                        or (suffix and set(suffix) <= set("ym"))
                    ):
                        continue
                    # Live or genuinely unresolvable LHS: conservative
                    # inclusion of the resolvable tokens, routed to the LHS's
                    # own list when the suffix is known (obj-y wins over m in
                    # real Kbuild, so y-only routing for a resolved y LHS is
                    # exact; the y+m union remains only for a dead LHS).
                    if suffix == "m":
                        lists = [d.obj_m]
                    elif suffix == "y":
                        lists = [d.obj_y]
                    else:
                        lists = [d.obj_y, d.obj_m]
                        self.warnings.append(f"{where}: unresolvable obj assignment '{lhs}' (treating as y and m)")
                    for tok in rhs.split():
                        try:
                            tx = expand(tok, Ctx(self.cfg, d.env, self.unset, self.warnings, where, self.absent))
                        except Unresolved:
                            continue
                        for tgt in lists:
                            tgt.append(tx)
                continue
            if "$(" in lhsx or "$(" in rhsx:
                self.warnings.append(f"{where}: partially expanded assignment '{lhs} {op} {rhs}'")
                continue

            # obj-* targets (must be checked before the composite-list match,
            # because "obj-y" itself looks like stem "obj" + suffix "y")
            if lhsx.startswith("obj-"):
                suffix = lhsx[4:]
                if suffix == "y":
                    targets = [d.obj_y]
                elif suffix == "m":
                    targets = [d.obj_m]
                elif suffix in ("n", ""):
                    continue
                else:
                    vals = set(suffix)
                    if vals and vals <= set("ym"):
                        # config-composite suffix (e.g. $(A)$(B) -> "ym"):
                        # contribute to both lists (conservative)
                        targets = [d.obj_y, d.obj_m]
                    else:
                        # arbitrary variable name (obj-kvm := ...) or =n
                        self.warnings.append(f"{where}: obj target '{lhsx}' ignored (suffix '{suffix}' not y/m)")
                        continue
                for tgt in targets:
                    tgt.extend(rhsx.split())
                continue

            # composite object lists: foo-y := ... / foo-objs += ... / foo-m := ...
            lm = re.match(r"^([A-Za-z0-9_.\-]+?)-(y|m|objs|n|)$", lhsx)
            if lm and not lhsx.startswith("obj-"):
                stem = lm.group(1)
                suffix = lm.group(2)
                objs = []
                dirs = []
                for t in rhsx.split():
                    (dirs if t.endswith("/") else objs).append(t)
                if suffix in ("", "n"):
                    # netfilter-$(CONFIG_X) with X=n/unset: real kbuild drops
                    # the whole line - obj tokens AND dir walk tokens. The
                    # dir-walk extension below (d.obj_y.extend) otherwise
                    # harvested config-gated dirs (arch/x86/math-emu with
                    # CONFIG_MATH_EMULATION absent on x86_64) as builtin
                    # units and broke their compiles. Suffix-y lines (libs-y,
                    # drivers-$(CONFIG_PCI=y)) are unaffected.
                    continue
                # Dir tokens in *-y vars are vmlinux/module walk lists
                # (core-y, libs-y, drivers-y, ...); walk them like obj-y.
                d.obj_y.extend(dirs)
                # Always record the value: other Kbuild code references it
                # ($(libs-y) in ARCH_LIB, etc.). `+=` appends, `:=`/`=` replaces.
                joined = " ".join(objs + dirs)
                if "+" in op:
                    d.env[lhsx] = (d.env.get(lhsx, "") + " " + joined).strip()
                else:
                    d.env[lhsx] = joined
                if suffix not in ("", "n") and objs:
                    # Assignment semantics mirror d.env above: `+=` appends,
                    # `:=`/`=` replaces - per SUFFIX bucket, because foo-y and
                    # foo-m are separate make variables. Provenance matters:
                    # real kbuild's real-obj-y consumes only -objs/-y parts
                    # of a built-in composite, while -m parts are consumed
                    # only when the composite itself is a module. mounts-m +=
                    # do_mounts_rd.o (CONFIG_BLK_DEV_RAM=m) is dead for the
                    # built-in mounts.o - flattening it into the builtin@init
                    # unit compiled do_mounts_rd.c against do_mounts.h's
                    # no-op stubs and broke the init unit.
                    bucket = "m" if suffix == "m" else "y"
                    if "+" in op:
                        d.obj_lists.setdefault(stem, {}).setdefault(bucket, []).extend(objs)
                    else:
                        prev = d.obj_lists.get(stem) or {}
                        d.obj_lists[stem] = {**prev, bucket: list(objs)}
                continue

            # any other var: record raw value (core-y, drivers-y, libs-y, ...)
            if re.match(r"^%s(-[a-z0-9]+)?$" % IDENT, lhsx):
                if "+" in op:
                    d.env[lhsx] = (d.env.get(lhsx, "") + " " + rhsx).strip()
                else:
                    # `:=`/`=` replace; presets (SRCARCH, srctree, ...) are
                    # overwritten exactly like make would
                    d.env[lhsx] = rhsx
                # dirs in any *-y/*-m var are walked too
                if re.search(r"-(y|m|objs)$", lhsx):
                    for tx in rhsx.split():
                        if tx.endswith("/"):
                            d.obj_y.append(tx)
                continue

        if splice_into is not None:
            # The parent's own tail registers/records the merged state; a
            # spliced fragment must not harvest (its real dir differs from
            # the state's owner) or promote the root env.
            return
        # process collected entries
        self.harvest(d, real)
        # remember the root Makefile env as global (ARCH_CORE, libs-y, ... are
        # consumed by the root Kbuild and arch Makefiles)
        if real == "" and fname == "Makefile":
            self.global_env = dict(d.env)

    def _mark_builtin(self, objpath):
        """Record a built-in object stem path and its existing sources."""
        self.builtin_objs.add(objpath)
        for ext in (".c", ".S", ".rs"):
            if os.path.exists(os.path.join(self.srcdir, objpath + ext)):
                self.builtin_files.add(objpath + ext)

    def _record_builtin(self, d, real):
        """obj-y objects - and the parts of composite objects whose
        composite target is obj-y - are built into vmlinux. Record their
        source paths so patch classification can keep core-feeding
        patches out of the leaf class (Option A)."""
        for tok in set(d.obj_y):
            if tok.endswith("/"):
                continue
            stem = tok[:-2] if tok.endswith(".o") else tok
            self._mark_builtin(os.path.normpath(os.path.join(real, stem)))
            # composite object parts inherit the composite's y/m status;
            # only -objs/-y parts are part-of-builtin (real-obj-y)
            for part in (d.obj_lists.get(stem) or {}).get("y", []):
                if part.endswith("/"):
                    continue
                pstem = part[:-2] if part.endswith(".o") else part
                self._mark_builtin(os.path.normpath(os.path.join(real, pstem)))

    def harvest(self, d, real):
        seen = set()
        self._record_builtin(d, real)
        self._register_builtin(d, real)
        for tok in d.obj_y + d.obj_m:
            if tok.endswith("/"):
                sub = os.path.normpath(os.path.join(real, tok[:-1]))
                if sub in seen or sub in self.visited:
                    continue
                seen.add(sub)
                self.visited.add(sub)
                f = kbuild_file(os.path.join(self.srcdir, sub))
                if f:
                    self.process_file(sub, os.path.basename(f))
                else:
                    self.warnings.append(f"{sub}: no Makefile/Kbuild")
        # Kbuild semantics: an object listed in both obj-y and obj-m is
        # built as built-in (y wins).
        y_set = set(d.obj_y)
        for tok in d.obj_m:
            if tok.endswith("/") or tok in y_set:
                continue
            stem = tok[:-2] if tok.endswith(".o") else tok
            if "/" in stem:
                # obj-$(CONFIG_X) += subdir/module.o: the module lives in the
                # subdir; its object list may be defined either there or here
                sub = os.path.dirname(stem)
                self.register_module(os.path.basename(stem), d, os.path.normpath(os.path.join(real, sub)))
            else:
                self.register_module(stem, d, real)

    def _register_builtin(self, d, real):
        """obj-y objects are compile units of their own (no full tree build,
        ever): every directory with built-in content becomes a per-directory
        unit whose sandbox compiles exactly that directory's objects; the
        aggregate seeds the artifacts and performs the vmlinux link.
        Registered as a pseudo-module (name 'builtin@<dir>') so the uniform
        machinery (dir fixes, include closures, patch applicability) applies
        without special-casing downstream."""
        objs = []
        for tok in d.obj_y:
            if tok.endswith("/"):
                continue
            stem = tok[:-2] if tok.endswith(".o") else tok
            parts = [o for o in (d.obj_lists.get(stem) or {}).get("y", [])
                     if not o.endswith("/")]
            if parts:
                objs.extend(parts)
            else:
                # no -y/-objs parts: real kbuild treats the target as a
                # plain single object (real-obj-y falls back to $(m))
                objs.append(tok if tok.endswith(".o") else stem + ".o")
        if not objs:
            return
        prev = self.modules.get("builtin@" + real)
        if prev is not None:
            self.warnings.append(f"{real}: duplicate built-in registration; merging")
            prev["objects"] = prev["objects"] + [
                o for o in objs if o not in prev["objects"]
            ]
            return
        self.modules["builtin@" + real] = {
            "name": "builtin@" + real,
            "key": "builtin@" + real,
            "builtin": True,
            "dir": real,
            "objects": objs,
            "order": len(self.module_order),
        }
        self.module_order.append("builtin@" + real)

    def register_module(self, stem, d, real):
        objects = []
        buckets = d.obj_lists.get(stem) or {}
        # real-obj-m consumes -objs/-y AND -m parts of a module composite
        lst = buckets.get("y", []) + buckets.get("m", [])
        if lst:
            for o in lst:
                if o.endswith(".o"):
                    objects.append(o)
                elif o.endswith("/"):
                    self.warnings.append(f"{real}: module {stem} lists dir '{o}' in object list")
        else:
            objects = [stem + ".o"]
        name = stem
        key = name
        if key in self.modules:
            key = name + "@" + real
            self.warnings.append(f"{real}: duplicate module name '{name}' (also defined elsewhere)")
        self.modules[key] = {
            "name": name,
            "key": key,
            "dir": real,
            "objects": objects,
            "order": len(self.module_order),
        }
        self.module_order.append(key)

    def resolve_sources(self):
        for m in self.modules.values():
            srcs = []
            for obj in m["objects"]:
                stem = obj[:-2]
                p = os.path.normpath(os.path.join(m["dir"], stem))
                for ext in (".c", ".S", ".rs"):
                    if os.path.exists(os.path.join(self.srcdir, p + ext)):
                        srcs.append(p + ext)
                        break
                else:
                    if not m["name"].startswith("builtin@"):
                        # generated objects (sed/gawk-produced .c/.S) have no
                        # source in the tree; the in-tree make rules build them
                        self.warnings.append(
                            f"{m['dir']}: no source found for object {obj}")
            m["sources"] = srcs

    def fix_module_dirs(self):
        """kbuild compiles obj-$(CONFIG_X) += subdir/file.o from the
        Makefile that names the object; the file's own directory needs no
        Makefile of its own (ti: 'icssg/icss_iep.o' with icssg/ holding
        only sources). An M= build of such a subdir fails with
        'Makefile: No such file or directory' - the per-module build must
        target a directory kbuild can process, i.e. one whose Makefile
        defines the module. Reassign the dir to the nearest ancestor
        whose Makefile/Kbuild mentions one of the module's objects, and
        prefix the dir-relative objects with the relative tail.
        Sources are already tree-relative (resolve_sources) and stay
        valid; the dir copy then carries the whole subtree (incl. the
        sources' subdir) into modsrc."""
        for m in self.modules.values():
            base = m["dir"]
            if (os.path.exists(os.path.join(self.srcdir, base, "Makefile"))
                    or os.path.exists(os.path.join(self.srcdir, base, "Kbuild"))):
                continue
            m["preFixDir"] = base
            objects = m.get("objects") or []
            anc = os.path.dirname(base)
            while anc and anc not in ("", ".", "/"):
                hit = False
                for mk in ("Makefile", "Kbuild"):
                    try:
                        with open(os.path.join(self.srcdir, anc, mk), errors="replace") as f:
                            mktext = f.read()
                    except OSError:
                        continue
                    if any(obj in mktext for obj in objects):
                        hit = True
                        break
                if hit:
                    tail = os.path.relpath(base, anc)
                    m["dir"] = anc
                    m["objects"] = [os.path.join(tail, o) for o in objects]
                    break
                anc = os.path.dirname(anc)
            else:
                self.warnings.append(
                    f"{base}: no ancestor Makefile mentions {objects}; dir kept")

    def detect_extra_dirs(self):
        """Modules may include headers outside their own directory via
        relative paths (e.g. 8250.h including ../serial_mctrl_gpio.h). The
        compile drv only copies the module dir, so record every in-tree
        directory whose files must also be copied for the includes to
        resolve. Walk breadth-first from the module dir: files in a newly
        discovered dir may themselves escape (capped for safety).
        Over-approximation is safe (extra copied sources).

        Include-root flags in the module's own Makefile/Kbuild name the
        other resolution roots an M= build has:

        - `-I $(src)/x`: $(src) is the module dir in an M= build, so these
          resolve inside modsrc once the named dir is copied there at its
          in-tree path (brcmfmac/bca: `-I $(src)/..` for core.h, bus.h).
          Added unconditionally - the real kernel build always compiles
          with these roots.
        - `-I$(srctree)/x`: still names srctree, which is the pruned dev
          tree, so the dir must be copied into modsrc AND made reachable
          (the drvs add KCFLAGS -I entries pointing at the modsrc copies).
          Added when a scanned include actually resolves inside such a
          root (au8522_priv.h "tuner-i2c.h" -> drivers/media/tuners).

        Both quoted ("...") and angle (<...>) includes are scanned; angle
        includes never resolve relative to the including file's directory,
        only through srctree roots.

        Ancestor Makefiles: subdir-ccflags-y propagates to every descendant
        directory in the real build (gpib: drivers/gpib/Makefile sets
        subdir-ccflags-y += -I$(src)/include, which is how gpibP.h in
        drivers/gpib/include/ is found), but an M= build of the module dir
        never reads ancestors, so those flags are lost. Collect their
        include roots here: $(src)-relative roots resolve relative to the
        ANCESTOR dir and are copied into modsrc at the in-tree path (the
        drvs' KCFLAGS -I entries make them reachable); $(srctree) roots
        join the candidate pool like the module's own."""
        inc_quoted_re = re.compile(r'#\s*include\s+"([^"]+)"')
        inc_angle_re = re.compile(r'#\s*include\s+<([^>]+)>')
        # -I flags, tolerating space between -I and the argument (kernel
        # Makefiles write both "-I$(src)/.." and "-I $(src)").
        flag_re = re.compile(
            r'-I\s*\$\((src|srctree)\)/?([^\s\\]*)|-I\s*([A-Za-z0-9_./][^\s\\]*)')
        max_depth = 8
        for m in self.modules.values():
            base = m["dir"]
            seen_dirs = {base}
            frontier = [base]
            candidates = []
            for mk in ("Makefile", "Kbuild"):
                try:
                    with open(os.path.join(self.srcdir, base, mk), errors="replace") as f:
                        # Join backslash continuations: flag_re matches per
                        # line and a -I on a continuation line (qtnfmac:
                        # ccflags-y += \  -Idrivers/...) would be missed.
                        mktext = re.sub(r"\\\\\s*\n", " ", f.read())
                except OSError:
                    continue

                def handle_flag(mm, _base=base):
                    kind, rest = mm.group(1), mm.group(2)
                    if kind is None and mm.group(3):
                        # Plain tree-relative -I path (qtnfmac: ccflags-y
                        # += \  -Idrivers/net/wireless/quantenna/qtnfmac,
                        # with a pcie/ source bare-including shm_ipc.h
                        # from the dir root). Real kbuild resolves it
                        # against the make CWD - the pruned core tree,
                        # where the module dir is absent - so modsrc must
                        # carry it and the drv must emit the own-dir -I
                        # when the path IS the module dir (extraDirs
                        # excludes base).
                        pdir = os.path.normpath(mm.group(3).rstrip("/\\"))
                        if pdir == _base:
                            m["ownDirIncludeFirst"] = True
                        elif (pdir.startswith(_base + os.sep)
                                and os.path.isdir(
                                    os.path.join(self.srcdir, pdir))):
                            seen_dirs.add(pdir)
                        return
                    if kind == "src":
                        # $(src) is the module dir in an M= build: the flag
                        # resolves inside modsrc once the named dir exists.
                        if not rest:
                            return
                        rdir = os.path.normpath(os.path.join(_base, rest))
                        if (rdir != _base
                                and os.path.isdir(os.path.join(self.srcdir, rdir))):
                            seen_dirs.add(rdir)
                    else:
                        cdir = os.path.normpath(rest.rstrip("/\\"))
                        if (cdir and cdir not in candidates
                                and os.path.isdir(os.path.join(self.srcdir, cdir))):
                            candidates.append(cdir)

                # Simple single-assignment variables (amdgpu:
                # FULL_AMD_PATH=$(src)/..) hide include directives and -I
                # flags behind \$(VAR) that the literal scanners cannot
                # see. Resolve them with bounded substitution (chained
                # assignments resolve too: FULL_AMD_DISPLAY_PATH =
                # \$(FULL_AMD_PATH)/\$(DISPLAY_FOLDER_NAME)); unknown vars
                # stay \$(X) and are skipped downstream. Trailing comments
                # are stripped from values so resolved paths stay clean.
                assign = {}
                for am in re.finditer(
                        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:+?]?=\s*(.+)$",
                        mktext, re.M):
                    name, val = am.group(1), am.group(2).split("#")[0].strip()
                    if name not in assign and val:
                        assign[name] = val

                def subst(tok):
                    for _ in range(7):
                        nxt = re.sub(
                            r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)",
                            lambda mm2: assign.get(mm2.group(1), mm2.group(0)),
                            tok)
                        if nxt == tok:
                            break
                        tok = nxt
                    return tok

                for mm in flag_re.finditer(mktext):
                    handle_flag(mm)
                for mm in flag_re.finditer(subst(mktext)):
                    handle_flag(mm)

                # include directives with \$(VAR)-based paths that resolve
                # to sibling source dirs (amdgpu: include
                # \$(FULL_AMD_PATH)/amdkfd/Makefile) need those dirs copied
                # into modsrc at in-tree paths - extraDirs gives the copy
                # AND the extraIncFlags -I. Only the module's own Makefile
                # is scanned: an included dir (display) is copied whole,
                # so ITS internal includes resolve against the modsrc copy
                # at build time. srctree- and unresolved-var tokens resolve
                # against the build tree and are skipped. Included
                # Makefiles pull their object lists from the dir, so the
                # dir (not the file) is what gets staged.
                for im in re.finditer(
                        r"^\s*(?:-include|sinclude|include)\s+(.+)$",
                        mktext, re.M):
                    for tok in im.group(1).split():
                        rtok = subst(tok)
                        if rtok.startswith("$(src)/"):
                            rpath = os.path.normpath(os.path.join(
                                base, rtok[len("$(src)/"):]))
                        elif "$(" in rtok:
                            continue
                        else:
                            rpath = os.path.normpath(os.path.join(base, rtok))
                            if not os.path.exists(
                                    os.path.join(self.srcdir, rpath)):
                                rpath = os.path.normpath(rtok)
                        if (rpath and not rpath.startswith("..")
                                and not (rpath == base
                                         or rpath.startswith(base + os.sep))
                                and os.path.isdir(
                                    os.path.join(self.srcdir, rpath))):
                            seen_dirs.add(rpath)

            # Rule prerequisites and recipe words that escape the module
            # dir. Real kbuild resolves bare tokens against the make CWD
            # - the kernel root in an in-tree build - but the unit's
            # single-module sub-make runs with the MODULE dir as CWD,
            # where the pruned core tree lacks them; the drv stages file
            # symlinks into modsrc for these. Two shapes: scsi_mod
            # $(obj)/scsi_devinfo_tbl.c: include/scsi/scsi_devinfo.h is
            # kernel-root-relative as written; nfsd writes bare-target
            # rules and recipe tool refs with ../ paths that pathwalk
            # into modsrc root (../../Documentation/sunrpc/xdr/nfs4_1.x,
            # ../../tools/net/sunrpc/xdrgen/xdrgen). Pairs are emitted
            # as "token::normalized" - the token resolves from the
            # module dir, the normalized path names the symlink target.
            root_rel = {}
            for mk in ("Makefile", "Kbuild"):
                try:
                    with open(os.path.join(self.srcdir, base, mk), errors="replace") as f:
                        mktext = re.sub(r"\\\\\s*\n", " ", f.read())
                except OSError:
                    continue
                toks = [mm.group(1) for mm in RULE_PREREQ_RE.finditer(mktext)]
                # Whole-file word scan: bare-target rules and recipe
                # tool references the rule regex misses. The isfile
                # gates keep comment/prose noise out.
                toks.extend(mm.group(0) for mm in re.finditer(r"\S+", mktext))
                for tok in toks:
                    ent = None
                    if tok.startswith("$(") or tok.startswith(base + os.sep):
                        continue
                    if tok in ("Makefile", "Kbuild", "Kconfig"):
                        # Top-level files every Makefile names; staging
                        # them inside the module dir would shadow the
                        # self/ copies.
                        continue
                    if os.path.isfile(os.path.join(self.srcdir, tok)):
                        ent = (tok, tok)
                    elif tok.startswith(".."):
                        norm = os.path.normpath(os.path.join(base, tok))
                        if (not norm.startswith("..")
                                and not (norm == base
                                         or norm.startswith(base + os.sep))
                                and os.path.isfile(os.path.join(self.srcdir, norm))):
                            ent = (tok, norm)
                    if ent is not None:
                        root_rel[ent[0]] = ent[1]
            if root_rel:
                m["rootRelPairs"] = [
                    f"{tok}::{norm}" for tok, norm in sorted(root_rel.items())]

            # subdir-ccflags-y in ancestor Makefiles (kbuild propagates it
            # to all descendants; M= builds of the module dir never read
            # ancestors, so the roots must be recovered here).
            anc = os.path.dirname(base)
            while anc and anc not in ("", ".", "/"):
                for mk in ("Makefile", "Kbuild"):
                    try:
                        with open(os.path.join(self.srcdir, anc, mk), errors="replace") as f:
                            # Join backslash continuations (see above).
                            mktext = re.sub(r"\\\\\s*\n", " ", f.read())
                    except OSError:
                        continue
                    for ln in mktext.splitlines():
                        if "subdir-ccflags-y" not in ln:
                            continue
                        for mm in flag_re.finditer(ln):
                            kind, rest = mm.group(1), mm.group(2)
                            if kind is None and mm.group(3):
                                # Plain tree-relative -I path in an
                                # ancestor's subdir-ccflags-y: tree paths,
                                # resolved against the make CWD; same
                                # own-dir handling as above.
                                pdir = os.path.normpath(
                                    mm.group(3).rstrip("/\\"))
                                if pdir == base:
                                    m["ownDirIncludeFirst"] = True
                                elif (pdir.startswith(base + os.sep)
                                        and os.path.isdir(
                                            os.path.join(self.srcdir,
                                                         pdir))):
                                    seen_dirs.add(pdir)
                                continue
                            if kind == "src":
                                rdir = os.path.normpath(
                                    os.path.join(anc, rest) if rest else anc)
                                if (rdir != base and rdir not in seen_dirs
                                        and os.path.isdir(
                                            os.path.join(self.srcdir, rdir))):
                                    seen_dirs.add(rdir)
                            else:
                                cdir = os.path.normpath(rest.rstrip("/\\"))
                                if (cdir and cdir not in candidates
                                        and os.path.isdir(
                                            os.path.join(self.srcdir, cdir))):
                                    candidates.append(cdir)
                parent = os.path.dirname(anc)
                if parent == anc:
                    break
                anc = parent
            # Include closure over FILES, seeded with the module's own
            # sources. Multi-directory modules keep their sources in
            # subdirs of the module dir (hns3: objects hns3pf/*.o under
            # dir hns3), so a top-level dir scan misses them - and a
            # subtree walk over-collects (shared dirs like dvb-frontends
            # would re-scan every sibling module's files per module).
            # A file-level BFS follows the actual include graph:
            # quoted includes resolving inside the module dir enqueue the
            # reached header (its own includes matter; no copy needed -
            # the whole subtree ships in modsrc anyway), includes
            # resolving outside the module dir record their dir for
            # copying, and unresolved bare/angle includes resolve through
            # the -I candidate roots collected above (the drvs make those
            # roots reachable via KCFLAGS -I entries). All paths stay
            # tree-relative.
            scanned_files = set()
            file_frontier = [s for s in (m.get("sources") or [])]
            for _ in range(max_depth):
                files_nxt = []
                for rel in file_frontier:
                    if rel in scanned_files:
                        continue
                    scanned_files.add(rel)
                    try:
                        with open(os.path.join(self.srcdir, rel), errors="replace") as f:
                            text = f.read()
                    except OSError:
                        continue
                    fdir = os.path.dirname(rel)
                    # Tracepoint machinery: '#include TRACE_INCLUDE(...)'
                    # is macro-expanded, so the include regex cannot see
                    # it. When a reached header pulls in
                    # trace/define_trace.h, resolve its
                    # TRACE_INCLUDE_PATH/TRACE_INCLUDE_FILE and record the
                    # dir that must be on the -I path. The real build gets
                    # it there via the Makefile's -I flags, which are dead
                    # here for srctree-based ones; the drvs' KCFLAGS -I
                    # entries (one per extraDir) supply it instead. The
                    # a/b KCFLAGS probe already covers PATHs that climb out
                    # of include/trace into the module's own dir, so only
                    # dirs that are not already reachable need recording
                    # (hns3: TRACE_INCLUDE_PATH=. with the trace header in
                    # the hns3pf/ subdir of the module dir).
                    if "trace/define_trace.h" in text:
                        pm = re.search(r'#define\s+TRACE_INCLUDE_PATH\s+([^\n]+)', text)
                        fm = re.search(r'#define\s+TRACE_INCLUDE_FILE\s+([^\n]+)', text)
                        if pm and fm:
                            tpath = re.sub(r"/\*.*", "", pm.group(1)).strip()
                            tfname = re.sub(r"/\*.*", "", fm.group(1)).strip() + ".h"
                            for loc in (
                                os.path.normpath(os.path.join(fdir, tpath, tfname)),
                                os.path.normpath(os.path.join("include/trace", tpath, tfname)),
                                os.path.normpath(os.path.join(base, tpath, tfname)),
                            ):
                                if os.path.exists(os.path.join(self.srcdir, loc)):
                                    d = os.path.dirname(loc)
                                    if d and d != base and d not in seen_dirs:
                                        seen_dirs.add(d)
                                    elif d == base:
                                        # The trace header lives in the module
                                        # dir itself. In-tree the define_trace
                                        # re-include resolves via -I$(src)
                                        # (ccflags-y or a per-file
                                        # CFLAGS_<f>.o flag, e.g. mac802154's
                                        # CFLAGS_trace.o := -I$(src)). The
                                        # sandbox has no automatic own-dir -I,
                                        # and a KCFLAGS extraDir may contain a
                                        # same-named trace header of a sibling
                                        # module (mac802154 vs
                                        # net/ieee802154) shadowing the
                                        # module's own system. The drv places
                                        # -I$PWD/modsrc/<dir> FIRST when this
                                        # flag is set.
                                        m["ownDirIncludeFirst"] = True
                                    break
                    incs = [(mm.group(1), True)
                            for mm in inc_quoted_re.finditer(text)]
                    incs += [(mm.group(1), False)
                             for mm in inc_angle_re.finditer(text)]
                    for inc, quoted in incs:
                        if inc.startswith("/"):
                            continue
                        if not quoted and inc.startswith(".."):
                            # Angle includes with a ../ escape are resolved
                            # by gcc ONLY through -I roots (the including
                            # file's dir is not searched for <...>). The
                            # sandbox needs an -I root whose parent is
                            # modsrc so '../X' lands on modsrc/X: the drv
                            # adds -I$PWD/modsrc/<dir>/.. when this flag
                            # is set (the a/b probe only serves two-level
                            # escapes: a/b/../../ == modsrc).
                            m["parentDirInclude"] = True
                        target = os.path.normpath(os.path.join(fdir, inc))
                        if os.path.exists(os.path.join(self.srcdir, target)):
                            if (target != base
                                    and not target.startswith(base + os.sep)):
                                td = os.path.dirname(target)
                                if td and td not in seen_dirs:
                                    seen_dirs.add(td)
                            # enqueue the reached header so its own
                            # includes are followed as well
                            files_nxt.append(target)
                        elif not quoted or "/" not in inc:
                            # Unresolved bare-name or angle include:
                            # resolved via the collected -I$(srctree)
                            # include roots. For ANGLE includes carrying
                            # an explicit ../ escape the including file's
                            # dir is NOT a gcc search root (<...> ignores
                            # it); in-tree they resolve through the
                            # kbuild global -I$(srctree)/include root
                            # (mtip32xx: <../drivers/ata/ahci.h> joins to
                            # drivers/ata/ahci.h), and the pruned
                            # compile-time source may lack the target.
                            # Probe the module dir and the global include
                            # root too, and record the JOINED dir (a ../
                            # join lands OUTSIDE the probed root).
                            roots = list(candidates)
                            if not quoted and inc.startswith(".."):
                                roots += [base, "include"]
                            for c in roots:
                                t2 = os.path.normpath(
                                    os.path.join(c, inc))
                                if os.path.exists(
                                        os.path.join(self.srcdir, t2)):
                                    if inc.startswith(".."):
                                        # ../ escapes the probed root; the
                                        # file's canonical dir is what
                                        # modsrc must carry. Sandbox -I
                                        # resolution goes through the
                                        # modsrc-rooted probe dirs
                                        # (a/b/.. == modsrc), not through
                                        # an -I entry for this dir.
                                        td = os.path.dirname(t2)
                                    else:
                                        # the root doubles as the sandbox
                                        # -I entry: -I<dir> + <inc> hits
                                        # the copied file.
                                        td = c
                                    if td and td not in seen_dirs:
                                        seen_dirs.add(td)
                                    if td == base:
                                        # The -I root IS the module dir
                                        # itself (qtnfmac: ccflags-y +=
                                        # -Idrivers/.../qtnfmac and a
                                        # pcie/ source bare-includes
                                        # shm_ipc.h from the dir root).
                                        # modsrc carries the dir, but no
                                        # extraDir -I is emitted for it
                                        # (extraDirs excludes base) and
                                        # the unit's own -I<tree path>
                                        # resolves against the pruned
                                        # core tree CWD - so the drv must
                                        # place -I$PWD/modsrc/<dir> first.
                                        m["ownDirIncludeFirst"] = True
                                    files_nxt.append(t2)
                                    break
                file_frontier = files_nxt
                if not file_frontier:
                    break
            # Object tokens may escape the module dir via ../ paths
            # (kvm-y := $(KVM)/kvm_main.o from virt/kvm/Makefile.kvm): the
            # escaped sources must be staged into modsrc at their tree
            # paths so the M= build's relative ../../../ references
            # resolve to copied files.
            for s in m.get("sources") or [ ]:
                sd = os.path.dirname(s)
                if sd and not (sd == m["dir"] or sd.startswith(m["dir"] + os.sep)):
                    seen_dirs.add(sd)
            m["extraDirs"] = sorted(seen_dirs - {base})
            # Option A: the full transitive include closure (file level) is
            # needed to decide which host patches a module must apply.
            m["closureFiles"] = sorted(scanned_files)


    # ---------- Option A: patch localization ----------

    DIFFGIT_RE = re.compile(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$')
    MINUS_RE = re.compile(r'^--- (?:"?a/(.+?)"?|/dev/null)(?:\t|$)')
    PLUS_RE = re.compile(r'^\+\+\+ (?:"?b/(.+?)"?|/dev/null)(?:\t|$)')
    BINARY_RE = re.compile(r'^Binary files .* and (?:"?b/(.+?)"?) differ$')

    def analyze_patches(self, patches):
        """Patch localization v2: every compile unit (module or built-in
        directory) applies its own applicable patch subset; nothing builds
        a full tree.

        prepare-relevant -> ALSO applied in the shared prepare tree (the
        modules_prepare output: scripts, generated headers). That is: the
        root Makefile/Kbuild, everything under scripts/, and the
        transitive include closure of the prepare-time generated-header
        entry files (asm-offsets.c, bounds.c). These files feed every
        compilation by construction, so carrying them in the prepare
        baseline is correct - a change there legitimately re-hashes the
        units that compile against them.
        everything else   -> reaches units ONLY through their own sets.

        Applicability is file/dir based: a unit gets a patch when a
        touched path is one of its sources, sits in its include closure,
        lies under its dir, or lies in one of its extraDirs. Kconfig
        patches additionally reach every unit whose dir lies under the
        Kconfig's dir (the unit's config symbols resolve only against the
        patched Kconfig hierarchy). Creator dependencies (patch B modifies
        a file patch A creates) are resolved into the same set
        transitively, in patch-list order.

        copyTree marks units whose patch set cannot land in the cheap
        modsrc scratch:
          * a NON-prepare patch touches paths outside the unit's
            dir/extraDirs - the shared prepare tree does not carry them,
            and kbuild's include search order makes a modsrc overlay lose
            to srctree for angle includes, so the unit must compile with
            srctree = its own patched copy of the tree;
          * a Kconfig patch is in the set - the unit must re-run
            syncconfig against its own patched Kconfigs (the prepare
            tree's config deliberately drops symbols whose Kconfig the
            prepare tree does not carry);
          * the unit's dir (or its build file) is patch-created - there is
            nothing upstream to seed the scratch from.
        copyTree units reflink-copy the prepared build+source trees and
        apply their set there (srctree = the copy). Built-in units always
        run in a copy (their targets build in-tree, which needs a writable
        objtree).
        """
        infos = []
        for p in patches:
            name, path = p["name"], p["file"]
            if not path:
                continue
            try:
                with open(path, "r", errors="replace") as f:
                    text = f.read()
            except OSError:
                self.warnings.append(f"patch {name}: unreadable {path}; ignoring")
                continue
            touched = set()
            creates = set()
            pending_a = "unset"  # None = `--- /dev/null` seen (create)
            for line in text.splitlines():
                m = self.DIFFGIT_RE.match(line)
                if m:
                    touched.update((m.group(1), m.group(2)))
                    pending_a = "unset"
                    continue
                m = self.MINUS_RE.match(line)
                if m:
                    pending_a = None if m.group(1) is None else m.group(1)
                    if pending_a is not None:
                        touched.add(pending_a)
                    continue
                m = self.PLUS_RE.match(line)
                if m:
                    tgt = m.group(1)
                    if tgt is not None:
                        touched.add(tgt)
                        # modification keeps a == b; anything else (create
                        # via /dev/null, rename via a/old b/new) is a new
                        # file for the unpatched tree
                        if pending_a != tgt:
                            creates.add(tgt)
                    pending_a = "unset"
                    continue
                m = self.BINARY_RE.match(line)
                if m:
                    touched.add(m.group(1))
                    creates.add(m.group(1))
            infos.append({
                "name": name,
                "file": path,
                "touched": sorted(touched),
                "creates": sorted(creates),
            })

        # creator dependencies (list order = patch application order)
        created_by = {}
        for p in infos:
            for c in p["creates"]:
                created_by.setdefault(c, p["name"])
        for p in infos:
            p["creatorDeps"] = sorted({
                created_by[t]
                for t in p["touched"]
                if t in created_by and created_by[t] != p["name"]
            })

        # prepare vs localized: see the docstring. The prepare-time include
        # closure is computed over the same include-resolution rules as
        # detect_extra_dirs, seeded from the generated-header entry files.
        arch_dirs = [self.arch]
        if self.arch in ("x86_64", "i386", "i686"):
            arch_dirs.append("x86")
        prepare_closure = set()
        for a in arch_dirs:
            prepare_closure |= self.header_closure([
                os.path.join("arch", a, "kernel", "asm-offsets.c"),
                os.path.join("kernel", "bounds.c"),
            ])
        for p in infos:
            prep = []
            for t in p["touched"]:
                if t in ("Makefile", "Kbuild"):
                    prep.append(f"{t}: root build file")
                elif t == "scripts" or t.startswith("scripts" + os.sep):
                    prep.append(f"{t}: scripts")
                elif t in prepare_closure:
                    prep.append(f"{t}: prepare-time generated-header input")
            p["prepare"] = bool(prep)
            p["prepareReasons"] = prep
            if prep:
                self.warnings.append(
                    f"patch {p['name']}: prepare-relevant "
                    f"({'; '.join(prep[:3])}"
                    f"{'; ...' if len(prep) > 3 else ''})")

        # per-unit applicability
        touch_by_name = {p["name"]: set(p["touched"]) for p in infos}
        creator_deps = {p["name"]: set(p["creatorDeps"]) for p in infos}
        for m in self.modules.values():
            base = m["dir"]
            srcs = set(m.get("sources") or [])
            closure = set(m.get("closureFiles") or [])
            extras = set(m.get("extraDirs") or [])
            have = set()
            for p in infos:
                hit = False
                for t in p["touched"]:
                    if t in srcs or t in closure:
                        hit = True
                        break
                    td = os.path.dirname(t)
                    if td == base or td.startswith(base + os.sep):
                        hit = True
                        break
                    if td in extras:
                        hit = True
                        break
                    if (os.path.basename(t).startswith("Kconfig")
                            and (td == "" or base.startswith(td + os.sep))):
                        # Kconfig in an ancestor dir: the unit's config
                        # symbols resolve only against the patched hierarchy
                        hit = True
                        break
                if hit:
                    have.add(p["name"])
            # creator-dep transitive closure
            changed = True
            while changed:
                changed = False
                for nm in list(have):
                    for dn in creator_deps.get(nm, ()):
                        if dn not in have:
                            have.add(dn)
                            changed = True
            m["patches"] = sorted(have)

        # copyTree: which units cannot use the cheap modsrc scratch (see the
        # method docstring). Built-in units always run in a copy.
        patch_by_name = {p["name"]: p for p in infos}
        creates_all = set()
        for p in infos:
            creates_all.update(p["creates"])
        for m in self.modules.values():
            base = m["dir"]
            extras = set(m.get("extraDirs") or [])
            have = set(m["patches"])
            reasons = []
            for nm in have:
                pinfo = patch_by_name[nm]
                for t in touch_by_name[nm]:
                    td = os.path.dirname(t)
                    if td == base or td.startswith(base + os.sep) or td in extras:
                        continue
                    if not pinfo["prepare"]:
                        # a prepare patch reaches the unit through the
                        # prepare tree (its modsrc hunks fail harmlessly);
                        # anything else must come from the unit's own copy
                        reasons.append(f"{nm}: {t} outside modsrc coverage")
                if any(os.path.basename(t).startswith("Kconfig")
                        for t in touch_by_name[nm]):
                    reasons.append(f"{nm}: Kconfig patch (syncconfig in copy)")
            for d2 in [x for x in (m.get("preFixDir"), base) if x]:
                if self.origsrc:
                    if not os.path.isdir(os.path.join(self.origsrc, d2)):
                        reasons.append(f"{d2}: dir created by patches")
                    elif not (
                        os.path.exists(os.path.join(self.origsrc, d2, "Makefile"))
                        or os.path.exists(os.path.join(self.origsrc, d2, "Kbuild"))
                    ):
                        reasons.append(f"{d2}: Makefile/Kbuild created by patches")
            m["copyTree"] = bool(reasons) or bool(m.get("builtin"))
            m["copyTreeReasons"] = reasons

        self.patch_info = infos
        nprep = sum(1 for p in infos if p["prepare"])
        affected = sum(1 for m in self.modules.values() if m.get("patches"))
        copied = sum(1 for m in self.modules.values() if m.get("copyTree"))
        self.warnings.append(
            f"patch analysis: {len(infos)} patches "
            f"({nprep} prepare-relevant), "
            f"{affected} units carry an applicable patch set, "
            f"{copied} units run in a private tree copy")

    def header_closure(self, entries):
        """Transitive include closure over tree-relative paths, resolving
        quoted includes relative to the including file and angle includes
        through the kernel include roots (same over-approximation
        philosophy as detect_extra_dirs)."""
        roots = ["include"]
        if self.arch == "x86_64" or self.arch == "i386" or self.arch == "i686":
            roots.append(os.path.join("arch", "x86", "include"))
        roots.append(os.path.join("arch", self.arch, "include"))
        qre = re.compile(r'#\s*include\s+"([^"]+)"')
        are = re.compile(r'#\s*include\s+<([^>]+)>')
        seen = set()
        frontier = list(entries)
        while frontier:
            rel = frontier.pop()
            if rel in seen:
                continue
            seen.add(rel)
            try:
                with open(os.path.join(self.srcdir, rel), errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            fdir = os.path.dirname(rel)
            incs = [(m.group(1), True) for m in qre.finditer(text)]
            incs += [(m.group(1), False) for m in are.finditer(text)]
            for inc, quoted in incs:
                if inc.startswith("/"):
                    continue
                target = os.path.normpath(os.path.join(fdir, inc))
                if os.path.exists(os.path.join(self.srcdir, target)):
                    frontier.append(target)
                    continue
                if not quoted or "/" not in inc:
                    for r in roots:
                        t2 = os.path.normpath(os.path.join(r, inc))
                        if os.path.exists(os.path.join(self.srcdir, t2)):
                            frontier.append(t2)
                            break
        return seen

    def scan_exports_and_deps(self):
        mod_files = [(m["key"], [os.path.join(self.srcdir, s) for s in m["sources"]]) for m in self.modules.values()]
        files = [f for _, fs in mod_files for f in fs]
        results = run_parallel(_scan_file, files, 16)
        sym2mods = {}
        idx = 0
        for key, fs in mod_files:
            for f in fs:
                for sym in results[idx]:
                    sym2mods.setdefault(sym, []).append(key)
                idx += 1
        keys = [k for k, _ in mod_files]
        refs = run_parallel(_scan_refs, [fs for _, fs in mod_files], 8)
        exported = set(sym2mods.keys())
        # Noise filter: an exported symbol referenced by an implausible
        # fraction of modules is a name collision (e.g. ib_core exports a
        # symbol literally named "func" from a macro artifact), not a
        # dependency. Dropping those edges only reduces over-approximation;
        # under-approximation is confined to symbols that genuinely appear
        # in nearly every module, which are meaningless as edges anyway.
        threshold = max(10, len(keys) // 50)
        ref_count = {}
        for tokens in refs:
            for t in tokens & exported:
                ref_count[t] = ref_count.get(t, 0) + 1
        noisy = {t for t, c in ref_count.items() if c > threshold}
        if noisy:
            self.warnings.append(
                f"symbol noise filter: dropped {len(noisy)} colliding symbols "
                f"(referenced by more than {threshold} modules)"
            )
        effective = exported - noisy
        for key, tokens in zip(keys, refs):
            deps = set()
            for t in tokens & effective:
                for mk in sym2mods[t]:
                    if mk != key:
                        deps.add(mk)
            self.modules[key]["deps"] = sorted(deps)
        self.noisySymbols = sorted(noisy)
        self.prune_cycles()

    def prune_cycles(self):
        """The per-module fixpoint in default.nix interpolates dep *store
        paths* into each compile drv's env, so the dep graph must be a DAG:
        a cycle makes drv construction infinitely recursive (the 5-module
        smoke subset was acyclic by luck; the full 7290-module set is not).

        Eval-time granularity is the module NAME, not the map key: the
        fixpoint keys drvs by name (for duplicate registrations of one
        name the last twin wins) and resolves each dep key with
        self.${dep} - a plain key is a name and resolves, a name@dir key
        does not resolve at all (null, dropped). The graph that must be
        a DAG is the projection of dep edges onto names.

        Two classes of cycle exist in that projection and both are pruned:
        - name self-edges: a dep key whose name half equals the depender's
          own name (duplicate registrations of one module name). Key-level
          these are edges between distinct twins; name-level they are
          self-references and recurse on their own.
        - real multi-node SCCs in the name graph (e.g. nfsv3 <-> nfsv4,
          usbhid <-> hid). Broken deterministically by registration
          order: within an SCC only edges from a lower-order to a strictly
          higher-order name are kept.

        Cost: dropped edges only narrow the module-level tier's
        KBUILD_EXTRA_SYMBOLS, which is best-effort by design (modpost warns,
        symbols resolve at insmod, the authoritative symvers is the
        aggregate's global modpost). The aggregate and the image are
        unaffected. Unresolvable name@dir dep keys stay in each module's
        dep list (consumers drop them); they cannot contribute cycles."""
        def name_of(k):
            return k.split("@", 1)[0]

        # built-in units are tier-inert: they never consume dep artifacts,
        # so they carry no edges. This must happen BEFORE the graph walk:
        # their keys contain "@" and DO resolve at eval (the fixpoint attr
        # key is the full "builtin@<dir>" name), so treating them like the
        # inert "name@dir" twin keys would leave builtin<->module cycles
        # alive in the output graph.
        for m in self.modules.values():
            if m.get("builtin"):
                m["deps"] = []

        # deterministic name order = first registration
        names = {}
        for key in self.module_order:
            nm = self.modules[key]["name"]
            if nm not in names:
                names[nm] = len(names)

        # name-level graph: union of resolvable dep edges over twins
        graph = {nm: set() for nm in names}
        self_edges = []
        for key, m in self.modules.items():
            nm = m["name"]
            for d in m["deps"]:
                if "@" in d:
                    continue  # self."name@dir" is null in the fixpoint
                dn = name_of(d)
                if dn == nm:
                    self_edges.append((key, d))
                    continue
                if dn in names:
                    graph[nm].add(dn)
        if self_edges:
            self.warnings.append(
                f"cycle prune: dropped {len(self_edges)} name self-edges "
                "(duplicate module registrations): "
                + ", ".join(f"{a} -> {b}" for a, b in self_edges)
            )

        # iterative Tarjan SCC over the name graph
        idx, low = {}, {}
        on = set()
        stack = []
        counter = 0
        sccs = []
        for start in graph:
            if start in idx:
                continue
            work = [(start, iter(sorted(graph[start])))]
            idx[start] = low[start] = counter
            counter += 1
            stack.append(start)
            on.add(start)
            while work:
                v, it = work[-1]
                advanced = False
                for w in it:
                    if w not in idx:
                        idx[w] = low[w] = counter
                        counter += 1
                        stack.append(w)
                        on.add(w)
                        work.append((w, iter(sorted(graph[w]))))
                        advanced = True
                        break
                    elif w in on and idx[w] < low[v]:
                        low[v] = idx[w]
                if advanced:
                    continue
                work.pop()
                if low[v] == idx[v]:
                    scc = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        scc.append(w)
                        if w == v:
                            break
                    sccs.append(scc)
                if work:
                    pv = work[-1][0]
                    if low[v] < low[pv]:
                        low[pv] = low[v]

        scc_of = {}
        for i, scc in enumerate(sccs):
            for nm in scc:
                scc_of[nm] = i

        # break each multi-node SCC by name registration order
        dropped = []
        for nm, edges in graph.items():
            for d in sorted(edges):
                if scc_of[nm] == scc_of[d] and names[d] <= names[nm]:
                    dropped.append((nm, d))
        if dropped:
            drop_set = set(dropped)
            for nm in graph:
                graph[nm] = {d for d in graph[nm] if (nm, d) not in drop_set}
            self.warnings.append(
                f"cycle prune: broke {len({scc_of[a] for a, _ in dropped})} SCC(s) "
                f"by registration order, dropped {len(dropped)} name edges: "
                + ", ".join(f"{a} <- {b}" for a, b in dropped)
            )

        # write back per-key dep lists
        for key, m in self.modules.items():
            nm = m["name"]
            kept = []
            for d in m["deps"]:
                if "@" in d:
                    kept.append(d)  # unresolvable at eval; inert
                    continue
                dn = name_of(d)
                if dn != nm and dn in graph[nm]:
                    kept.append(d)
            m["deps"] = kept

    def drop_build_tree_bound_modules(self):
        """kheaders (IKHEADERS) embeds the kernel headers into
        kheaders_data.tar.xz; kernel/Makefile's kheaders-objlist target
        runs 'find include/generated ...' - a build-tree path that does
        not resolve in an isolated M= sandbox (the single-target rule
        runs it under the modsrc dir). No modsrc-only build can produce
        correct data. Drop such modules from the tier; the final
        aggregate's make builds missing artifacts naturally in the real
        build tree (which has include/generated)."""
        drop = {"kheaders"}
        dropped = [k for k, m in self.modules.items()
                   if m["name"] in drop or m["name"].split("@")[0] in drop]
        for k in dropped:
            del self.modules[k]
        if dropped:
            self.warnings.append(
                f"dropped build-tree-bound modules: {sorted(dropped)}")

    def run(self, patches=None):
        self.process_file("", "Makefile")
        if os.path.exists(os.path.join(self.srcdir, "Kbuild")):
            # root has two kbuild files; bypass the per-dir visited guard once
            saved = set(self.visited)
            self.visited.discard("")
            self.process_file("", "Kbuild")
            self.visited |= saved
        else:
            self.warnings.append("root: no Kbuild file")
        self.resolve_sources()
        self.fix_module_dirs()
        self.detect_extra_dirs()
        if patches:
            self.analyze_patches(patches)
            # closureFiles is internal to classification - never serialized
            # (7941 file lists would bloat the JSON the evaluator imports).
            for m in self.modules.values():
                m.pop("closureFiles", None)
        self.scan_exports_and_deps()
        self.prune_cycles()
        self.drop_build_tree_bound_modules()


def run_parallel(func, items, chunksize):
    """ProcessPoolExecutor with a serial fallback. Some sandboxed
    environments (and re-imported __main__ scripts) cannot spawn workers."""
    try:
        with ProcessPoolExecutor() as ex:
            return list(ex.map(func, items, chunksize=chunksize))
    except Exception as e:
        print(f"mapper: parallel scan unavailable ({e!r}); running serial", file=sys.stderr)
        return [func(i) for i in items]


def _scan_file(path):
    syms = []
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return syms
    return [m.group(1) for m in EXPORT_RE.finditer(text)]


def _scan_refs(paths):
    tokens = set()
    for path in paths:
        try:
            with open(path, "r", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        tokens.update(TOKEN_RE.findall(text))
    return tokens


def main():
    srcdir, configfile, arch, outjson, outwarn = sys.argv[1:6]
    # argv[6] is the patch manifest JSON text (passed via an env var),
    # not a file path.
    patches = json.loads(sys.argv[6]) if len(sys.argv) > 6 else []
    # entries without a patch file (config-only entries like
    # ec-qcom-hamoa-enable) carry nothing to classify or apply
    patches = [p for p in patches if p.get("file")]
    # argv[7] (optional): the UNPATCHED upstream tree - used by copyTree
    # detection (units whose dirs/build files the patches create).
    origsrc = sys.argv[7] if len(sys.argv) > 7 else None
    cfg, unset = read_config(configfile)
    mapper = Mapper(srcdir, cfg, unset, arch, origsrc=origsrc)
    mapper.run(patches)
    nprep = sum(1 for p in mapper.patch_info if p["prepare"])
    nbuiltin = sum(1 for m in mapper.modules.values() if m.get("builtin"))
    ncopied = sum(1 for m in mapper.modules.values() if m.get("copyTree"))
    stats = {
        "arch": arch,
        "units": len(mapper.modules),
        "builtins": nbuiltin,
        "copyTree": ncopied,
        "warnings": len(mapper.warnings),
        "absentConfigSymbols": len(mapper.absent),
        "patches": len(mapper.patch_info),
        "preparePatches": nprep,
    }
    out = {
        "stats": stats,
        "order": mapper.module_order,
        "modules": list(mapper.modules.values()),
        "noisySymbols": getattr(mapper, "noisySymbols", []),
        "patches": mapper.patch_info,
    }
    with open(outjson, "w") as f:
        json.dump(out, f, indent=1)
    with open(outwarn, "w") as f:
        f.write("\n".join(mapper.warnings) + "\n")
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
