"""FreeType's TrueType glyph loader (truetype/ttgload.c, ttobjs.c, 2.14.3) as PDFium drives it.

CFX_Face::RenderGlyph loads an SFNT glyph *hinted* (FT_LOAD_NO_BITMAP | FT_LOAD_PEDANTIC) at 64 ppem
under FT_Set_Transform, and when that load fails it loads it again with FT_LOAD_NO_HINTING;
LoadGlyphPath loads it unhinted. So `TrueTypeFace.outline` runs the font's programs (`ttinterp`):
fpgm once, prep once per size reset, then each glyph's instructions, in v40 backward compatibility
mode; `path` does not.

State that outlives a glyph load in FreeType and could make a glyph depend on the loads before it
is handled rather than guessed: the twilight zone (prep zeroes it, glyph programs may write it) is
kept as FreeType keeps it, and a load that starts from a twilight other than prep's is also run from
prep's; if the two outlines differ the glyph is refused. Refused (`Unported`): variable fonts,
colour/bitmap-only fonts, tricky fonts (FreeType hints them even when asked not to), a prep that
sets INSTCTRL bit 2 (it resets the saved graphics state on every load), overlapping-outline flags
(FreeType renders those with its overlap-aware rasteriser), scaled component offsets, and opcodes
FreeType leaves undefined but whose stack effect it still applies.
"""
from __future__ import annotations

import struct

from .ftoutline import GlyphError, Unported, _glyph_path, divfix, i32, mulfix
from . import ttinterp as T

TRICKY_NAMES = ("cpop", "DFGirl-W6-WIN-BF", "DFGothic-EB", "DFGyoSho-Lt", "DFHei", "DFHSGothic-W5",
                "DFHSMincho-W3", "DFHSMincho-W7", "DFKaiSho-SB", "DFKaiShu", "DFKai-SB", "DFMing", "DLC",
                "HuaTianKaiTi?", "HuaTianSongTi?", "Ming(for ISO10646)", "MingLiU", "MingMedium",
                "PMingLiU", "MingLi43")

TRICKY_IDS = (  # tt_check_trickyness_sfnt_ids: (checksum, length) of cvt, fpgm, prep
    ((0x05BCF058, 0x2E4), (0x28233BF1, 0x87C4), (0xA344A1EA, 0x1E1)),
    ((0x05BCF058, 0x2E4), (0x28233BF1, 0x87C4), (0xA344A1EB, 0x1E1)),
    ((0x12C3EBB2, 0x350), (0xB680EE64, 0x87A7), (0xCE939563, 0x758)),
    ((0x11E5EAD4, 0x350), (0xCE5956E9, 0xBC85), (0x8272F416, 0x45)),
    ((0x1257EB46, 0x350), (0xF699D160, 0x715F), (0xD222F568, 0x3BC)),
    ((0x1262EB4E, 0x350), (0xE86A5D64, 0x7940), (0x7850F729, 0x5FF)),
    ((0x122DEB0A, 0x350), (0x3D16328A, 0x859B), (0xA93FC33B, 0x2CB)),
    ((0x125FEB26, 0x350), (0xA5ACC982, 0x7EE1), (0x90999196, 0x41F)),
    ((0x11E5EAD4, 0x350), (0x5A30CA3B, 0x9063), (0x13A42602, 0x7E)),
    ((0x11E5EAD4, 0x350), (0xA6E78C01, 0x8998), (0x13A42602, 0x7E)),
    ((0x11E5EAD4, 0x360), (0x9DB282B2, 0xC06E), (0x53E6D7CA, 0x82)),
    ((0x1243EB18, 0x350), (0xBA0A8C30, 0x74AD), (0xF3D83409, 0x37B)),
    ((0x07DCF546, 0x308), (0x40FE7C90, 0x8E2A), (0x608174B5, 0x7A)),
    ((0xEB891238, 0x308), (0xD2E4DCD4, 0x676F), (0x8EA5F293, 0x3B8)),
    ((0xFFFBFFFC, 0x8), (0x9C9E48B8, 0xBEA2), (0x70020112, 0x8)),
    ((0xFFFBFFFC, 0x8), (0x0A5A0483, 0x17C39), (0x70020112, 0x8)),
    ((0, 0), (0x40C92555, 0xE5), (0xA39B58E3, 0x117C)),
    ((0, 0), (0x33C41652, 0xE5), (0x26D6C52A, 0xF6A)),
    ((0, 0), (0x6DB1651D, 0x19D), (0x6C6E4B03, 0x2492)),
    ((0, 0), (0x40C92555, 0xE5), (0xDE51FAD0, 0x117C)),
    ((0, 0), (0x85E47664, 0xE5), (0xA6C62831, 0x1CAA)),
    ((0, 0), (0x2D891CFD, 0x19D), (0xA0604633, 0x1DE8)),
    ((0, 0), (0x40AA774C, 0x1CB), (0x9B5CAA96, 0x1F9A)),
    ((0, 0), (0x0D3DE9CB, 0x141), (0xD4127766, 0x2280)),
    ((0, 0), (0x4A692698, 0x1F0), (0x340D4346, 0x1FCA)),
    ((0, 0), (0xCD34C604, 0x166), (0x6CF31046, 0x22B0)),
    ((0, 0), (0x5DA75315, 0x19D), (0x40745A5F, 0x22E0)),
    ((0, 0), (0xF055FC48, 0x1C2), (0x3900DED3, 0x1E18)),
    ((0x00170003, 0x60), (0xDBB4306E, 0x58AA), (0xD643482A, 0x35)),
    ((0x1269EB58, 0x350), (0x5CD5957A, 0x6A4E), (0xF758323A, 0x380)),
    ((0x122FEB0B, 0x350), (0x7F10919A, 0x70A9), (0x7CD7E7B7, 0x25C)))

