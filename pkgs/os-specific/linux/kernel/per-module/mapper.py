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
            return "n"
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
    # `ifdef CONFIG_X` is true whenever the symbol is mentioned in .config,
    # including "=n" (kconfig exports =n symbols into auto.conf).
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
        self.obj_lists = {}  # stem -> [objects] from foo-y / foo-objs / foo-m
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
            defined = (name.startswith("CONFIG_") and (name in cfg or name in unset)) or (
                name in self.env and self.env[name]
            )
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
    def __init__(self, srcdir, cfg, unset, arch):
        self.srcdir = srcdir
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

    def process_file(self, dirpath, fname):
        real = dirpath if dirpath == "" else os.path.normpath(dirpath)
        path = os.path.join(self.srcdir, real, fname) if fname else os.path.join(self.srcdir, real)
        fkey = (real, fname)
        if fkey in self.processed:
            return
        try:
            with open(path, "r", errors="replace") as f:
                text = f.read()
        except OSError:
            self.warnings.append(f"{real}: missing kbuild file {fname}")
            return
        self.processed.add(fkey)
        d = Dir(real)
        d.env["SRCARCH"] = self.arch
        d.env["ARCH"] = self.arch
        d.env["srctree"] = "."
        d.env["src"] = real
        d.env["obj"] = real
        d.env.update(self.global_env)

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
                    if base in ("Makefile", "Kbuild") or base.endswith(".mk"):
                        incdir = os.path.dirname(incx)
                        if incdir and not incx.startswith("/"):
                            incdir = os.path.normpath(os.path.join(real, incdir))
                        elif not incdir:
                            incdir = real
                        self.process_file(incdir, base)
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
                # Conservative: obj lines with unresolvable LHS still count
                # toward obj-y/obj-m with the resolvable tokens.
                if re.match(r"^obj-", lhs.strip()):
                    for tok in rhs.split():
                        try:
                            tx = expand(tok, Ctx(self.cfg, d.env, self.unset, self.warnings, where, self.absent))
                        except Unresolved:
                            continue
                        d.obj_y.append(tx)
                        d.obj_m.append(tx)
                    self.warnings.append(f"{where}: unresolvable obj assignment '{lhs}' (treating as y and m)")
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
                if suffix in ("", "n") and not dirs:
                    # e.g. netfilter-$(CONFIG_X) with X=n/unset: no-op
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
                    d.obj_lists.setdefault(stem, []).extend(objs)
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

        # process collected entries
        self.harvest(d, real)
        # remember the root Makefile env as global (ARCH_CORE, libs-y, ... are
        # consumed by the root Kbuild and arch Makefiles)
        if real == "" and fname == "Makefile":
            self.global_env = dict(d.env)

    def harvest(self, d, real):
        seen = set()
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

    def register_module(self, stem, d, real):
        objects = []
        lst = d.obj_lists.get(stem)
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
                    self.warnings.append(f"{m['dir']}: no source found for object {obj}")
            m["sources"] = srcs

    def detect_extra_dirs(self):
        """Modules may include headers outside their own directory via
        relative paths (e.g. 8250.h including ../serial_mctrl_gpio.h). The
        compile drv only copies the module dir, so record every in-tree
        directory whose files must also be copied for the includes to
        resolve. Walk breadth-first from the module dir: files in a newly
        discovered dir may themselves escape (capped for safety).
        Over-approximation is safe (extra copied sources).

        Bare-name quoted includes (e.g. au8522_priv.h including
        "tuner-i2c.h") are resolved by the kernel build via the module's
        own Makefile flags (-I$(srctree)/...). In an M= build those flags
        still name srctree paths, and the srctree is the pruned dev tree,
        so the referenced directory must be copied into modsrc and made
        reachable again (the drvs add KCFLAGS -I entries pointing at the
        modsrc copies). Detect that class here: a quoted include that does
        not resolve relative to the scanning dir is looked up by basename
        inside the module Makefile's -I$(srctree) directories."""
        inc_re = re.compile(r'#\s*include\s+"([^"]+)"')
        # Cross-tree include roots the module's own Makefile/Kbuild names.
        # These are the only resolution candidates an M= build has for
        # bare-name quoted includes beyond the module dir itself.
        cc_re = re.compile(r'-I\$\(srctree\)/?([^\s\\]+)')
        max_depth = 4
        for m in self.modules.values():
            base = m["dir"]
            seen_dirs = {base}
            frontier = [base]
            candidates = []
            for mk in ("Makefile", "Kbuild"):
                try:
                    with open(os.path.join(self.srcdir, base, mk), errors="replace") as f:
                        mktext = f.read()
                except OSError:
                    continue
                for mm in cc_re.finditer(mktext):
                    cdir = os.path.normpath(mm.group(1).rstrip("/\\"))
                    if (cdir and cdir not in candidates
                            and os.path.isdir(os.path.join(self.srcdir, cdir))):
                        candidates.append(cdir)
            for _ in range(max_depth):
                nxt = []
                for d in frontier:
                    dpath = os.path.join(self.srcdir, d)
                    try:
                        names = os.listdir(dpath)
                    except OSError:
                        continue
                    for fn in names:
                        if not fn.endswith((".c", ".h", ".S", ".rs", ".i", ".s")):
                            continue
                        rel = os.path.normpath(os.path.join(d, fn))
                        try:
                            with open(os.path.join(self.srcdir, rel), errors="replace") as f:
                                text = f.read()
                        except OSError:
                            continue
                        for mm in inc_re.finditer(text):
                            inc = mm.group(1)
                            if inc.startswith("/"):
                                continue
                            target = os.path.normpath(os.path.join(d, inc))
                            if os.path.exists(os.path.join(self.srcdir, target)):
                                if target == base or target.startswith(base + os.sep):
                                    continue
                                td = os.path.dirname(target)
                            elif "/" not in inc:
                                # Bare name: resolved via the module
                                # Makefile's -I$(srctree) include roots.
                                td = next(
                                    (cd for cd in candidates
                                     if os.path.exists(
                                         os.path.join(self.srcdir, cd, inc))),
                                    None,
                                )
                            else:
                                td = None
                            # Only copy directories for includes that resolve
                            # to an existing file in the source tree; quoted
                            # includes that exist nowhere (generated headers,
                            # asm/... resolved via -I) need no copy.
                            if not td or td in seen_dirs:
                                continue
                            seen_dirs.add(td)
                            nxt.append(td)
                if not nxt:
                    break
                frontier = nxt
            m["extraDirs"] = sorted(seen_dirs - {base})

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

    def run(self):
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
        self.detect_extra_dirs()
        self.scan_exports_and_deps()
        self.prune_cycles()


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
    cfg, unset = read_config(configfile)
    mapper = Mapper(srcdir, cfg, unset, arch)
    mapper.run()
    stats = {
        "arch": arch,
        "modules": len(mapper.modules),
        "warnings": len(mapper.warnings),
        "absentConfigSymbols": len(mapper.absent),
    }
    out = {
        "stats": stats,
        "order": mapper.module_order,
        "modules": list(mapper.modules.values()),
        "noisySymbols": getattr(mapper, "noisySymbols", []),
    }
    with open(outjson, "w") as f:
        json.dump(out, f, indent=1)
    with open(outwarn, "w") as f:
        f.write("\n".join(mapper.warnings) + "\n")
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
