#!/usr/bin/env python3
"""
Legacy Windows PostScript Preprocessor (Win 3.1 Win35Dict + Win 9x/ME PSCRIPT 4.0)
- 선행 시리얼/모뎀 노이즈(ATE0Q0V1 등), 공백, EOT(0x04) 제거 및 다중 잡 자동 분할
- Win 3.1: save/1 setcachelimit/TTUpdataT3Font 동적 슬롯 덮어쓰기 제거
           -> 비트맵 해시 중복 제거 + 영구 고유 Type 3 폰트(/FIXT3_n, 슬롯 33~254) 재배치
- Win 9x/ME: 0x14 제어 바이트 삭제 + 공백 치환 손상(/Lc/setl inecap) 복구
- 선택 기능: gs 기반 PDF/비트맵 PDF 변환, Win 98 Type 3 GID 텍스트 덤프
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

SLOT_MIN = 33
SLOT_MAX = 254
MAX_SLOTS = SLOT_MAX - SLOT_MIN + 1  # 222 glyphs per bank (BCh=32 및 제어문자 회피)

JOB_START_RE = re.compile(rb"(?:^|\r?\n)[ \t\x00-\x08\x0b\x0c\x0e-\x1f]*%!PS")
WIN31_GRE_RE = re.compile(r"^\s*/(MSTT\w+)\s+(.+?)\s+/\1\s+GreNewFont\b")
WIN31_FONT_RE = re.compile(r"^\s*([0-9.+\-\s]+?)\s+/(MSTT\w+)\s+font\b")
WIN31_T3_HDR_RE = re.compile(r"%%BeginResource:\s*TTUpdataT3Font\s+font\s+(MSTT\w+)")
WIN31_GLYPH_RE = re.compile(
    r"/(G[0-9A-Fa-f]+)\s*(\[[^\]]+\])\s*"
    r"/\1\s*\{((?:[^{}]|\{[^{}]*\})*)\}\s*"
    r"(\d+)\s*/\1\s+(MSTT\w+)\s+AddChar",
    re.DOTALL,
)

GRENEWFONT_TEMPLATE = (
    "Win35Dict begin\n"
    "/GreNewFont{10 dict dup 3 1 roll def dup begin 6 1 roll\n"
    "/FontType 3 def /FontMatrix exch def /FontBBox exch def\n"
    "/FontInfo 2 dict def FontInfo/UnderlinePosition 3 -1 roll put\n"
    "FontInfo/UnderlineThickness 3 -1 roll put\n"
    "/Encoding 256 array def 0 1 255{Encoding exch/.notdef put}for\n"
    "/CharProcs 257 dict def CharProcs/.notdef{}put\n"
    "/Metrics 257 dict def Metrics/.notdef 3 -1 roll put\n"
    "/BuildChar{/char exch def /fontdict exch def\n"
    "/charname fontdict/Encoding get char get def\n"
    "fontdict/Metrics get charname get aload pop __CACHE_OP__\n"
    "fontdict begin Encoding char get CharProcs exch get end exec}def\n"
    "end definefont pop}def\n"
    "/AddChar{begin Encoding 3 1 roll put CharProcs 3 1 roll put\n"
    "Metrics 3 1 roll put end}def\n"
    "end\n"
)


def split_jobs(raw: bytes) -> list[bytes]:
    starts = []
    for m in JOB_START_RE.finditer(raw):
        idx = m.start() + m.group(0).index(b"%!PS")
        starts.append(idx)
    if not starts:
        return []

    jobs = []
    for i, st in enumerate(starts):
        ed = starts[i + 1] if i + 1 < len(starts) else len(raw)
        chunk = raw[st:ed]
        eof_pos = chunk.rfind(b"%%EOF")
        if eof_pos != -1:
            nl = chunk.find(b"\n", eof_pos)
            chunk = chunk[: nl + 1] if nl != -1 else chunk[: eof_pos + 5] + b"\n"
        chunk = chunk.replace(b"\x04", b"")
        jobs.append(chunk)
    return jobs


def detect_dialect(job_bytes: bytes) -> str:
    head = job_bytes[:8192]
    if b"Win35Dict" in head:
        return "win31"
    if b"PSCRIPT.DRV Version 4." in head or b"Pscript_Win_Driver_Incr" in head:
        return "win9x"
    return "unknown"


def find_ps_string_span(line: str):
    start = line.find("(")
    if start < 0:
        return None
    depth = 0
    i = start
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return (start, i)
        i += 1
    return None


def decode_ps_string(s: str) -> bytes:
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch != "\\":
            out.append(ord(ch) & 0xFF)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        c = s[i]
        if c in "01234567":
            oct_digits = c
            i += 1
            for _ in range(2):
                if i < n and s[i] in "01234567":
                    oct_digits += s[i]
                    i += 1
                else:
                    break
            out.append(int(oct_digits, 8) & 0xFF)
        elif c == "n":
            out.append(10)
            i += 1
        elif c == "r":
            out.append(13)
            i += 1
        elif c == "t":
            out.append(9)
            i += 1
        elif c == "b":
            out.append(8)
            i += 1
        elif c == "f":
            out.append(12)
            i += 1
        elif c == "\r":
            i += 2 if (i + 1 < n and s[i + 1] == "\n") else 1
        elif c == "\n":
            i += 1
        else:
            out.append(ord(c) & 0xFF)
            i += 1
    return bytes(out)


def encode_ps_octal(bs: bytes) -> str:
    return "".join(f"\\{b:03o}" for b in bs)


def glyph_digest(metrics: str, body: str) -> str:
    m_norm = " ".join(metrics.split())
    b_norm = re.sub(r"\s+", "", body)
    return hashlib.sha1(f"{m_norm}|{b_norm}".encode("latin-1")).hexdigest()


def rewrite_sb_line(line: str, slot_map: dict[int, int]) -> str:
    if not re.search(r"\bSB\s*$", line):
        return line
    span = find_ps_string_span(line)
    if span is None:
        return line
    st, ed = span
    raw_bytes = decode_ps_string(line[st + 1 : ed])
    mapped = bytes(slot_map.get(b, b) for b in raw_bytes)
    return line[: st + 1] + encode_ps_octal(mapped) + line[ed:]


def fix_win31(job_bytes: bytes, no_cache: bool = False) -> tuple[bytes, dict]:
    text = job_bytes.decode("latin-1")
    lines = text.splitlines(keepends=True)

    gre_params: dict[str, str] = {}
    last_font_args: dict[str, str] = {}
    banks_by_sig: dict[tuple[str, str], list[dict]] = {}
    all_banks: list[dict] = []

    stats = {
        "dialect": "win31",
        "blocks_rewritten": 0,
        "glyph_instances": 0,
        "unique_glyphs": 0,
        "banks_created": 0,
    }

    def alloc_bank(orig_font: str, params: str, glyphs: list[tuple[int, str, str, str]]) -> dict:
        sig = (orig_font, params)
        bank_list = banks_by_sig.setdefault(sig, [])
        req_keys = {g[3] for g in glyphs}

        # 1) 모든 글리프를 이미 보유한 기존 뱅크 재사용
        for b in bank_list:
            if req_keys.issubset(b["key_to_slot"]):
                return b

        # 2) 마지막 뱅크에 부족한 글리프만 추가할 여유가 있으면 재사용
        if bank_list:
            cur = bank_list[-1]
            missing = req_keys - set(cur["key_to_slot"])
            if len(cur["entries"]) + len(missing) <= MAX_SLOTS:
                target = cur
            else:
                target = None
        else:
            target = None

        # 3) 여유가 없으면 새 뱅크 생성
        if target is None:
            bname = f"FIXT3_{len(all_banks)}"
            target = {
                "name": bname,
                "params": params,
                "key_to_slot": {},
                "entries": [],  # (slot, metrics, body)
            }
            bank_list.append(target)
            all_banks.append(target)

        for _, metrics, body, gkey in glyphs:
            if gkey not in target["key_to_slot"]:
                slot = SLOT_MIN + len(target["entries"])
                target["key_to_slot"][gkey] = slot
                target["entries"].append((slot, metrics, body))
        return target

    out_lines: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m_gre = WIN31_GRE_RE.match(line)
        if m_gre:
            gre_params[m_gre.group(1)] = m_gre.group(2)
            out_lines.append(line)
            i += 1
            continue

        m_fnt = WIN31_FONT_RE.match(line)
        if m_fnt:
            last_font_args[m_fnt.group(2)] = m_fnt.group(1).strip()
            out_lines.append(line)
            i += 1
            continue

        if line.strip() == "save":
            # 다음 비공백 2줄 이내에 setcachelimit 또는 TTUpdataT3Font가 있는지 확인
            is_t3_save = False
            checked = 0
            j = i + 1
            while j < n and checked < 2:
                st = lines[j].strip()
                if st:
                    checked += 1
                    if st == "1 setcachelimit" or "TTUpdataT3Font" in st:
                        is_t3_save = True
                        break
                j += 1

            if is_t3_save:
                buf = [line]
                j = i + 1
                found_restore = False
                while j < n:
                    buf.append(lines[j])
                    if lines[j].strip() == "restore":
                        found_restore = True
                        break
                    j += 1

                if found_restore:
                    block_text = "".join(buf)
                    m_hdr = WIN31_T3_HDR_RE.search(block_text)
                    end_res = block_text.find("%%EndResource")
                    if m_hdr and end_res != -1:
                        orig_font = m_hdr.group(1)
                        res_body = block_text[m_hdr.end() : end_res]
                        after_end = block_text.find("\n", end_res)
                        tail_start = after_end + 1 if after_end != -1 else len(block_text)
                        restore_pos = block_text.rfind("restore")
                        tail_text = block_text[tail_start:restore_pos]

                        glyphs = []
                        for gm in WIN31_GLYPH_RE.finditer(res_body):
                            metrics = gm.group(2)
                            body = gm.group(3)
                            orig_code = int(gm.group(4))
                            gkey = glyph_digest(metrics, body)
                            glyphs.append((orig_code, metrics, body, gkey))

                        if glyphs:
                            params = gre_params.get(
                                orig_font,
                                "[42.0 0 0 0 0 0] 23 23 [-42.0 -42.0 42.0 42.0] "
                                "[1 42 div 0 0 1 42 div 0 0]",
                            )
                            bank = alloc_bank(orig_font, params, glyphs)
                            slot_map = {
                                orig_code: bank["key_to_slot"][gkey]
                                for orig_code, _, _, gkey in glyphs
                            }
                            f_args = last_font_args.get(orig_font, "32 0 0 42 42 0 0 1 42")

                            out_lines.append("gs\n")
                            out_lines.append(f"{f_args} /{bank['name']} font\n")
                            for tline in tail_text.splitlines(keepends=True):
                                out_lines.append(rewrite_sb_line(tline, slot_map))
                            out_lines.append("gr\n")

                            stats["blocks_rewritten"] += 1
                            stats["glyph_instances"] += len(glyphs)
                            i = j + 1
                            continue

        out_lines.append(line)
        i += 1

    if all_banks:
        cache_op = "4{pop}repeat setcharwidth" if no_cache else "setcachedevice"
        inject = ["\n%%BeginResource: procset PSFix_T3Fonts 1 0\n"]
        inject.append(GRENEWFONT_TEMPLATE.replace("__CACHE_OP__", cache_op))
        for b in all_banks:
            bname = b["name"]
            inject.append(f"/{bname} {b['params']} /{bname} GreNewFont\n")
            for slot, metrics, body in b["entries"]:
                gname = f"G{slot:02X}"
                inject.append(f"/{gname} {metrics}\n")
                inject.append(f"/{gname} {{{body}}}\n")
                inject.append(f"  {slot} /{gname} {bname} AddChar\n")
        inject.append("%%EndResource\n")
        inject_str = "".join(inject)

        inserted = False
        for idx, l in enumerate(out_lines):
            if l.strip() == "%%EndSetup":
                out_lines.insert(idx, inject_str)
                inserted = True
                break
        if not inserted:
            for idx, l in enumerate(out_lines):
                if "Win35Dict begin" in l:
                    out_lines.insert(idx + 1, inject_str)
                    inserted = True
                    break

    stats["banks_created"] = len(all_banks)
    stats["unique_glyphs"] = sum(len(b["entries"]) for b in all_banks)
    return "".join(out_lines).encode("latin-1"), stats


def fix_win9x(job_bytes: bytes) -> tuple[bytes, dict]:
    c14 = job_bytes.count(b"\x14")
    fixed = job_bytes.replace(b"\x14", b"")

    space_bug = fixed.count(b"/Lc/setl inecap")
    if space_bug:
        fixed = fixed.replace(b"/Lc/setl inecap", b"/Lc/setlinecap")

    stats = {
        "dialect": "win9x",
        "ctrl_0x14_removed": c14,
        "setlinecap_repaired": space_bug,
    }
    return fixed, stats


def dump_win98_text(job_bytes: bytes, hangul_base_gid: int = 5878) -> list[str]:
    """
    Win 98 Type 3 서브폰트(MSTT...XX)의 GID 기반 텍스트 복원 (휴리스틱)
    - 빈 글리프({})를 공백(0x20)으로 삼아 기준 뱅크 및 ASCII 오프셋 자동 판별
    - 한글 완성형(U+AC00~U+D7A3)은 기본 굴림/바탕 계열 시작 GID(5878) 기준 매핑
    """
    text = job_bytes.decode("latin-1")

    # 1) F0, F1... -> MSTTxxxxxxxx 매핑 추출
    alias_to_mstt: dict[str, str] = {}
    for m in re.finditer(
        r"\((F\d+)\)\s*cvn\s+[\d.]+\s+\([^)]*\)\s*cvn\s*/TrueType\s+[TF]\s+\((MSTT[0-9A-Fa-f]+)\)\s*cvn\s+mF",
        text,
        re.DOTALL,
    ):
        alias_to_mstt[m.group(1)] = m.group(2)

    if not alias_to_mstt:
        return []

    # 2) 빈 글리프({})로 기준 뱅크 및 공백 GID 오프셋 탐지
    base_prefix = None
    base_bank = None
    space_code = None
    for m in re.finditer(
        r"/(G[0-9A-Fa-f]+)\s*\{\s*\}\s*(\d+)\s*/\1\s+(MSTT[0-9A-Fa-f]+)\s+AddChar",
        text,
    ):
        mstt = m.group(3)
        base_prefix = mstt[:-2]
        base_bank = int(mstt[-2:], 16)
        space_code = int(m.group(2))
        break

    if base_bank is None or space_code is None:
        return []

    ascii_offset = space_code - 0x20

    # 3) 페이지 본문에서 (x, y, 폰트, 문자열) 추출 후 y좌표 기준 줄 병합
    cur_alias = None
    cur_y = None
    runs: list[tuple[int, int, str]] = []  # (y, x, decoded_str)

    font_use_re = re.compile(r"^\s*(F\d+)[BI]*_\d+\s*$")
    move_re = re.compile(r"^\s*(\d+)\s+(\d+)\s+M\s*$")
    show_re = re.compile(r"\)\s*(?:sBT3|sB|S|A|W|D|asB|wsB|awsB|asBT3|wsBT3|awsBT3)\s*$")

    for line in text.splitlines():
        mf = font_use_re.match(line)
        if mf:
            cur_alias = mf.group(1)
            continue
        mm = move_re.match(line)
        if mm:
            cur_x = int(mm.group(1))
            cur_y = int(mm.group(2))
            continue
        if show_re.search(line) and cur_alias in alias_to_mstt and cur_y is not None:
            span = find_ps_string_span(line)
            if span is None:
                continue
            raw_bs = decode_ps_string(line[span[0] + 1 : span[1]])
            mstt = alias_to_mstt[cur_alias]
            if not mstt.startswith(base_prefix):
                continue
            bank_diff = int(mstt[-2:], 16) - base_bank
            chars = []
            for b in raw_bs:
                gid = (bank_diff << 8) | b
                if bank_diff == 0:
                    code = b - ascii_offset
                    chars.append(chr(code) if 0x20 <= code <= 0x7E else "?")
                else:
                    uni = 0xAC00 + (gid - hangul_base_gid)
                    chars.append(chr(uni) if 0xAC00 <= uni <= 0xD7A3 else f"[GID:{gid}]")
            runs.append((cur_y, cur_x, "".join(chars)))

    # 같은 y좌표끼리 x순 정렬해 한 줄로 합침
    by_y: dict[int, list[tuple[int, str]]] = {}
    for y, x, s in runs:
        by_y.setdefault(y, []).append((x, s))

    lines_out = []
    for y in sorted(by_y):
        parts = sorted(by_y[y], key=lambda t: t[0])
        lines_out.append("".join(p[1] for p in parts))
    return lines_out


def fix_ps_bytes(raw: bytes, no_cache: bool = False) -> list[tuple[bytes, dict]]:
    """외부 데몬(ps_spooler.py 등)에서 직접 호출할 수 있는 진입점"""
    results = []
    for job in split_jobs(raw):
        dialect = detect_dialect(job)
        if dialect == "win31":
            fixed, stats = fix_win31(job, no_cache=no_cache)
        elif dialect == "win9x":
            fixed, stats = fix_win9x(job)
        else:
            fixed = job.replace(b"\x14", b"")
            stats = {"dialect": "unknown", "ctrl_0x14_removed": job.count(b"\x14")}
        results.append((fixed, stats))
    return results


def convert_to_pdf(ps_path: Path, pdf_path: Path, raster: bool = False, dpi: int = 300) -> int:
    device = "pdfimage24" if raster else "pdfwrite"
    cmd = ["gs", "-dBATCH", "-dNOPAUSE", "-q", f"-sDEVICE={device}"]
    if raster:
        cmd.append(f"-r{dpi}")
    cmd.extend([f"-sOutputFile={pdf_path}", str(ps_path)])
    res = subprocess.run(cmd)
    return res.returncode


def main():
    ap = argparse.ArgumentParser(description="Legacy Windows PS Preprocessor (Win3.1 + Win9x/ME)")
    ap.add_argument("inputs", nargs="+", help="입력 .ps 또는 텍스트 덤프 파일")
    ap.add_argument("-o", "--out-dir", default=".", help="출력 디렉터리 (기본: 현재 폴더)")
    ap.add_argument("--no-cache", action="store_true", help="Win3.1 BuildChar에서 setcachedevice 비활성화")
    ap.add_argument("--pdf", action="store_true", help="gs pdfwrite로 벡터/Type3 PDF 생성")
    ap.add_argument("--raster-pdf", action="store_true", help="gs pdfimage24(300dpi)로 비트맵 PDF 생성")
    ap.add_argument("--dump-text", action="store_true", help="Win98 GID 텍스트 휴리스틱 추출 (.txt)")
    ap.add_argument("--hangul-gid", type=int, default=5878, help="Win98 한글 '가' 시작 GID (기본: 5878)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for inp in args.inputs:
        in_path = Path(inp)
        raw = in_path.read_bytes()
        jobs = fix_ps_bytes(raw, no_cache=args.no_cache)
        if not jobs:
            print(f"[SKIP] {in_path}: %!PS 헤더 없음", file=sys.stderr)
            continue

        stem = in_path.stem
        if stem.endswith(".ps"):
            stem = stem[:-3]

        for idx, (fixed, stats) in enumerate(jobs, 1):
            suffix = f"_job{idx}" if len(jobs) > 1 else ""
            ps_out = out_dir / f"{stem}{suffix}_fixed.ps"
            ps_out.write_bytes(fixed)

            orig_kb = len(raw) / 1024 if len(jobs) == 1 else 0
            out_kb = len(fixed) / 1024
            size_info = f"{orig_kb:.1f}KB -> {out_kb:.1f}KB" if orig_kb else f"{out_kb:.1f}KB"
            print(f"[OK] {ps_out.name} ({size_info}) {stats}")

            if args.pdf:
                pdf_out = out_dir / f"{stem}{suffix}.pdf"
                rc = convert_to_pdf(ps_out, pdf_out, raster=False)
                print(f"     -> pdfwrite rc={rc}: {pdf_out.name}")

            if args.raster_pdf:
                rpdf_out = out_dir / f"{stem}{suffix}_raster.pdf"
                rc = convert_to_pdf(ps_out, rpdf_out, raster=True)
                print(f"     -> pdfimage24 rc={rc}: {rpdf_out.name}")

            if args.dump_text and stats.get("dialect") == "win9x":
                lines = dump_win98_text(fixed, hangul_base_gid=args.hangul_gid)
                if lines:
                    txt_out = out_dir / f"{stem}{suffix}.txt"
                    txt_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    print(f"     -> text dump ({len(lines)} lines): {txt_out.name}")


if __name__ == "__main__":
    main()