ARGS_ARE_WORDS, ARGS_ARE_XY, ROUND_XY, HAVE_SCALE = 0x1, 0x2, 0x4, 0x8
MORE, XY_SCALE, TWO_BY_TWO, HAVE_INSTR, USE_MY_METRICS = 0x20, 0x40, 0x80, 0x100, 0x200
OVERLAP_COMPOUND, SCALED_OFFSET = 0x400, 0x800


def _checksum(b: bytes) -> int:
    s = 0
    n = len(b) & ~3
    for (v,) in struct.iter_unpack(">I", b[:n]):
        s += v
    shift = 24
    for c in b[n:]:
        s += c << shift
        shift -= 8
    return s & 0xFFFFFFFF


def is_tricky(face, names: list[str]) -> bool:
    """tt_check_trickyness: by family name, then by the checksums of cvt, fpgm and prep."""
    for name in names:
        if len(name) > 7 and name[6] == "+" and all("A" <= c <= "Z" for c in name[:6]):
            name = name[7:]
        if any(t in name for t in TRICKY_NAMES):
            return True
    matched = [0] * len(TRICKY_IDS)
    has = [False] * 3
    for tag, offset, length in face.dir:
        k = {b"cvt ": 0, b"fpgm": 1, b"prep": 2}.get(tag)
        if k is None:
            continue
        has[k] = True
        cs = None
        for j, ids in enumerate(TRICKY_IDS):
            if length == ids[k][1]:
                if cs is None:
                    cs = _checksum(face.data[offset:offset + length]) if offset + length <= len(face.data) else 0
                if ids[k][0] == cs:
                    matched[j] += 1
                if matched[j] == 3:
                    return True
    for j, ids in enumerate(TRICKY_IDS):
        for k in range(3):
            if not has[k] and not ids[k][1]:
                matched[j] += 1
        if matched[j] == 3:
            return True
    return False


def _family_names(data: bytes, font_number: int) -> list[str]:
    try:
        import io
        from fontTools.ttLib import TTFont
        tt = TTFont(io.BytesIO(data), lazy=True, fontNumber=font_number)
        if "name" not in tt:
            return []
        return [r.toUnicode() for r in tt["name"].names if r.nameID in (1, 16, 21)]
    except Exception:
        return []


def _pix_round(v: int) -> int:
    return i32(v + 32) & -64


class _Fail(Exception):
    """A glyph load error (FT_Load_Glyph returns non-zero)."""


class TrueTypeFace:
    """An embedded glyf font at 64 ppem, with the ftoutline.Face interface render_text uses."""

    def __init__(self, font=None, program=None):
        prog = program if program is not None else font.program
        face = prog.glyphs.face
        self.face = face
        for tag in (b"fvar", b"gvar", b"HVAR", b"VVAR", b"avar", b"CBLC", b"CBDT", b"sbix"):
            if face.table(tag):
                raise Unported(f"TrueType font with a {tag.decode().strip()} table")
        if is_tricky(face, _family_names(face.data, getattr(prog, "font_number", 0))):
            raise Unported("a tricky TrueType font (FreeType always hints it)")
        self.upem = face.units_per_em
        self.scale = divfix(64 << 6, self.upem)
        self.num_glyphs = face.num_glyphs
        self._outlines: dict = {}
        self._paths: dict = {}
        self._units: dict = {}
        self.bytecode_ready = -1            # size->bytecode_ready: -1 not run, else the error
        self.cvt_ready = -1

    # -- the size: fpgm, prep
    def _init_bytecode(self) -> None:
        f = self.face
        cvt = [struct.unpack_from(">h", f.cvt, 2 * i)[0] for i in range(len(f.cvt) // 2)]
        self.face_cvt = cvt
        self.exec = e = T.Exec(f.maxp, len(cvt), self.scale, self.num_glyphs,
                               {T.FONT: f.fpgm, T.CVT: f.prep})
        n = f.maxp.get("maxTwilightPoints", 0) + 4
        self.twilight = T.Zone(n, [[0, 0] for _ in range(n)], [[0, 0] for _ in range(n)],
                               [[0, 0] for _ in range(n)], [0] * n)
        self.size_gs = T.GS()
        err = None
        if f.fpgm:
            e.load_context(self.twilight)
            err = e.run_context(T.FONT, T.Zone(), self.size_gs)
            if not err:
                self._save(e)
        self.bytecode_ready = err or 0
        self.cvt_ready = -1

    def _save(self, e) -> None:
        for k in T.GS_SAVED:
            setattr(self.size_gs, k, getattr(e.gs, k))

    def _run_prep(self) -> None:
        e = self.exec
        self.size_gs = T.GS()
        tw = self.twilight
        for i in range(tw.n_points):
            tw.org[i][:] = [0, 0]
            tw.cur[i][:] = [0, 0]
        e.load_context(tw)
        e.storage_base[:] = [0] * e.store_size
        e.cvt_base[:] = [mulfix(v, self.scale) for v in self.face_cvt]
        err = None
        if self.face.prep:
            err = e.run_context(T.CVT, T.Zone(), self.size_gs)
            if not err:
                self._save(e)
        self.cvt_ready = err or 0
        if not err and self.size_gs.instruct_control & 2:
            raise Unported("a TrueType prep that sets INSTCTRL bit 2")
        self.prep_twilight = self._twilight_state()

    def _twilight_state(self):
        tw = self.twilight
        return ([p[:] for p in tw.org], [p[:] for p in tw.cur], list(tw.tags))

    def _set_twilight(self, state) -> None:
        tw = self.twilight
        for dst, src in ((tw.org, state[0]), (tw.cur, state[1])):
            for d, s in zip(dst, src):
                d[:] = s
        tw.tags[:] = state[2]

    # -- glyph loading
    def _load(self, gid: int, hinted: bool):
        """TT_Load_Glyph: (points, tags, contours) in 26.6 before the transform. Raises _Fail."""
        self.hinted = hinted
        if hinted:
            if self.bytecode_ready < 0:
                self._init_bytecode()
            if self.bytecode_ready:
                raise _Fail(self.bytecode_ready)
            if self.cvt_ready < 0:
                self._run_prep()
            if self.cvt_ready:
                raise _Fail(self.cvt_ready)
            if self.size_gs.instruct_control & 1:
                self.hinted = False
            else:
                e = self.exec
                e.load_context(self.twilight)
                e.bc = (self.size_gs.instruct_control & 4) ^ 4
        self.pts, self.tags, self.contours = [], [], []
        self.composites: list[int] = []
        self.overlap = False
        self.pp = [[0, 0], [0, 0], [0, 0], [0, 0]]
        self._load_glyph(gid, 0)
        if self.overlap:
            raise Unported("a TrueType glyph flagged as overlapping")
        pts = self.pts
        dx = self.pp[0][0]
        if dx:
            pts = [[i32(x - dx), y] for x, y in pts]
        return pts, [t & 3 for t in self.tags], list(self.contours)

    def _load_glyph(self, gid: int, recurse: int) -> None:
        f = self.face
        if recurse > 100:
            raise _Fail("Invalid_Composite")
        if gid >= self.num_glyphs:
            raise _Fail("Invalid_Glyph_Index")
        offset, length = f.location(gid)
        if length > 0 and not f.glyf[1]:
            raise _Fail("Invalid_Table")
        data = f.data[offset:offset + length] if length else b""
        if length and len(data) < length:
            raise _Fail("Invalid_Outline")
        if length == 0:
            n_contours, bbox = 0, (0, 0, 0, 0)
        else:
            if length < 10:
                raise _Fail("Invalid_Outline")
            n_contours = struct.unpack_from(">h", data, 0)[0]
            bbox = struct.unpack_from(">4h", data, 2)
        aw, lsb = f.metrics(gid)
        tsb, ah = self._vmetrics(gid, bbox[3])
        pp1x = i32(bbox[0] - lsb)
        pp = [[pp1x, 0], [i32(pp1x + aw), 0], [0, i32(bbox[3] + tsb)], [0, 0]]
        pp[3][1] = i32(pp[2][1] - ah)
        if self.hinted:
            pp[2][0] = pp[3][0] = T.cdiv(aw, 2)
        self.pp = pp
        s = self.scale
        if length == 0 or n_contours == 0:
            for p in pp:
                p[0], p[1] = mulfix(p[0], s), mulfix(p[1], s)
            return
        if n_contours > 0:
            self._simple(data, n_contours)
        else:
            self._composite(gid, data, recurse)

    def _vmetrics(self, gid: int, ymax: int):
        f = self.face
        if f.vhea and f.vmtx:
            ah, tsb = f.metrics(gid, True)
            return tsb, ah
        os2 = f.os2
        if os2:
            tsb = T._short(os2["sTypoAscender"] - ymax)
            ah = abs(os2["sTypoAscender"] - os2["sTypoDescender"]) & 0xFFFF
            return tsb, ah
        h = f.hhea or {"ascender": 0, "descender": 0}
        return T._short(h["ascender"] - ymax), abs(h["ascender"] - h["descender"]) & 0xFFFF

    def _simple(self, data: bytes, n_contours: int) -> None:
        p, limit = 10, len(data)
        if n_contours >= 0xFFF or p + 2 * n_contours + 2 > limit:
            raise _Fail("Invalid_Outline")
        ends = list(struct.unpack_from(f">{n_contours}H", data, p))
        p += 2 * n_contours
        prev = -1
        for c in ends:
            if c < prev + 1:
                raise _Fail("Invalid_Outline")
            prev = c
        n_points = ends[-1] + 1 if ends else 0
        n_ins = struct.unpack_from(">H", data, p)[0]
        p += 2
        if p + n_ins > limit:
            raise _Fail("Too_Many_Hints")
        ins = data[p:p + n_ins]
        p += n_ins
        flags = []
        while len(flags) < n_points:
            if p + 1 > limit:
                raise _Fail("Invalid_Outline")
            c = data[p]
            p += 1
            flags.append(c)
            if c & 8:
                if p + 1 > limit:
                    raise _Fail("Invalid_Outline")
                cnt = data[p]
                p += 1
                if len(flags) + cnt > n_points:
                    raise _Fail("Invalid_Outline")
                flags.extend([c] * cnt)
        if n_points and flags[0] & 0x40:
            self.overlap = True
        coords = []
        for short, same in ((2, 0x10), (4, 0x20)):
            v, out = 0, []
            for fl in flags:
                if fl & short:
                    if p + 1 > limit:
                        raise _Fail("Invalid_Outline")
                    d = data[p]
                    p += 1
                    v += d if fl & same else -d
                elif not fl & same:
                    if p + 2 > limit:
                        raise _Fail("Invalid_Outline")
                    v += struct.unpack_from(">h", data, p)[0]
                    p += 2
                out.append(v)
            coords.append(out)
        base = len(self.pts)
        pts = [[x, y] for x, y in zip(*coords)] + [p_[:] for p_ in self.pp]
        tags = [fl & 1 for fl in flags] + [0, 0, 0, 0]
        orus = [q[:] for q in pts] if self.hinted else None
        s = self.scale
        for q in pts:
            q[0], q[1] = mulfix(q[0], s), mulfix(q[1], s)
        self.pp = [q[:] for q in pts[-4:]]
        contours = [base + c for c in ends]
        if self.hinted:
            zone = T.Zone(len(pts), None, pts, orus, tags, list(ends), 0)
            self._hint(zone, ins, False)
        self.pts.extend(pts[:n_points])
        self.tags.extend(tags[:n_points])
        self.contours.extend(contours)

    def _hint(self, zone, ins: bytes, composite: bool) -> None:
        e = self.exec
        n = zone.n_points
        if ins:
            zone.org = [q[:] for q in zone.cur]
        if composite:
            e.x_scale = 0x10000
            zone.orus = [q[:] for q in zone.cur]
        else:
            e.x_scale = self.scale
        cur = zone.cur
        cur[n - 4][0] = _pix_round(cur[n - 4][0])
        cur[n - 3][0] = _pix_round(cur[n - 3][0])
        cur[n - 2][1] = _pix_round(cur[n - 2][1])
        cur[n - 1][1] = _pix_round(cur[n - 1][1])
        if ins:
            e.codes[T.GLYPH] = ins
            e.is_composite = composite
            err = e.run_context(T.GLYPH, zone, self.size_gs)
            if err:
                raise _Fail(err)
        if e.bc:
            return
        self.pp = [q[:] for q in cur[n - 4:]]

    def _composite(self, gid: int, data: bytes, recurse: int) -> None:
        del self.composites[recurse:]
        if gid in self.composites:
            raise _Fail("Invalid_Composite")
        self.composites.append(gid)
        p, limit = 10, len(data)
        subs = []
        while True:
            if p + 4 > limit:
                raise _Fail("Invalid_Composite")
            flags, index = struct.unpack_from(">HH", data, p)
            p += 4
            if index >= self.num_glyphs:
                raise _Fail("Invalid_Composite")
            count = 4 if flags & ARGS_ARE_WORDS else 2
            count += 2 if flags & HAVE_SCALE else 4 if flags & XY_SCALE else 8 if flags & TWO_BY_TWO else 0
            if p + count > limit:
                raise _Fail("Invalid_Composite")
            if flags & ARGS_ARE_XY:
                fmt = ">hh" if flags & ARGS_ARE_WORDS else ">bb"
            else:
                fmt = ">HH" if flags & ARGS_ARE_WORDS else ">BB"
            a1, a2 = struct.unpack_from(fmt, data, p)
            p += 4 if flags & ARGS_ARE_WORDS else 2
            xx = yy = 0x10000
            xy = yx = 0
            if flags & HAVE_SCALE:
                xx = yy = struct.unpack_from(">h", data, p)[0] * 4
                p += 2
            elif flags & XY_SCALE:
                xx, yy = (v * 4 for v in struct.unpack_from(">hh", data, p))
                p += 4
            elif flags & TWO_BY_TWO:
                xx, yx, xy, yy = (v * 4 for v in struct.unpack_from(">4h", data, p))
                p += 8
            subs.append((flags, index, a1, a2, (xx, xy, yx, yy),
                         bool(flags & (HAVE_SCALE | XY_SCALE | TWO_BY_TWO))))
            if not flags & MORE:
                break
        ins_pos = p
        s = self.scale
        for q in self.pp:
            q[0], q[1] = mulfix(q[0], s), mulfix(q[1], s)
        start_point, start_contour = len(self.pts), len(self.contours)
        for flags, index, a1, a2, m, have_scale in subs:
            saved = [q[:] for q in self.pp]
            num_base = len(self.pts)
            self._load_glyph(index, recurse + 1)
            if not flags & USE_MY_METRICS:
                self.pp = saved
            if len(self.pts) > num_base:
                self._component(flags, a1, a2, m, have_scale, start_point, num_base)
        if self.hinted and subs[-1][0] & HAVE_INSTR and len(self.pts) > start_point:
            self._composite_program(data, ins_pos, start_point, start_contour)
        if subs[0][0] & OVERLAP_COMPOUND:
            self.overlap = True

    def _component(self, flags, a1, a2, m, have_scale, start_point, num_base) -> None:
        pts = self.pts
        xx, xy, yx, yy = m
        if have_scale:
            for q in pts[num_base:]:
                x, y = q
                q[0] = i32(mulfix(x, xx) + mulfix(y, xy))
                q[1] = i32(mulfix(x, yx) + mulfix(y, yy))
        if not flags & ARGS_ARE_XY:
            k, l = a1 + start_point, a2 + num_base
            if k >= num_base or l >= len(pts):
                raise _Fail("Invalid_Composite")
            x, y = i32(pts[k][0] - pts[l][0]), i32(pts[k][1] - pts[l][1])
        else:
            x, y = a1, a2
            if not x and not y:
                return
            if flags & SCALED_OFFSET and have_scale:
                raise Unported("a TrueType component offset scaled by its transform")
            x, y = mulfix(x, self.scale), mulfix(y, self.scale)
            if self.hinted and flags & ROUND_XY:
                if not self.exec.bc:
                    x = _pix_round(x)
                y = _pix_round(y)
        if x or y:
            for q in pts[num_base:]:
                q[0], q[1] = i32(q[0] + x), i32(q[1] + y)

    def _composite_program(self, data, ins_pos, start_point, start_contour) -> None:
        if ins_pos + 2 > len(data):
            raise _Fail("Invalid_Composite")
        n_ins = struct.unpack_from(">H", data, ins_pos)[0]
        if n_ins == 0:
            return
        if n_ins > len(data) - ins_pos - 2:
            raise _Fail("Too_Many_Hints")
        ins = data[ins_pos + 2:ins_pos + 2 + n_ins]
        cur = self.pts[start_point:] + [q[:] for q in self.pp]
        tags = self.tags[start_point:] + [0, 0, 0, 0]
        n = len(cur) - 4
        for i in range(n):
            tags[i] &= ~(T.TOUCH_X | T.TOUCH_Y)
        zone = T.Zone(len(cur), None, cur, None, tags, self.contours[start_contour:], start_point)
        self._hint(zone, ins, True)
        self.tags[start_point:] = tags[:n]

    # -- the products
    def _hinted_outline(self, gid: int):
        before = self._twilight_state() if self.bytecode_ready == 0 and self.cvt_ready == 0 else None
        try:
            got = self._load(gid, True)
        except _Fail:
            got = None
        if before is not None and before != self.prep_twilight:
            after = self._twilight_state()
            self._set_twilight(self.prep_twilight)
            try:
                fresh = self._load(gid, True)
            except _Fail:
                fresh = None
            self._set_twilight(after)
            if fresh != got:
                raise Unported("a TrueType glyph whose hinting depends on the glyphs loaded before it")
        if got is None:
            try:
                got = self._load(gid, False)
            except _Fail:
                return None
        return got

    def outline(self, glyph: int, matrix: tuple[int, int, int, int]):
        """RenderGlyph's FT_Load_Glyph under FT_Set_Transform(matrix): contours [(points, tags)]."""
        key = (glyph, matrix)
        if key in self._outlines:
            return self._outlines[key]
        got = self._hinted_outline(glyph)
        out = None if got is None else _contours(got, matrix)
        self._outlines[key] = out
        return out

    def unhinted(self, glyph: int, matrix: tuple[int, int, int, int] = (0x10000, 0, 0, 0x10000)):
        """LoadGlyphPath's load: FT_Set_Pixel_Sizes (a size reset: prep runs again before the next
        hinted load), FT_Set_Transform(matrix) (a substitute's skew), then an unhinted load."""
        self.cvt_ready = -1 if self.bytecode_ready == 0 else self.cvt_ready
        try:
            return _contours(self._load(glyph, False), matrix)
        except _Fail:
            return None

    def path(self, glyph: int, matrix: tuple[int, int, int, int] = (0x10000, 0, 0, 0x10000)):
        """CFX_Face::LoadGlyphPath: [(x, y, kind, close)] in em units, from the unhinted outline."""
        key = (glyph, matrix)
        if key in self._paths:
            self.cvt_ready = -1 if self.bytecode_ready == 0 else self.cvt_ready
            return self._paths[key]
        out = self.unhinted(glyph, matrix)
        path = None if out is None else _glyph_path(out)
        self._paths[key] = path
        return path


def _contours(got, matrix):
    pts, tags, contours = got
    xx, xy, yx, yy = matrix
    identity = matrix == (0x10000, 0, 0, 0x10000)
    out, start = [], 0
    for end in contours:
        sp = []
        for x, y in pts[start:end + 1]:
            if not identity:
                x, y = i32(mulfix(x, xx) + mulfix(y, xy)), i32(mulfix(x, yx) + mulfix(y, yy))
            sp.append((x, y))
        out.append((sp, tags[start:end + 1]))
        start = end + 1
    return out
