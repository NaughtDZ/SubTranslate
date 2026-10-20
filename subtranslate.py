#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SubTranslate 1.0 — 单文件字幕翻译工具（零第三方依赖）

  python subtranslate.py                 # 启动 GUI
  python subtranslate.py --cli DIR ...   # 命令行批量
  python subtranslate.py --selftest      # 内置自检（不联网）

设计要点（为什么小模型也能稳定出片）：
  1. 模型永远不接触字幕文件结构：时间轴、序号、样式头、标签全部由程序掌管。
  2. 模型只吃 "ID|原文" 行、只吐 "ID|译文" 行，行数必须一一对应。
  3. 行内格式标记（ASS 覆盖块、\\N、HTML 标签、VTT 时间戳）先换成占位符
     ⟦0⟧ ⟦1⟧，译完再换回来；数量对不上就重试，绝不让模型自由发挥。
  4. 批级校验 -> 失败子集重发 -> 再失败二分 -> 单条重试 -> 仍失败保留原文，
     任何一步都不会写出结构损坏的字幕。
  5. 支持多目录、递归扫描、语言自动识别、本地/远端 OpenAI 兼容接口与 Ollama。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import http.client
import json
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

APP = "SubTranslate"
VERSION = "1.0.0"
CONFIG_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "SubTranslate"
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_PATH = CONFIG_DIR / "cache.json"

PH_OPEN, PH_CLOSE = "\u27e6", "\u27e7"          # ⟦ ⟧
BRK = "\u23ce"                                  # ⏎ 行内换行标记

# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def fmt_ms_srt(ms: int) -> str:
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ms_vtt(ms: int) -> str:
    return fmt_ms_srt(ms).replace(",", ".")


def fmt_ms_ass(ms: int) -> str:
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms // 10:02d}"


def fmt_ms_sbv(ms: int) -> str:
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


_TIME_RE = re.compile(r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")


def parse_time_ms(s: str) -> int:
    """解析 1:02:03.456 / 02:03,45 / 1:02:03.45 等。"""
    m = _TIME_RE.search(s.strip())
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mi = int(m.group(2))
    se = int(m.group(3))
    frac = m.group(4) or "0"
    if len(frac) == 1:
        ms = int(frac) * 100
    elif len(frac) == 2:
        ms = int(frac) * 10
    else:
        ms = int(frac[:3])
    return h * 3600000 + mi * 60000 + se * 1000 + ms


def read_text_file(path: Path) -> tuple[str, str, bool]:
    """返回 (文本, 编码, 是否带 BOM)。"""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8", True
    for enc in ("utf-8", "gb18030", "big5", "shift_jis", "cp1252"):
        try:
            return raw.decode(enc), ("utf-8" if enc == "utf-8" else enc), False
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1", False


def write_text_file(path: Path, text: str, encoding: str, bom: bool) -> None:
    data = text.encode(encoding, "replace")
    if bom and not data.startswith(b"\xef\xbb\xbf"):
        data = b"\xef\xbb\xbf" + data
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class Cue:
    start: int
    end: int
    text: str
    meta: dict = field(default_factory=dict)
    translated: Optional[str] = None
    state: str = "pending"          # pending / ok / kept / failed
    note: str = ""


@dataclass
class SubDoc:
    path: Path
    fmt: "SubtitleFormat"
    cues: list[Cue]
    meta: dict = field(default_factory=dict)
    encoding: str = "utf-8"
    bom: bool = False


# --------------------------------------------------------------------------
# 格式：基类 + 各实现
# --------------------------------------------------------------------------


class SubtitleFormat:
    key = ""
    name = ""
    exts: tuple[str, ...] = ()
    writable = True

    @classmethod
    def sniff(cls, text: str) -> int:
        return 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        raise NotImplementedError

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        raise NotImplementedError

    # 统计信息
    @classmethod
    def info(cls) -> str:
        return cls.name


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _cue_text(c: Cue) -> str:
    return c.translated if c.translated is not None else c.text


# --------------------------- SRT ---------------------------


class SrtFormat(SubtitleFormat):
    key, name = "srt", "SubRip (SRT)"
    exts = (".srt",)

    @classmethod
    def sniff(cls, text: str) -> int:
        if text.lstrip("\ufeff \n\t").upper().startswith("WEBVTT"):
            return 0
        return 90 if re.search(r"-->\s*\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}", text) else 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        sep = ","
        m0 = re.search(r"-->\s*\d{1,3}:\d{2}:\d{2}([,.])\d{1,3}", text)
        if m0:
            sep = m0.group(1)
        blocks = re.split(r"\n{2,}", text.strip("\n"))
        cues: list[Cue] = []
        for b in blocks:
            lines = b.split("\n")
            ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
            if ti is None:
                continue
            m = re.search(
                r"(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})",
                lines[ti],
            )
            if not m:
                continue
            body = "\n".join(lines[ti + 1:]).strip("\n")
            cues.append(Cue(parse_time_ms(m.group(1)), parse_time_ms(m.group(2)), body))
        if not cues:
            return None
        return SubDoc(path, cls, cues, {"sep": sep})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        sep = doc.meta.get("sep", ",")
        out: list[str] = []
        for i, c in enumerate(doc.cues, 1):
            out.append(str(i))
            out.append(
                f"{fmt_ms_srt(c.start).replace(',', sep)} --> "
                f"{fmt_ms_srt(c.end).replace(',', sep)}"
            )
            out.append(_cue_text(c))
            out.append("")
        return "\n".join(out).rstrip("\n") + "\n"


# --------------------------- ASS / SSA ---------------------------


class AssFormat(SubtitleFormat):
    key, name = "ass", "Advanced SubStation Alpha (ASS/SSA)"
    exts = (".ass", ".ssa")

    @classmethod
    def sniff(cls, text: str) -> int:
        if re.search(r"^\s*\[Script Info\]", text, re.I | re.M) and re.search(
            r"^\s*\[Events\]", text, re.I | re.M
        ):
            return 95
        return 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        lines = text.split("\n")
        in_events = False
        fields: list[str] = []
        cues: list[Cue] = []
        for idx, line in enumerate(lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                in_events = s.lower() == "[events]"
                continue
            if not in_events:
                continue
            if s.lower().startswith("format:"):
                fields = [x.strip() for x in s.split(":", 1)[1].split(",")]
                continue
            if not s.lower().startswith("dialogue:"):
                continue
            if not fields or fields[-1].lower() != "text":
                continue
            head, rest = line.split(":", 1)
            rest2 = rest.lstrip()
            lead = len(rest) - len(rest2)
            parts = rest2.split(",", len(fields) - 1)
            if len(parts) < len(fields):
                continue
            try:
                si = [f.lower() for f in fields].index("start")
                ei = [f.lower() for f in fields].index("end")
            except ValueError:
                continue
            prefix = head + ":" + " " * lead + ",".join(parts[:-1]) + ","
            cues.append(
                Cue(
                    parse_time_ms(parts[si]),
                    parse_time_ms(parts[ei]),
                    parts[-1],
                    {"line": idx, "prefix": prefix},
                )
            )
        if not cues:
            return None
        return SubDoc(path, cls, cues, {"lines": lines})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        lines = list(doc.meta["lines"])
        for c in doc.cues:
            i = c.meta["line"]
            lines[i] = c.meta["prefix"] + _cue_text(c).replace("\n", "\\N")
        return "\n".join(lines)


# --------------------------- WebVTT ---------------------------


class VttFormat(SubtitleFormat):
    key, name = "vtt", "WebVTT (VTT)"
    exts = (".vtt",)

    @classmethod
    def sniff(cls, text: str) -> int:
        return 96 if text.lstrip("\ufeff \n\t").upper().startswith("WEBVTT") else 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        chunks = re.split(r"\n{2,}", text.strip("\n"))
        if not chunks:
            return None
        head = chunks[0]
        cues: list[Cue] = []
        segments: list[tuple[str, Any]] = []
        for ch in chunks[1:]:
            lines = ch.split("\n")
            tline = next((i for i, l in enumerate(lines) if "-->" in l), None)
            if tline is None:
                segments.append(("raw", ch))
                continue
            m = re.search(
                r"(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})\s*-->\s*"
                r"(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})",
                lines[tline],
            )
            if not m:
                segments.append(("raw", ch))
                continue
            cue_id = lines[:tline]
            settings = lines[tline][m.end():].strip()
            body = "\n".join(lines[tline + 1:]).strip("\n")
            c = Cue(
                parse_time_ms(m.group(1)),
                parse_time_ms(m.group(2)),
                body,
                {"id": cue_id, "settings": settings},
            )
            cues.append(c)
            segments.append(("cue", len(cues) - 1))
        if not cues:
            return None
        return SubDoc(path, cls, cues, {"head": head, "segments": segments})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        out = [doc.meta["head"], ""]
        for kind, val in doc.meta["segments"]:
            if kind == "raw":
                out.append(val)
                out.append("")
                continue
            c = doc.cues[val]
            for lid in c.meta.get("id", []):
                out.append(lid)
            t = f"{fmt_ms_vtt(c.start)} --> {fmt_ms_vtt(c.end)}"
            if c.meta.get("settings"):
                t += " " + c.meta["settings"]
            out.append(t)
            out.append(_cue_text(c))
            out.append("")
        return "\n".join(out).rstrip("\n") + "\n"


# --------------------------- LRC ---------------------------


class LrcFormat(SubtitleFormat):
    key, name = "lrc", "LRC 歌词"
    exts = (".lrc",)

    TAGS = re.compile(r"((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)")
    META = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|length|tool|au|#):", re.I)

    @classmethod
    def sniff(cls, text: str) -> int:
        if re.search(r"^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", text, re.M):
            return 70
        if re.search(r"^\s*\[(ti|ar|al|by|offset):", text, re.M | re.I):
            return 55
        return 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        lines = text.split("\n")
        cues: list[Cue] = []
        for idx, line in enumerate(lines):
            if cls.META.match(line.strip()):
                continue
            m = cls.TAGS.match(line.strip())
            if not m:
                continue
            tags = m.group(1)
            body = line.strip()[len(tags):]
            times = [parse_time_ms(x) for x in cls.TAGS.findall(tags) and
                     re.findall(r"\[(\d{1,3}:\d{2}(?:[.:]\d{1,3})?)\]", tags)]
            if not times:
                continue
            cues.append(
                Cue(times[0], times[0] + 3000, body,
                    {"line": idx, "tags": tags, "times": times})
            )
        if not cues:
            return None
        for i, c in enumerate(cues):
            if i + 1 < len(cues):
                c.end = cues[i + 1].start
            if c.end <= c.start:
                c.end = c.start + 2000
        return SubDoc(path, cls, cues, {"lines": lines})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        lines = list(doc.meta["lines"])
        for c in doc.cues:
            lines[c.meta["line"]] = c.meta["tags"] + _cue_text(c).replace("\n", " ")
        return "\n".join(lines)


# --------------------------- SBV ---------------------------


class SbvFormat(SubtitleFormat):
    key, name = "sbv", "SubViewer (SBV)"
    exts = (".sbv",)

    @classmethod
    def sniff(cls, text: str) -> int:
        return 60 if re.search(r"^\d{1,3}:\d{2}:\d{2}\.\d{3},\d{1,3}:\d{2}:\d{2}\.\d{3}\s*$",
                               text, re.M) else 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        blocks = re.split(r"\n{2,}", text.strip("\n"))
        cues: list[Cue] = []
        for b in blocks:
            lines = b.split("\n")
            if not lines:
                continue
            m = re.match(r"\s*(\d{1,3}:\d{2}:\d{2}\.\d{3}),\s*(\d{1,3}:\d{2}:\d{2}\.\d{3})",
                         lines[0])
            if not m:
                continue
            cues.append(Cue(parse_time_ms(m.group(1)), parse_time_ms(m.group(2)),
                            "\n".join(lines[1:]).strip("\n")))
        if not cues:
            return None
        return SubDoc(path, cls, cues, {})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        out = []
        for c in doc.cues:
            out.append(f"{fmt_ms_sbv(c.start)},{fmt_ms_sbv(c.end)}")
            out.append(_cue_text(c))
            out.append("")
        return "\n".join(out).rstrip("\n") + "\n"


# --------------------------- MicroDVD / MPL2 ---------------------------


class MicroDvdFormat(SubtitleFormat):
    key, name = "sub", "MicroDVD (SUB)"
    exts = (".sub",)

    @classmethod
    def sniff(cls, text: str) -> int:
        return 50 if re.search(r"^\{\d+\}\{\d+\}", text, re.M) else 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        lines = text.split("\n")
        cues: list[Cue] = []
        fps = 25.0
        for idx, line in enumerate(lines):
            m = re.match(r"^\{(\d+)\}\{(\d+)\}(.*)$", line)
            if not m:
                continue
            body = m.group(3)
            if body.startswith("{"):        # {Y:i} 之类样式前缀
                mm = re.match(r"^(\{[^}]*\})(.*)$", body)
                if mm:
                    body = mm.group(2)
            start = int(int(m.group(1)) / fps * 1000)
            end = int(int(m.group(2)) / fps * 1000)
            cues.append(Cue(start, end, body.replace("|", "\n"),
                            {"line": idx, "head": line[:len(line) - len(m.group(3))],
                             "style": line[len(line) - len(m.group(3)):len(line) - len(body)]}))
        if not cues:
            return None
        return SubDoc(path, cls, cues, {"lines": lines, "fps": fps})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        lines = list(doc.meta["lines"])
        for c in doc.cues:
            txt = _cue_text(c).replace("\n", "|").replace("|", "|")
            lines[c.meta["line"]] = c.meta["head"] + txt
        return "\n".join(lines)


class Mpl2Format(SubtitleFormat):
    key, name = "mpl2", "MPL2"
    exts = (".mpl2", ".txt")

    @classmethod
    def sniff(cls, text: str) -> int:
        return 45 if re.search(r"^\[\d+\]\[\d+\]", text, re.M) else 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        lines = text.split("\n")
        cues: list[Cue] = []
        for idx, line in enumerate(lines):
            m = re.match(r"^\[(\d+)\]\[(\d+)\](.*)$", line)
            if not m:
                continue
            cues.append(Cue(int(m.group(1)) * 100, int(m.group(2)) * 100,
                            m.group(3).replace("|", "\n"), {"line": idx}))
        if not cues:
            return None
        return SubDoc(path, cls, cues, {"lines": lines})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        lines = list(doc.meta["lines"])
        for c in doc.cues:
            lines[c.meta["line"]] = (
                f"[{c.start // 100}][{c.end // 100}]" +
                _cue_text(c).replace("\n", "|")
            )
        return "\n".join(lines)


# --------------------------- SAMI ---------------------------


class SamiFormat(SubtitleFormat):
    key, name = "smi", "SAMI (SMI)"
    exts = (".smi", ".sami")

    @classmethod
    def sniff(cls, text: str) -> int:
        if re.search(r"<sync\b", text, re.I):
            return 85
        if re.search(r"<sami\b", text, re.I):
            return 80
        return 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        marks = list(re.finditer(r"<sync\b[^>]*>", text, re.I))
        if not marks:
            return None
        blocks: list[tuple[str, str]] = []       # (head, body)
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            seg = text[m.start():end]
            head, rest = re.match(r"(<sync\b[^>]*>)(.*)$", seg, re.I | re.S).groups()
            pm = re.match(r"(<p\b[^>]*>)(.*)$", rest, re.I | re.S)
            if pm:
                head += pm.group(1)
                rest = pm.group(2)
            blocks.append((head, rest))
        cues: list[Cue] = []
        segments: list[tuple[str, Any]] = []
        pos = 0
        starts = [int(re.search(r"start\s*=\s*\"?(\d+)", b[0], re.I).group(1))
                  if re.search(r"start\s*=\s*\"?(\d+)", b[0], re.I) else 0 for b in blocks]
        for i, (head, body) in enumerate(blocks):
            start = starts[i]
            end = starts[i + 1] if i + 1 < len(starts) else start + 3000
            plain = re.sub(r"<[^>]+>", "", body)
            plain = plain.replace("&nbsp;", " ").strip()
            if not plain:                       # 清屏块
                segments.append(("raw", head + body))
                continue
            c = Cue(start, end, body, {"head": head})
            cues.append(c)
            segments.append(("cue", len(cues) - 1))
        if not cues:
            return None
        return SubDoc(path, cls, cues, {"segments": segments, "raw": text})

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        out = []
        for kind, val in doc.meta["segments"]:
            if kind == "raw":
                out.append(val)
                continue
            c = doc.cues[val]
            body = _cue_text(c).replace("\n", "<br>")
            out.append(c.meta["head"] + body)
        return "".join(out)


# --------------------------- TTML ---------------------------


class TtmlFormat(SubtitleFormat):
    key, name = "ttml", "TTML / DFXP"
    exts = (".ttml", ".dfxp", ".xml")

    P_RE = re.compile(r"(<p\b[^>]*>)(.*?)(</p>)", re.I | re.S)

    @classmethod
    def sniff(cls, text: str) -> int:
        if re.search(r"<tt\b", text, re.I) and re.search(r"<p\b", text, re.I):
            return 84
        if re.search(r"<\?xml", text) and re.search(r"begin\s*=", text):
            return 60
        return 0

    @classmethod
    def parse(cls, text: str, path: Path) -> Optional[SubDoc]:
        text = _norm(text)
        cues: list[Cue] = []
        for m in cls.P_RE.finditer(text):
            tag, inner = m.group(1), m.group(2)
            bt = re.search(r'begin\s*=\s*"([^"]+)"', tag, re.I)
            et = re.search(r'end\s*=\s*"([^"]+)"', tag, re.I)
            if not bt:
                continue
            start = cls._t(bt.group(1))
            end = cls._t(et.group(1)) if et else start + 3000
            body = re.sub(r"<br\s*/?>", "\n", inner, flags=re.I)
            body = re.sub(r"<[^>]+>", "", body)
            body = (body.replace("&lt;", "<").replace("&gt;", ">")
                        .replace("&amp;", "&").replace("&quot;", '"').replace("&apos;", "'"))
            cues.append(Cue(start, end, body,
                            {"open": m.group(1), "close": m.group(3)}))
        if not cues:
            return None
        return SubDoc(path, cls, cues, {"raw": text})

    @staticmethod
    def _t(s: str) -> int:
        s = s.strip()
        if s.endswith("ms"):
            return int(float(s[:-2]))
        if s.endswith("s") and ":" not in s:
            return int(float(s[:-1]) * 1000)
        return parse_time_ms(s)

    @classmethod
    def render(cls, doc: SubDoc) -> str:
        # 逐条按顺序替换 <p> 内部文本
        raw = doc.meta["raw"]
        out = []
        pos = 0
        for c in doc.cues:
            m = cls.P_RE.search(raw, pos)
            if not m:
                break
            out.append(raw[pos:m.start(2)])
            txt = (_cue_text(c).replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;").replace("\n", "<br/>"))
            out.append(txt)
            pos = m.end(2)
        out.append(raw[pos:])
        return "".join(out)


# --------------------------------------------------------------------------
# 格式注册
# --------------------------------------------------------------------------

FORMATS: list[type[SubtitleFormat]] = [
    SrtFormat, AssFormat, VttFormat, LrcFormat, SbvFormat,
    MicroDvdFormat, Mpl2Format, SamiFormat, TtmlFormat,
]

EXT_MAP: dict[str, type[SubtitleFormat]] = {}
for _f in FORMATS:
    for _e in _f.exts:
        EXT_MAP.setdefault(_e, _f)

ALL_EXTS = sorted(EXT_MAP.keys())


def detect_format(path: Path, text: str) -> Optional[type[SubtitleFormat]]:
    scores: list[tuple[int, type[SubtitleFormat]]] = []
    for f in FORMATS:
        try:
            scores.append((f.sniff(text), f))
        except Exception:
            continue
    scores.sort(key=lambda x: -x[0])
    if scores and scores[0][0] >= 45:
        return scores[0][1]
    by_ext = EXT_MAP.get(path.suffix.lower())
    return by_ext


def load_subdoc(path: Path) -> SubDoc:
    text, enc, bom = read_text_file(path)
    body = text.lstrip("\ufeff")
    fmt = detect_format(path, body)
    if fmt is None:
        raise ValueError(f"无法识别字幕格式: {path.name}")
    doc = fmt.parse(body, path)
    if doc is None:
        raise ValueError(f"解析失败（文件可能为空或损坏）: {path.name}")
    doc.encoding = enc
    doc.bom = bom
    return doc


def save_subdoc(doc: SubDoc, out_path: Path) -> None:
    text = doc.fmt.render(doc)
    try:
        write_text_file(out_path, text, doc.encoding, doc.bom)
    except UnicodeEncodeError:
        write_text_file(out_path, text, "utf-8", True)


# --------------------------------------------------------------------------
# 标签保护（占位符化）
# --------------------------------------------------------------------------

TAG_RE = re.compile(
    r"(\\[Nnh])"                                   # ASS 换行/硬空格
    r"|(\{[^}]{0,400}\})"                          # ASS 覆盖块
    r"|(<\d{1,3}:\d{2}:\d{2}[.,]\d{1,3}>)"         # WebVTT 时间戳标签
    r"|(</?(?:i|b|u|s|em|strong|font|span|c|v|ruby|rt|br|p|div)\b[^>]{0,200}>)"  # HTML/VTT
    r"|(&(?:[a-zA-Z][a-zA-Z0-9]{1,10}|#\d{1,6}|#x[0-9a-fA-F]{1,6});)"           # 实体
)


def protect_tags(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []

    def repl(m: re.Match) -> str:
        tokens.append(m.group(0))
        return f"{PH_OPEN}{len(tokens) - 1}{PH_CLOSE}"

    return TAG_RE.sub(repl, text), tokens


def restore_tags(masked: str, tokens: list[str]) -> str:
    if not tokens:
        return masked
    out = masked
    # 宽容匹配 ⟦ 0 ⟧ / ⟦0⟧
    for k in range(len(tokens)):
        out = re.sub(rf"{PH_OPEN}\s*{k}\s*{PH_CLOSE}", lambda _m, t=tokens[k]: t, out)
    # 兜底：清掉任何残留占位符
    out = re.sub(rf"{PH_OPEN}\s*\d+\s*{PH_CLOSE}", "", out)
    return out


PH_FIND = re.compile(rf"{PH_OPEN}\s*(\d+)\s*{PH_CLOSE}")


def placeholder_ids(s: str) -> list[int]:
    return [int(x) for x in PH_FIND.findall(s)]


# --------------------------------------------------------------------------
# 线上文本：模型只看到「纯文本 + || 换行」，标签由程序回填
# --------------------------------------------------------------------------

BREAK = "||"


@dataclass
class WireText:
    plain: str                                  # 纯文本，换行已变成 ||
    tags: list[tuple[float, str]]               # (位置比例 0..1, 标签原文)
    break_ratios: list[float]                   # 每个换行在原文中的位置比例
    n_breaks: int = 0

    def __str__(self) -> str:
        return self.plain


def make_wire(text: str) -> WireText:
    """剥掉所有格式标签，换行归一成 \\n，记录标签位置，再把换行换成 ||。"""
    t = text.replace("\\N", "\n").replace("\\n", "\n")
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = t.replace(BREAK, "\uff5c\uff5c")         # 原文自带的 || 转全角，避免歧义
    parts: list[tuple[str, str]] = []
    last = 0
    for m in TAG_RE.finditer(t):
        if m.start() > last:
            parts.append(("t", t[last:m.start()]))
        parts.append(("g", m.group(0)))
        last = m.end()
    if last < len(t):
        parts.append(("t", t[last:]))
    plain = "".join(s for k, s in parts if k == "t")
    total = max(1, len(plain.replace("\n", "")))
    tags: list[tuple[float, str]] = []
    breaks: list[float] = []
    acc = 0
    for kind, s in parts:
        if kind == "t":
            for ch in s:
                if ch == "\n":
                    breaks.append(acc / total)
                else:
                    acc += 1
        else:
            tags.append((acc / total, s))
    n = plain.count("\n")
    return WireText(plain.replace("\n", BREAK), tags, breaks, n)


def apply_wire(w: WireText, trans: str) -> str:
    """把模型译文还原成带标签、带换行的字幕文本。"""
    t = trans.replace("\\n", BREAK).replace("\r", "").replace("\n", BREAK)
    # 换行数量向原文看齐
    guard = 0
    while t.count(BREAK) > w.n_breaks and guard < 50:
        i = t.rfind(BREAK)
        t = t[:i] + t[i + 2:]
        guard += 1
    while t.count(BREAK) < w.n_breaks and w.break_ratios and guard < 50:
        k = t.count(BREAK)
        ratio = w.break_ratios[k] if k < len(w.break_ratios) else 0.5
        pos = max(0, min(len(t), int(round(ratio * len(t)))))
        t = t[:pos] + BREAK + t[pos:]
        guard += 1
    # 标签回填：从后往前，位置按比例
    for ratio, tag in sorted(w.tags, key=lambda x: x[0], reverse=True):
        pos = max(0, min(len(t), int(round(ratio * len(t)))))
        t = t[:pos] + tag + t[pos:]
    return t.replace(BREAK, "\n")


# --------------------------------------------------------------------------
# 语言识别
# --------------------------------------------------------------------------

LANG_NAMES: dict[str, str] = {
    "auto": "自动识别", "zh-CN": "简体中文", "zh-TW": "繁體中文", "en": "English",
    "ja": "日本語", "ko": "한국어", "fr": "Français", "de": "Deutsch",
    "es": "Español", "ru": "Русский", "pt": "Português", "it": "Italiano",
    "ar": "العربية", "th": "ไทย", "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
    "tr": "Türkçe", "pl": "Polski", "nl": "Nederlands", "uk": "Українська",
}

LATIN_HINTS: dict[str, tuple[str, ...]] = {
    "en": ("the", "and", "you", "that", "is", "of", "to", "it", "this", "what"),
    "es": ("el", "la", "de", "que", "y", "en", "los", "una", "por", "para"),
    "fr": ("le", "la", "les", "de", "et", "un", "une", "que", "pas", "vous"),
    "de": ("der", "die", "das", "und", "ist", "nicht", "ein", "eine", "mit", "ich"),
    "pt": ("o", "a", "de", "que", "e", "do", "da", "em", "um", "para"),
    "it": ("il", "la", "di", "che", "e", "un", "una", "per", "non", "sono"),
    "id": ("yang", "dan", "di", "itu", "ini", "tidak", "untuk", "dengan", "saya", "kamu"),
    "vi": ("của", "và", "là", "không", "trong", "được", "người", "này", "tôi", "bạn"),
    "tr": ("bir", "ve", "bu", "için", "değil", "ile", "olarak", "çok", "ben", "sen"),
    "nl": ("de", "het", "een", "en", "van", "niet", "dat", "ik", "je", "zijn"),
    "pl": ("nie", "się", "jest", "że", "na", "to", "ale", "jak", "tak", "co"),
}

_CJK = re.compile(r"[\u4e00-\u9fff]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_CYR = re.compile(r"[\u0400-\u04ff]")
_ARAB = re.compile(r"[\u0600-\u06ff]")
_THAI = re.compile(r"[\u0e00-\u0e7f]")
_HEBR = re.compile(r"[\u0590-\u05ff]")
_GREEK = re.compile(r"[\u0370-\u03ff]")
_DEVA = re.compile(r"[\u0900-\u097f]")
_LATIN = re.compile(r"[A-Za-z\u00c0-\u024f]")


def sample_text(doc: SubDoc, limit: int = 6000) -> str:
    buf: list[str] = []
    total = 0
    for c in doc.cues:
        t = plain_text(c.text)
        if not t:
            continue
        buf.append(t)
        total += len(t)
        if total >= limit:
            break
    return "\n".join(buf)


def plain_text(s: str) -> str:
    s = TAG_RE.sub(" ", s)
    s = re.sub(r"\{[^}]*\}", " ", s)
    s = re.sub(r"<[^>]*>", " ", s)
    s = s.replace(BRK, " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def guess_lang(text: str) -> tuple[str, float]:
    if not text.strip():
        return ("unknown", 0.0)
    n = max(1, len(text))
    counts = {
        "han": len(_CJK.findall(text)), "kana": len(_KANA.findall(text)),
        "hangul": len(_HANGUL.findall(text)), "cyr": len(_CYR.findall(text)),
        "arab": len(_ARAB.findall(text)), "thai": len(_THAI.findall(text)),
        "hebr": len(_HEBR.findall(text)), "greek": len(_GREEK.findall(text)),
        "deva": len(_DEVA.findall(text)), "latin": len(_LATIN.findall(text)),
    }
    ratio = {k: v / n for k, v in counts.items()}
    if ratio["hangul"] > 0.08:
        return ("ko", 0.9)
    if ratio["kana"] > 0.04:
        return ("ja", 0.9)
    if ratio["han"] > 0.10:
        return ("zh-CN", 0.8)
    if ratio["cyr"] > 0.10:
        return ("ru", 0.75)
    if ratio["arab"] > 0.10:
        return ("ar", 0.75)
    if ratio["thai"] > 0.10:
        return ("th", 0.85)
    if ratio["hebr"] > 0.10:
        return ("he", 0.75)
    if ratio["greek"] > 0.10:
        return ("el", 0.7)
    if ratio["deva"] > 0.10:
        return ("hi", 0.8)
    if ratio["latin"] > 0.20:
        words = re.findall(r"[A-Za-z\u00c0-\u024f']+", text.lower())
        if not words:
            return ("unknown", 0.0)
        best, score = "en", 0.0
        for code, hints in LATIN_HINTS.items():
            hs = set(hints)
            hit = sum(1 for w in words if w in hs) / len(words)
            if hit > score:
                best, score = code, hit
        conf = clamp(0.4 + score * 3, 0.3, 0.9)
        return (best, conf)
    return ("unknown", 0.0)


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a professional subtitle translator. You translate subtitle lines and nothing else.

INPUT: one subtitle per line, as: <id>|<text>
OUTPUT: exactly one line per input line, as: <id>|<translation>

RULES:
1. Copy <id> exactly. Never renumber, reorder, merge, split or drop lines.
2. Output nothing else: no timestamps, no numbering, no quotes, no markdown, no explanations, no blank lines.
3. The marker || marks a line break inside one subtitle. Reproduce it exactly where the break belongs.
4. Translate everything else faithfully. Keep numbers, names, units and sound effects consistent with the source.
5. If a line is already in the target language, keep it unchanged.
6. Keep each translation short enough to read on screen; never drop half of the sentence."""

RETRY_HINT = (
    "\n\nREMINDER: reply with exactly {n} lines, each formatted as \"<id>|<translation>\", "
    "nothing before or after. IDs must be: {ids}"
)


def build_batch_messages(
    items: list[tuple[int, str]],
    src: str,
    dst: str,
    system: str,
    glossary: dict[str, str] | None = None,
    retry: bool = False,
) -> list[dict]:
    src_name = LANG_NAMES.get(src, src)
    dst_name = LANG_NAMES.get(dst, dst)
    lines = [f"{i}|{t}" for i, t in items]
    body = "\n".join(lines)
    gl = ""
    if glossary:
        gl = "\n\nGlossary (use these translations):\n" + "\n".join(
            f"- {k} => {v}" for k, v in list(glossary.items())[:40]
        )
    user = (
        f"Translate the following subtitle lines from {src_name} to {dst_name}.\n"
        f"Return the same IDs, one line each, in the same order, and nothing else.\n\n"
        f"{body}{gl}"
    )
    if retry:
        user += RETRY_HINT.format(n=len(items), ids=", ".join(str(i) for i, _ in items))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


LANG_DETECT_PROMPT = """You are a language identification tool.
Read the text and answer with ONE ISO 639-1 code only (examples: en, ja, ko, zh, fr, de, es, ru, ar, th, vi, id).
No punctuation, no explanation. If unsure, answer your best guess."""


# --------------------------------------------------------------------------
# LLM 客户端
# --------------------------------------------------------------------------


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        provider: str = "openai",
        timeout: float = 180.0,
        temperature: float = 0.2,
        max_tokens: int = 0,
        retries: int = 3,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.provider = provider.lower()
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retries = retries
        self.last_usage: dict = {}
        self._active = None
        self._active_lock = threading.Lock()
        self._aborted = threading.Event()

    def abort(self) -> None:
        """从其他线程掐断正在进行的请求（shutdown + close，让阻塞的 read 立刻返回）。"""
        self._aborted.set()
        with self._active_lock:
            conn = self._active
        if conn is None:
            return
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    def reset_abort(self) -> None:
        self._aborted.clear()

    # ---- 路径 ----
    def _endpoint(self) -> str:
        if self.provider == "ollama":
            return self.base_url + "/api/chat"
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _models_endpoint(self) -> str:
        if self.provider == "ollama":
            return self.base_url + "/api/tags"
        base = self.base_url
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        return base + "/models"

    @staticmethod
    def _opener(url: str):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

    def _post(self, url: str, payload: dict) -> dict:
        if self._aborted.is_set():
            raise LLMError("已取消")
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        is_https = u.scheme == "https"
        port = u.port or (443 if is_https else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        if is_https:
            conn = http.client.HTTPSConnection(
                host, port, timeout=self.timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        with self._active_lock:
            self._active = conn
        status, raw = 0, ""
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            if self._aborted.is_set():
                raise LLMError("已取消") from e
            raise LLMError(f"连接失败: {e}") from e
        finally:
            try:
                conn.close()
            except Exception:
                pass
            with self._active_lock:
                if self._active is conn:
                    self._active = None
        if self._aborted.is_set():
            raise LLMError("已取消")
        if status >= 400:
            raise LLMError(f"HTTP {status}: {raw[:400]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"响应不是 JSON: {raw[:200]!r}") from e

    def chat(self, messages: list[dict], temperature: float | None = None) -> str:
        temp = self.temperature if temperature is None else temperature
        if self.provider == "ollama":
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temp},
            }
        else:
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temp,
                "stream": False,
            }
            if self.max_tokens > 0:
                payload["max_tokens"] = self.max_tokens
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                data = self._post(self._endpoint(), payload)
                self.last_usage = data.get("usage") or {}
                if self.provider == "ollama":
                    msg = data.get("message") or {}
                    content = msg.get("content", "")
                else:
                    choices = data.get("choices") or []
                    if not choices:
                        raise LLMError(f"空响应: {str(data)[:300]}")
                    msg = choices[0].get("message") or {}
                    content = msg.get("content") or choices[0].get("text") or ""
                    fr = choices[0].get("finish_reason")
                    if fr == "length":
                        raise LLMError("输出被截断（finish_reason=length）")
                if not content.strip():
                    raise LLMError("模型返回空内容")
                return content
            except LLMError as e:
                last = e
                if self._aborted.is_set() or "已取消" in str(e):
                    break
                if attempt < self.retries:
                    time.sleep(min(8.0, 1.2 * attempt))
            except Exception as e:  # noqa: BLE001
                last = e
                if self._aborted.is_set():
                    break
                if attempt < self.retries:
                    time.sleep(min(8.0, 1.2 * attempt))
        raise LLMError(str(last) if last else "未知错误")

    def list_models(self) -> list[str]:
        url = self._models_endpoint()
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with self._opener(url).open(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"获取模型列表失败: {e}") from e
        if isinstance(data, dict) and "data" in data:
            return [m.get("id", "") for m in data["data"] if m.get("id")]
        if isinstance(data, dict) and "models" in data:
            return [m.get("name", "") for m in data["models"] if m.get("name")]
        return []


# --------------------------------------------------------------------------
# 翻译引擎
# --------------------------------------------------------------------------


@dataclass
class TransCfg:
    batch_items: int = 24
    batch_chars: int = 2600
    workers: int = 2
    temperature: float = 0.2
    retries_batch: int = 2
    retries_single: int = 3
    on_fail: str = "keep"          # keep / error
    glossary: dict = field(default_factory=dict)
    system_prompt: str = SYSTEM_PROMPT
    use_cache: bool = True
    cancel: threading.Event = field(default_factory=threading.Event)


class Cache:
    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.data: dict[str, str] = {}
        self.dirty = False
        self._lock = threading.Lock()
        if enabled and path.exists():
            try:
                self.data = json.loads(path.read_text("utf-8"))
            except Exception:
                self.data = {}
        self.hits = 0

    def key(self, model: str, src: str, dst: str, system: str, text: str) -> str:
        return sha1("|".join([model, src, dst, sha1(system), text]))

    def get(self, k: str) -> Optional[str]:
        if not self.enabled:
            return None
        v = self.data.get(k)
        if v is not None:
            self.hits += 1
        return v

    def put(self, k: str, v: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self.data.get(k) != v:
                self.data[k] = v
                self.dirty = True

    def save(self) -> None:
        if not self.enabled or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception:
            pass


class Translator:
    def __init__(self, client: LLMClient, cfg: TransCfg, cache: Optional[Cache] = None):
        self.client = client
        self.cfg = cfg
        self.cache = cache
        self.stats = {"ok": 0, "kept": 0, "failed": 0, "skipped": 0,
                      "calls": 0, "cache": 0}
        self._lock = threading.Lock()

    # ---------- 公共 ----------
    def translate_doc(
        self,
        doc: SubDoc,
        src: str,
        dst: str,
        log: Callable[[str], None] = print,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        items: list[tuple[int, str]] = []
        for i, c in enumerate(doc.cues, 1):
            if not is_translatable(c.text):
                c.translated = c.text
                c.state = "skipped"
                c.note = "无需翻译"
                with self._lock:
                    self.stats["skipped"] += 1
                continue
            items.append((i, c.text))
        total = len(items)
        if not items:
            return
        done = 0
        for start in range(0, total, self.cfg.batch_items):
            if self.cfg.cancel.is_set():
                break
            chunk = items[start:start + self.cfg.batch_items]
            # 按字符预算再切
            sub: list[tuple[int, str]] = []
            chars = 0
            for it in chunk:
                if sub and chars + len(it[1]) > self.cfg.batch_chars:
                    res = self.translate_many(sub, src, dst)
                    self._apply(doc, res)
                    done += len(sub)
                    if progress:
                        progress(done, total)
                    sub, chars = [], 0
                sub.append(it)
                chars += len(it[1])
            if sub:
                res = self.translate_many(sub, src, dst)
                self._apply(doc, res)
                done += len(sub)
                if progress:
                    progress(min(done, total), total)
        for c in doc.cues:
            if c.state == "pending":
                c.translated = c.text
                c.state = "failed"
                c.note = "取消或未处理"

    def _apply(self, doc: SubDoc, res: dict[int, Optional[str]]) -> None:
        for i, c in enumerate(doc.cues, 1):
            if i not in res:
                continue
            t = res[i]
            if t is None:
                c.translated = c.text
                c.state = "kept"
                c.note = "翻译失败，保留原文"
                with self._lock:
                    self.stats["kept"] += 1
            else:
                c.translated = t
                c.state = "ok"
                with self._lock:
                    self.stats["ok"] += 1

    # ---------- 线上格式（wire: 纯文本 + || 换行，标签不进模型） ----------
    @staticmethod
    def _wire(text: str) -> WireText:
        return make_wire(text)

    @staticmethod
    def _unwire(w: WireText, trans: str) -> str:
        return apply_wire(w, trans)

    def _cache_valid(self, src_text: str, hit: str) -> bool:
        if not hit or looks_like_noise(hit):
            return False
        w1 = make_wire(src_text)
        w2 = make_wire(hit)
        if len(w1.tags) != len(w2.tags):
            return False
        if w1.n_breaks != w2.n_breaks:
            return False
        return True

    # ---------- 批处理 ----------
    def translate_many(
        self, items: list[tuple[int, str]], src: str, dst: str
    ) -> dict[int, Optional[str]]:
        if not items:
            return {}
        # 去重：同文本只翻一次
        uniq: dict[str, list[int]] = {}
        for i, t in items:
            uniq.setdefault(t, []).append(i)
        reps = [(ids[0], t) for t, ids in uniq.items()]
        out: dict[int, Optional[str]] = {}
        todo: list[tuple[int, WireText, str]] = []
        for i, t in reps:
            hit = None
            if self.cache:
                hit = self.cache.get(self.cache.key(self.client.model, src, dst,
                                                     self.cfg.system_prompt, t))
            if hit is not None and self._cache_valid(t, hit):
                out[i] = hit
                with self._lock:
                    self.stats["cache"] += 1
            else:
                todo.append((i, self._wire(t), t))
        if todo:
            got = self._translate_batch(todo, src, dst)
            for i, w, t in todo:
                if i not in got:
                    continue
                final_text = self._unwire(w, got[i])
                out[i] = final_text
                if self.cache:
                    self.cache.put(self.cache.key(self.client.model, src, dst,
                                                  self.cfg.system_prompt, t), final_text)
        # 分发到重复项
        result: dict[int, Optional[str]] = {}
        for t, ids in uniq.items():
            val = out.get(ids[0])
            for i in ids:
                result[i] = val
        return result

    def _translate_batch(
        self,
        batch: list[tuple[int, WireText, str]],
        src: str,
        dst: str,
        depth: int = 0,
    ) -> dict[int, str]:
        if not batch or self.cfg.cancel.is_set():
            return {}
        if depth > 8:
            return {}
        for attempt in range(self.cfg.retries_batch):
            if self.cfg.cancel.is_set():
                return {}
            try:
                raw = self._call(batch, src, dst, retry=attempt > 0)
            except LLMError:
                if self.cfg.cancel.is_set():
                    return {}
                time.sleep(min(6.0, 1.0 + attempt * 1.5))
                continue
            got, bad = self._check(batch, raw)
            if not bad:
                return got
            # 部分失败：只重发坏行
            if len(bad) < len(batch):
                sub = [it for it in batch if it[0] in bad]
                got.update(self._translate_batch(sub, src, dst, depth + 1))
                return got
            # 全失败：退出重试，走拆分
            break
        # 二分降级
        if len(batch) > 1:
            mid = len(batch) // 2
            a = self._translate_batch(batch[:mid], src, dst, depth + 1)
            b = self._translate_batch(batch[mid:], src, dst, depth + 1)
            a.update(b)
            return a
        # 单条：多试几次（太短的条目模型经常直接忽略，保持原样即可）
        one = batch[0]
        if len(one[1].plain) <= 5:
            return {}
        for attempt in range(self.cfg.retries_single):
            if self.cfg.cancel.is_set():
                return {}
            try:
                raw = self._call([one], src, dst, retry=True)
            except LLMError:
                if self.cfg.cancel.is_set():
                    return {}
                time.sleep(min(6.0, 1.0 + attempt))
                continue
            got, bad = self._check([one], raw)
            if not bad:
                return got
        return {}

    def _call(self, batch, src, dst, retry=False) -> str:
        msgs = build_batch_messages(
            [(i, w.plain) for i, w, _t in batch], src, dst, self.cfg.system_prompt,
            glossary=self.cfg.glossary, retry=retry,
        )
        with self._lock:
            self.stats["calls"] += 1
        return self._run_interruptible(lambda: self.client.chat(msgs))

    def _run_interruptible(self, fn: Callable[[], str]) -> str:
        """把阻塞请求放到 daemon 线程，主线程轮询取消标志——点停止 0.2s 内就能返回。

        Windows 上跨线程 shutdown/close 唤不醒阻塞的 recv（socket 被 makefile
        持有 _io_refs，close 只是标记），所以不依赖打断 socket，直接不等它。
        """
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["ok"] = fn()
            except BaseException as e:  # noqa: BLE001
                box["err"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()
        while th.is_alive():
            if self.cfg.cancel.is_set():
                raise LLMError("已取消")
            th.join(0.2)
        if "err" in box:
            raise box["err"]
        return box.get("ok", "")

    # ---------- 校验 ----------
    def _check(
        self, batch: list[tuple[int, WireText, str]], raw: str
    ) -> tuple[dict[int, str], list[int]]:
        got = parse_reply(raw)
        good: dict[int, str] = {}
        bad: list[int] = []
        for i, w, _t in batch:
            t = got.get(i)
            if self._validate_one(w, t):
                bad.append(i)
            else:
                good[i] = t
        return good, bad

    def _validate_one(self, w: WireText, t: Optional[str]) -> str:
        if t is None:
            return "missing"
        t = t.strip()
        if not t:
            return "empty"
        if looks_like_noise(t):
            return "noise"
        if t == w.plain.strip():
            if len(t) <= 4:            # OK. / Yes. 之类保持原样可接受
                return ""
            return "untranslated"
        src_len = len(w.plain.replace(BREAK, ""))
        out_len = len(t.replace(BREAK, ""))
        if src_len >= 15 and out_len < src_len * 0.12:
            return "too_short"
        return ""


def looks_like_noise(t: str) -> bool:
    low = t.strip().lower()
    if low.startswith(("translation", "here is", "sure,", "sure!", "of course",
                       "以下", "译文", "翻译", "好的")):
        return True
    if "```" in t or "-->" in t:
        return True
    if re.match(r"^\s*\d{1,2}:\d{2}:\d{2}", t):
        return True
    if re.match(r"^\s*(\d+)\s*[.、:：]\s*\D", t) and "|" not in t:
        return True
    return False


def parse_reply(raw: str) -> dict[int, str]:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"```[a-zA-Z0-9_-]*", "", raw)
    got: dict[int, str] = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,6})\s*[|｜]\s*(.*)$", line)
        if not m:
            m = re.match(r"^(\d{1,6})\s*[.、:：]\s*(.*)$", line)
        if not m:
            continue
        i = int(m.group(1))
        t = m.group(2).strip()
        if i not in got:
            got[i] = t
    return got


def is_translatable(text: str) -> bool:
    t = plain_text(text)
    t = t.replace(BRK, "").strip()
    if not t:
        return False
    letters = re.findall(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u4e00-\u9fff"
                         r"\u3040-\u30ff\uac00-\ud7af\u0600-\u06ff\u0e00-\u0e7f]", t)
    return len(letters) >= 2


def valid_translation(src: str, dst: str) -> bool:
    if not dst or not dst.strip():
        return False
    if looks_like_noise(dst):
        return False
    src_ph = protect_tags(src)[1]
    if src_ph:
        if not set(range(len(src_ph))).issubset(set(placeholder_ids(dst))):
            return False
    if src.count(BRK) != dst.count(BRK):
        return False
    return True


# --------------------------------------------------------------------------
# 扫描与输出路径
# --------------------------------------------------------------------------


def scan_dirs(dirs: Iterable[str], recursive: bool = True,
              ext_set: Iterable[str] | None = None) -> list[Path]:
    exts = set(x.lower() for x in (ext_set or ALL_EXTS))
    found: list[Path] = []
    for d in dirs:
        p = Path(d)
        if not p.exists():
            continue
        if p.is_file():
            if p.suffix.lower() in exts:
                found.append(p)
            continue
        it = p.rglob("*") if recursive else p.glob("*")
        for f in it:
            if f.is_file() and f.suffix.lower() in exts:
                found.append(f)
    # 去重 + 排序
    seen: set[str] = set()
    out: list[Path] = []
    for f in sorted(found, key=lambda x: str(x).lower()):
        k = str(f.resolve()).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def output_path(src: Path, dst_lang: str, suffix: str = "", outdir: str = "") -> Path:
    suf = suffix or dst_lang
    name = f"{src.stem}.{suf}{src.suffix}"
    base = Path(outdir) if outdir else src.parent
    return base / name


# --------------------------------------------------------------------------
# 流程
# --------------------------------------------------------------------------


def make_client(cfg: dict) -> LLMClient:
    return LLMClient(
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model", ""),
        api_key=cfg.get("api_key", ""),
        provider=cfg.get("provider", "openai"),
        timeout=float(cfg.get("timeout", 180)),
        temperature=float(cfg.get("temperature", 0.2)),
        max_tokens=int(cfg.get("max_tokens", 0)),
        retries=int(cfg.get("http_retries", 3)),
    )


def detect_doc_language(doc: SubDoc, client: LLMClient | None, log=print) -> str:
    text = sample_text(doc)
    code, conf = guess_lang(text)
    if conf >= 0.7 or client is None:
        log(f"  语言: {code} (本地判定, 置信度 {conf:.2f})")
        return code
    try:
        ans = client.chat(
            [{"role": "system", "content": LANG_DETECT_PROMPT},
             {"role": "user", "content": text[:2000]}],
            temperature=0.0,
        ).strip()
        m = re.search(r"\b([a-z]{2,3}(?:-[A-Z]{2})?)\b", ans)
        if m:
            log(f"  语言: {m.group(1)} (LLM 判定)")
            return m.group(1)
    except Exception as e:  # noqa: BLE001
        log(f"  LLM 语言识别失败: {e}")
    log(f"  语言: {code} (回退)")
    return code


def run_pipeline(cfg: dict, log=print,
                 progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> dict:
    cancel = cancel or threading.Event()
    client = make_client(cfg)
    watch_done = threading.Event()

    def _watch_cancel() -> None:
        """点停止后立刻掐断在途 HTTP 请求，而不是等它超时。"""
        while not watch_done.is_set():
            if cancel.wait(0.2):
                client.abort()
                try:
                    log("已请求停止，正在中断当前请求…")
                except Exception:
                    pass
                return

    threading.Thread(target=_watch_cancel, daemon=True).start()
    cache = Cache(CACHE_PATH, enabled=cfg.get("use_cache", True))
    tcfg = TransCfg(
        batch_items=int(cfg.get("batch_items", 24)),
        batch_chars=int(cfg.get("batch_chars", 2600)),
        workers=int(cfg.get("workers", 2)),
        temperature=float(cfg.get("temperature", 0.2)),
        on_fail=cfg.get("on_fail", "keep"),
        glossary=cfg.get("glossary") or {},
        system_prompt=cfg.get("system_prompt") or SYSTEM_PROMPT,
        use_cache=cfg.get("use_cache", True),
        cancel=cancel,
    )
    outdir = (cfg.get("outdir") or "").strip()
    if outdir:
        try:
            Path(outdir).mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            log(f"输出目录创建失败: {e}")
    files = scan_dirs(cfg.get("dirs") or [], cfg.get("recursive", True),
                      cfg.get("exts") or ALL_EXTS)
    if not files:
        log("没有找到字幕文件。")
        watch_done.set()
        return {"files": 0}
    log(f"找到 {len(files)} 个字幕文件。")
    dst = cfg.get("dst", "zh-CN")
    src_cfg = cfg.get("src", "auto")
    stats = {"files": len(files), "ok": 0, "kept": 0, "failed": 0, "skipped": 0}
    done_files = 0
    lock = threading.Lock()

    def one(path: Path) -> None:
        nonlocal done_files
        if cancel.is_set():
            return
        try:
            out = output_path(path, dst, cfg.get("suffix", ""), cfg.get("outdir", ""))
            if out.exists() and not cfg.get("overwrite", False):
                log(f"[跳过] 已存在: {out.name}")
                with lock:
                    stats["skipped"] += 1
                return
            doc = load_subdoc(path)
            src = src_cfg
            if src in ("", "auto"):
                log(f"[识别] {path.name}")
                src = detect_doc_language(doc, client, log)
            log(f"[翻译] {path.name}  ({len(doc.cues)} 条, {src} -> {dst})")
            tr = Translator(client, tcfg, cache)
            tr.translate_doc(doc, src, dst, log=log)
            save_subdoc(doc, out)
            with lock:
                stats["ok"] += tr.stats["ok"]
                stats["kept"] += tr.stats["kept"]
                stats["failed"] += tr.stats["failed"]
                stats["skipped"] += tr.stats["skipped"]
                done_files += 1
            log(f"[完成] {out.name}  正常 {tr.stats['ok']} / 保留 {tr.stats['kept']} "
                f"/ 无需翻译 {tr.stats['skipped']} / 失败 {tr.stats['failed']} "
                f"/ 缓存 {tr.stats['cache']}")
        except Exception as e:  # noqa: BLE001
            log(f"[错误] {path.name}: {e}")
            with lock:
                stats["failed"] += 1

    if tcfg.workers <= 1:
        for f in files:
            one(f)
            if progress:
                progress(done_files, len(files))
    else:
        with cf.ThreadPoolExecutor(max_workers=tcfg.workers) as ex:
            futs = {ex.submit(one, f): f for f in files}
            for fu in cf.as_completed(futs):
                if progress:
                    progress(done_files, len(files))
                if cancel.is_set():
                    for x in futs:
                        x.cancel()
                    break
    cache.save()
    watch_done.set()
    log(f"全部结束：文件 {stats['files']}，正常 {stats['ok']}，保留 {stats['kept']}，"
        f"失败 {stats['failed']}，跳过 {stats['skipped']}")
    return stats


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,500
Hello, world!

2
00:00:04,000 --> 00:00:06,200
<i>This</i> is a test.

3
00:00:07,000 --> 00:00:09,000
Second line
after break.
"""

SAMPLE_ASS = """[Script Info]
Title: Test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\\i1}Hello{\\i0}, world!
Dialogue: 0,0:00:04.00,0:00:06.20,Default,,0,0,0,,{\\an8}Top line\\Nsecond line
"""

SAMPLE_VTT = """WEBVTT - test

NOTE this is a note

00:00:01.000 --> 00:00:03.500 align:middle
Hello, world!

00:00:04.000 --> 00:00:06.200
<v Bob>This is a test.</v>
"""

SAMPLE_LRC = """[ti:Test Song]
[ar:Someone]
[offset:0]
[00:01.00]Hello, world!
[00:04.00][00:07.00]This is a test.
"""

SAMPLE_SMI = """<SAMI>
<HEAD><TITLE>Test</TITLE></HEAD>
<BODY>
<SYNC Start=1000><P Class=KRCC>Hello, world!
<SYNC Start=4000><P Class=KRCC>This is a test.
<SYNC Start=7000><P Class=KRCC>&nbsp;
</BODY>
</SAMI>
"""

SAMPLE_SBV = """0:00:01.000,0:00:03.500
Hello, world!

0:00:04.000,0:00:06.200
This is a test.
"""

SAMPLE_SUB = """{25}{87}Hello, world!
{100}{155}This is a test.
"""

SAMPLE_MPL2 = """[10][35]Hello, world!
[40][62]This is a test.
"""

SAMPLE_TTML = """<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml">
<body><div>
<p begin="00:00:01.000" end="00:00:03.500">Hello, world!</p>
<p begin="00:00:04.000" end="00:00:06.200">This is a test.</p>
</div></body>
</tt>
"""

SAMPLES = {
    "srt": SAMPLE_SRT, "ass": SAMPLE_ASS, "vtt": SAMPLE_VTT, "lrc": SAMPLE_LRC,
    "smi": SAMPLE_SMI, "sbv": SAMPLE_SBV, "sub": SAMPLE_SUB,
    "mpl2": SAMPLE_MPL2, "ttml": SAMPLE_TTML,
}


class GoodMockClient:
    """模拟一个听话的模型：保留 ID、占位符、⏎，并且只输出 ID|译文。"""

    model = "mock-good"
    provider = "mock"

    def __init__(self, prefix="【译】"):
        self.prefix = prefix
        self.calls = 0

    def _tr(self, t: str) -> str:
        parts = re.split(rf"({PH_OPEN}\d+{PH_CLOSE}|{BRK}|\|\|)", t)
        acc: list[str] = []
        for p in parts:
            if not p:
                continue
            if p in (BRK, BREAK) or re.fullmatch(rf"{PH_OPEN}\d+{PH_CLOSE}", p):
                acc.append(p)
            else:
                s = p.strip()
                if s:
                    acc.append(self.prefix + s)
        return "".join(acc)

    def chat(self, messages, temperature=None):
        self.calls += 1
        user = messages[-1]["content"]
        out = []
        for line in user.split("\n"):
            m = re.match(r"^(\d+)\|(.*)$", line)
            if not m:
                continue
            out.append(f"{m.group(1)}|{self._tr(m.group(2))}")
        return "\n".join(out)


class BadMockClient(GoodMockClient):
    """模拟小模型的常见翻车：丢行 / 加前言 / 改分隔符 / 改 ID / 丢占位符。"""

    def __init__(self, seed=7, rate=0.18):
        super().__init__("【译】")
        import random
        self.rng = random.Random(seed)
        self.rate = rate

    def chat(self, messages, temperature=None):
        raw = super().chat(messages, temperature)
        lines = raw.split("\n")
        out = []
        for ln in lines:
            r = self.rng.random()
            if r < self.rate * 0.3:
                continue                                   # 丢行
            if r < self.rate * 0.5:
                ln = ln.replace("|", ". ", 1)              # 改分隔符
            elif r < self.rate * 0.7:
                ln = ln + "。"                              # 多余标点
            elif r < self.rate * 0.85:
                ln = re.sub(rf"{PH_OPEN}\d+{PH_CLOSE}", "", ln)  # 丢占位符
            out.append(ln)
        if self.rng.random() < 0.4:
            out.insert(0, "Here is the translation:")      # 加前言
        return "\n".join(out)


def selftest(verbose: bool = True) -> int:
    ok_count = 0
    fail_count = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok_count, fail_count
        if cond:
            ok_count += 1
            if verbose:
                print(f"  PASS  {name}")
        else:
            fail_count += 1
            print(f"  FAIL  {name}  {detail}")

    print("== 1. 格式往返（解析 -> 渲染 -> 再解析） ==")
    for key, text in SAMPLES.items():
        tmp = Path(f"_selftest_{key}{'.' + key if key != 'smi' else '.smi'}")
        try:
            fmt = detect_format(tmp, text)
            check(f"{key}: 识别格式", fmt is not None and fmt.key == key,
                  f"got {fmt.key if fmt else None}")
            if not fmt:
                continue
            doc = fmt.parse(text, tmp)
            check(f"{key}: 解析出条目", doc is not None and len(doc.cues) >= 2,
                  f"cues={len(doc.cues) if doc else 0}")
            if not doc:
                continue
            rendered = fmt.render(doc)
            doc2 = fmt.parse(rendered, tmp)
            check(f"{key}: 往返条数一致", doc2 is not None and len(doc2.cues) == len(doc.cues),
                  f"{len(doc2.cues) if doc2 else 0} != {len(doc.cues)}")
            if doc2:
                same = all(a.start == b.start and a.end == b.end
                           for a, b in zip(doc.cues, doc2.cues))
                check(f"{key}: 往返时间轴一致", same)
                txt_same = all(plain_text(a.text) == plain_text(b.text)
                               for a, b in zip(doc.cues, doc2.cues))
                check(f"{key}: 往返文本一致", txt_same)
        except Exception as e:  # noqa: BLE001
            check(f"{key}: 无异常", False, repr(e))
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    print("== 2. 标签保护 ==")
    keys = [
        r"{\i1}Hello{\i0}\Nsecond line",
        "a <i>test</i> &amp; <font color=red>red</font>",
        "<00:00:01.000>karaoke<c.yellow>text</c>",
    ]
    for k in keys:
        masked, tokens = protect_tags(k)
        check(f"保护: {k[:22]!r}", PH_OPEN not in k and len(tokens) > 0)
        check(f"还原: {k[:22]!r}", restore_tags(masked, tokens) == k,
              f"{restore_tags(masked, tokens)!r}")

    print("== 3. 好模型：全格式端到端 ==")
    client = GoodMockClient()
    for key, text in SAMPLES.items():
        ext = "." + ("smi" if key == "smi" else key)
        tmp = Path(f"_selftest_{key}{ext}")
        try:
            fmt = detect_format(tmp, text)
            doc = fmt.parse(text, tmp)
            if not doc:
                check(f"{key}: 解析", False)
                continue
            tr = Translator(client, TransCfg(batch_items=10, use_cache=False))
            tr.translate_doc(doc, "en", "zh-CN")
            rendered = fmt.render(doc)
            doc2 = fmt.parse(rendered, tmp)
            check(f"{key}: 翻译后结构完好",
                  doc2 is not None and len(doc2.cues) == len(doc.cues))
            translated = sum(1 for c in doc.cues if c.state == "ok")
            check(f"{key}: 有译文", translated >= len(doc.cues) - 1,
                  f"{translated}/{len(doc.cues)}")
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    print("== 4. 坏模型：校验与降级重试 ==")
    bad = BadMockClient(seed=3, rate=0.35)
    text = SAMPLE_SRT
    fmt = SrtFormat
    doc = fmt.parse(text, Path("x.srt"))
    tr = Translator(bad, TransCfg(batch_items=10, retries_batch=3, retries_single=4,
                                  use_cache=False))
    tr.translate_doc(doc, "en", "zh-CN")
    rendered = fmt.render(doc)
    doc2 = fmt.parse(rendered, Path("x.srt"))
    check("坏模型: 输出仍可解析", doc2 is not None)
    check("坏模型: 条数不变", doc2 is not None and len(doc2.cues) == len(doc.cues))
    check("坏模型: 至少救回大部分", tr.stats["ok"] >= len(doc.cues) - 1,
          f"ok={tr.stats['ok']} kept={tr.stats['kept']}")
    for c in doc.cues:
        if c.translated:
            check(f"坏模型: 无脏输出 {c.translated[:18]!r}",
                  not looks_like_noise(c.translated))

    print("== 5. 解析器容错 ==")
    check("时间码 . 与 ,", parse_time_ms("00:00:01.500") == 1500)
    check("时间码 无小时", parse_time_ms("01:02.50") == 62500)
    got = parse_reply("1|你好\n2. 世界\n```")
    check("容错解析 1.", got.get(1) == "你好" and got.get(2) == "世界")
    check("噪声识别", looks_like_noise("Here is the translation:") and
          looks_like_noise("Translation: hello") and not looks_like_noise("Hello there"))
    check("语言: 中文", guess_lang("这是一个中文句子，用于测试。")[0] == "zh-CN")
    check("语言: 日文", guess_lang("これはテストです。")[0] == "ja")
    check("语言: 英文", guess_lang("This is an English sentence for testing.")[0] == "en")

    print(f"\n结果: {ok_count} 通过, {fail_count} 失败")
    return 0 if fail_count == 0 else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="subtranslate",
        description="单文件字幕翻译工具（支持远端/本地 LLM）",
    )
    p.add_argument("dirs", nargs="*", help="要扫描的目录（可多个）")
    p.add_argument("--cli", action="store_true", help="命令行模式（默认启动 GUI）")
    p.add_argument("--selftest", action="store_true", help="运行内置自检")
    p.add_argument("--to", default="zh-CN", help="目标语言 (默认 zh-CN)")
    p.add_argument("--from", dest="src", default="auto", help="源语言 (默认 auto)")
    p.add_argument("--provider", default="openai", choices=["openai", "ollama"])
    p.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    p.add_argument("--api-key", default="")
    p.add_argument("--model", default="")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--batch", type=int, default=24, help="每批条数")
    p.add_argument("--batch-chars", type=int, default=2600)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--suffix", default="", help="输出后缀（默认=目标语言）")
    p.add_argument("--outdir", default="")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--exts", default="", help="只处理这些扩展名，逗号分隔")
    p.add_argument("--dry-run", action="store_true", help="只扫描，不调用模型")
    p.add_argument("--max-tokens", type=int, default=0)
    p.add_argument("--list-models", action="store_true")
    return p


def cfg_from_args(a) -> dict:
    exts = [x.strip() for x in a.exts.split(",") if x.strip()] if a.exts else ALL_EXTS
    return {
        "dirs": a.dirs, "src": a.src, "dst": a.to,
        "provider": a.provider, "base_url": a.base_url, "api_key": a.api_key,
        "model": a.model, "temperature": a.temperature,
        "batch_items": a.batch, "batch_chars": a.batch_chars,
        "workers": a.workers, "suffix": a.suffix, "outdir": a.outdir,
        "overwrite": a.overwrite, "recursive": not a.no_recursive,
        "use_cache": not a.no_cache, "exts": exts,
        "max_tokens": a.max_tokens,
    }


def main_cli(a) -> int:
    if a.list_models:
        c = make_client(cfg_from_args(a))
        for m in c.list_models():
            print(m)
        return 0
    if a.dry_run:
        files = scan_dirs(a.dirs, not a.no_recursive,
                          [x.strip() for x in a.exts.split(",")] if a.exts else ALL_EXTS)
        print(f"找到 {len(files)} 个文件：")
        for f in files:
            print(" ", f)
        return 0
    cfg = cfg_from_args(a)
    if not cfg["model"]:
        print("错误：需要 --model", file=sys.stderr)
        return 2
    run_pipeline(cfg)
    return 0


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

DEFAULT_CFG = {
    "provider": "openai", "base_url": "http://127.0.0.1:1234/v1",
    "api_key": "", "model": "", "src": "auto", "dst": "zh-CN",
    "suffix": "", "outdir": "", "overwrite": False, "recursive": True,
    "batch_items": 24, "batch_chars": 2600, "workers": 2,
    "temperature": 0.2, "max_tokens": 0, "use_cache": True,
    "system_prompt": SYSTEM_PROMPT, "dirs": [],
}

PRESETS = {
    "LM Studio (本地)": ("openai", "http://127.0.0.1:1234/v1"),
    "Ollama (本地)": ("ollama", "http://127.0.0.1:11434"),
    "OpenAI": ("openai", "https://api.openai.com/v1"),
    "DeepSeek": ("openai", "https://api.deepseek.com/v1"),
    "自定义": (None, None),
}


_MODEL_NOISE = re.compile(
    r"embed|rerank|bge|e5-|ocr|clip|whisper|tts|diffusion|joycaption|vl-", re.I
)


def pick_default_model(values: list[str]) -> str:
    """优先挑一个像聊天/翻译的模型，别默认选到 embedding。"""
    for v in values:
        if not _MODEL_NOISE.search(v):
            return v
    return values[0] if values else ""


class DropTarget:
    """给 tkinter 窗口挂 Windows 文件拖放（WM_DROPFILES，纯 ctypes，零依赖）。"""

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def __init__(self, widget, callback, log=None):
        self.widget = widget
        self.callback = callback
        self.log = log or (lambda m: None)
        self.ok = False
        self._keep = []
        if sys.platform != "win32":
            return
        try:
            self._install()
            self.ok = True
        except Exception as e:  # noqa: BLE001
            self.log(f"拖放不可用: {e}")

    def _install(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                     ctypes.c_size_t, ctypes.c_ssize_t)
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragAcceptFiles.restype = None
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           wintypes.LPWSTR, wintypes.UINT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragFinish.argtypes = [wintypes.HANDLE]
        shell32.DragFinish.restype = None
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                           ctypes.c_size_t, ctypes.c_ssize_t]

        hwnd = self.widget.winfo_id()
        shell32.DragAcceptFiles(hwnd, True)

        def read_drop(hdrop) -> list[str]:
            count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            out = []
            for i in range(count):
                n = shell32.DragQueryFileW(hdrop, i, None, 0) + 1
                buf = ctypes.create_unicode_buffer(n)
                shell32.DragQueryFileW(hdrop, i, buf, n)
                out.append(buf.value)
            return out

        def proc(hwnd_, msg, wparam, lparam):
            if msg == self.WM_DROPFILES:
                try:
                    files = read_drop(wparam)
                except Exception:
                    files = []
                finally:
                    try:
                        shell32.DragFinish(wparam)
                    except Exception:
                        pass
                if files:
                    try:
                        self.callback(files)
                    except Exception as e:  # noqa: BLE001
                        self.log(f"处理拖放失败: {e}")
                return 0
            if not self._old:
                return 0
            return user32.CallWindowProcW(self._old, hwnd_, msg, wparam, lparam)

        self._proc = WNDPROC(proc)
        if hasattr(user32, "SetWindowLongPtrW"):
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                                 ctypes.c_void_p]
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            old = user32.GetWindowLongPtrW(hwnd, self.GWLP_WNDPROC)
            user32.SetWindowLongPtrW(hwnd, self.GWLP_WNDPROC, self._proc)
        else:
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                              ctypes.c_long]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            old = user32.GetWindowLongW(hwnd, self.GWLP_WNDPROC)
            user32.SetWindowLongW(hwnd, self.GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p).value)
        self._old = ctypes.c_void_p(old) if old else None
        self._keep.append(self._proc)


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        root.title(f"{APP} {VERSION} — 字幕翻译")
        root.geometry("1120x860")
        root.minsize(980, 720)
        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(self.load_config())
        self.cancel = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.dirs: list[str] = list(self.cfg.get("dirs") or [])
        self.running = False
        self._models_busy = False
        self._build()
        self._fill()
        self._install_drop()
        self.root.after(100, self.poll)      # 常驻轮询：刷新模型/拖放都靠它消费队列

    # ---------- 配置 ----------
    def load_config(self) -> dict:
        try:
            return json.loads(CONFIG_PATH.read_text("utf-8"))
        except Exception:
            return {}

    def save_config(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.cfg["dirs"] = list(self.dirs)
            CONFIG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), "utf-8")
        except Exception as e:
            self.log(f"保存配置失败: {e}")

    # ---------- UI ----------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        pad = {"padx": 6, "pady": 3}

        top = ttk.LabelFrame(self.root, text="输入（目录或单个文件；可直接把文件/文件夹拖进窗口）")
        top.pack(fill="x", padx=8, pady=(8, 4))
        row = ttk.Frame(top)
        row.pack(fill="x", padx=6, pady=4)
        self.lst_dirs = tk.Listbox(row, height=5, selectmode="extended")
        self.lst_dirs.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(row, command=self.lst_dirs.yview)
        sb.pack(side="left", fill="y")
        self.lst_dirs.config(yscrollcommand=sb.set)
        btns = ttk.Frame(row)
        btns.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(btns, text="添加目录…", command=self.add_dir).pack(fill="x", pady=2)
        ttk.Button(btns, text="添加文件…", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="移除选中", command=self.remove_dir).pack(fill="x", pady=2)
        ttk.Button(btns, text="清空", command=self.clear_dirs).pack(fill="x", pady=2)
        ttk.Button(btns, text="扫描预览", command=self.preview).pack(fill="x", pady=2)

        mid = ttk.Frame(self.root)
        mid.pack(fill="x", padx=8, pady=4)

        api = ttk.LabelFrame(mid, text="模型接口")
        api.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.var_preset = tk.StringVar(value="LM Studio (本地)")
        self.var_provider = tk.StringVar(value=self.cfg.get("provider", "openai"))
        self.var_base = tk.StringVar(value=self.cfg.get("base_url", ""))
        self.var_key = tk.StringVar(value=self.cfg.get("api_key", ""))
        self.var_model = tk.StringVar(value=self.cfg.get("model", ""))

        ttk.Label(api, text="预设").grid(row=0, column=0, sticky="e", **pad)
        cb = ttk.Combobox(api, textvariable=self.var_preset, values=list(PRESETS),
                          state="readonly", width=16)
        cb.grid(row=0, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", self.on_preset)
        ttk.Label(api, text="协议").grid(row=0, column=2, sticky="e", **pad)
        ttk.Combobox(api, textvariable=self.var_provider, values=["openai", "ollama"],
                     state="readonly", width=8).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(api, text="Base URL").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(api, textvariable=self.var_base, width=48).grid(
            row=1, column=1, columnspan=3, sticky="we", **pad)
        ttk.Label(api, text="API Key").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(api, textvariable=self.var_key, show="*", width=48).grid(
            row=2, column=1, columnspan=3, sticky="we", **pad)
        ttk.Label(api, text="模型").grid(row=3, column=0, sticky="e", **pad)
        self.cb_model = ttk.Combobox(api, textvariable=self.var_model, width=34)
        self.cb_model.grid(row=3, column=1, sticky="we", **pad)
        self.btn_models = ttk.Button(api, text="刷新模型", command=self.fetch_models,
                                     width=10)
        self.btn_models.grid(row=3, column=2, sticky="w", **pad)
        self.var_temp = tk.DoubleVar(value=float(self.cfg.get("temperature", 0.2)))
        ttk.Label(api, text="温度").grid(row=3, column=3, sticky="e", **pad)
        ttk.Spinbox(api, from_=0.0, to=1.5, increment=0.1, width=6,
                    textvariable=self.var_temp).grid(row=3, column=4, sticky="w", **pad)
        api.columnconfigure(1, weight=1)

        lang = ttk.LabelFrame(mid, text="语言与输出")
        lang.pack(side="left", fill="both", expand=False, padx=(4, 0))
        self.var_src = tk.StringVar(value=self.cfg.get("src", "auto"))
        self.var_dst = tk.StringVar(value=self.cfg.get("dst", "zh-CN"))
        self.var_suffix = tk.StringVar(value=self.cfg.get("suffix", ""))
        self.var_overwrite = tk.BooleanVar(value=bool(self.cfg.get("overwrite")))
        self.var_recursive = tk.BooleanVar(value=bool(self.cfg.get("recursive", True)))
        self.var_cache = tk.BooleanVar(value=bool(self.cfg.get("use_cache", True)))
        names = [LANG_NAMES[k] for k in LANG_NAMES]
        code2name = {v: k for k, v in LANG_NAMES.items()}
        self.code2name, self.names_all = code2name, names

        ttk.Label(lang, text="源语言").grid(row=0, column=0, sticky="e", **pad)
        self.cb_src = ttk.Combobox(lang, values=names, state="readonly", width=14)
        self.cb_src.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(lang, text="目标语言").grid(row=1, column=0, sticky="e", **pad)
        self.cb_dst = ttk.Combobox(lang, values=names[1:], state="readonly", width=14)
        self.cb_dst.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(lang, text="输出后缀").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(lang, textvariable=self.var_suffix, width=16).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(lang, text="已存在则覆盖", variable=self.var_overwrite).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(lang, text="递归子目录", variable=self.var_recursive).grid(
            row=4, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(lang, text="启用翻译缓存", variable=self.var_cache).grid(
            row=5, column=0, columnspan=2, sticky="w", **pad)
        self.var_batch = tk.IntVar(value=int(self.cfg.get("batch_items", 24)))
        self.var_workers = tk.IntVar(value=int(self.cfg.get("workers", 2)))
        ttk.Label(lang, text="批大小").grid(row=6, column=0, sticky="e", **pad)
        ttk.Spinbox(lang, from_=1, to=100, width=8, textvariable=self.var_batch).grid(
            row=6, column=1, sticky="w", **pad)
        ttk.Label(lang, text="并发").grid(row=7, column=0, sticky="e", **pad)
        ttk.Spinbox(lang, from_=1, to=8, width=8, textvariable=self.var_workers).grid(
            row=7, column=1, sticky="w", **pad)

        outbox = ttk.LabelFrame(self.root, text="输出位置（留空 = 与源字幕同目录）")
        outbox.pack(fill="x", padx=8, pady=4)
        self.var_outdir = tk.StringVar(value=self.cfg.get("outdir", ""))
        ttk.Entry(outbox, textvariable=self.var_outdir).pack(
            side="left", fill="x", expand=True, padx=(8, 6), pady=6)
        ttk.Button(outbox, text="浏览…", command=self.pick_outdir, width=8).pack(
            side="left", padx=2)
        ttk.Button(outbox, text="打开", command=self.open_outdir, width=6).pack(
            side="left", padx=2)
        ttk.Button(outbox, text="清空", command=lambda: self.var_outdir.set(""),
                   width=6).pack(side="left", padx=2)
        ttk.Label(outbox, text="命名：原名.后缀.扩展名", foreground="#777").pack(
            side="left", padx=(8, 8))

        pr = ttk.LabelFrame(self.root, text="系统提示词（可改；越短越硬越好）")
        pr.pack(fill="both", expand=False, padx=8, pady=4)
        self.txt_prompt = tk.Text(pr, height=9, wrap="word", undo=True)
        self.txt_prompt.pack(fill="both", expand=True, padx=6, pady=4)
        pbar = ttk.Frame(pr)
        pbar.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Button(pbar, text="恢复默认", command=self.reset_prompt).pack(side="left")
        ttk.Button(pbar, text="保存配置", command=self.save_config).pack(side="left", padx=6)

        run = ttk.Frame(self.root)
        run.pack(fill="x", padx=8, pady=4)
        self.btn_run = ttk.Button(run, text="开始翻译", command=self.start)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(run, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(run, text="测试连接", command=self.test_conn).pack(side="left", padx=6)
        self.prog = ttk.Progressbar(run, mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=8)
        self.lbl_stat = ttk.Label(run, text="就绪")
        self.lbl_stat.pack(side="left", padx=6)

        lg = ttk.LabelFrame(self.root, text="日志")
        lg.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.txt_log = tk.Text(lg, height=10, wrap="word", state="disabled")
        self.txt_log.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb2 = ttk.Scrollbar(lg, command=self.txt_log.yview)
        sb2.pack(side="right", fill="y")
        self.txt_log.config(yscrollcommand=sb2.set)

    def _fill(self) -> None:
        for d in self.dirs:
            self.lst_dirs.insert("end", d)
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", self.cfg.get("system_prompt") or SYSTEM_PROMPT)
        src = LANG_NAMES.get(self.cfg.get("src", "auto"), "自动识别")
        dst = LANG_NAMES.get(self.cfg.get("dst", "zh-CN"), "简体中文")
        self.cb_src.set(src)
        self.cb_dst.set(dst)
        for name, (prov, url) in PRESETS.items():
            if url and url == self.cfg.get("base_url"):
                self.var_preset.set(name)
                break

    # ---------- 拖放 ----------
    def _install_drop(self) -> None:
        # 窗口过程里绝不能碰 tkinter 控件（Tcl 不可重入），只入队
        def enqueue(files: list[str]) -> None:
            self.q.put(("drop", files))

        self._drop_root = DropTarget(self.root, enqueue, self.log)
        self._drop_list = DropTarget(self.lst_dirs, enqueue, self.log)
        if self._drop_root.ok or self._drop_list.ok:
            self.log("拖放已启用：把字幕文件或文件夹拖进窗口即可")
        else:
            self.log("拖放不可用（仅 Windows 支持），请用「添加目录/添加文件」按钮")

    def on_drop(self, paths: list[str]) -> None:
        added = skipped = 0
        exts = set(ALL_EXTS)
        for raw in paths:
            p = os.path.normpath(raw)
            if p in self.dirs:
                continue
            if os.path.isdir(p):
                self.dirs.append(p)
                self.lst_dirs.insert("end", p)
                added += 1
            elif os.path.isfile(p):
                if Path(p).suffix.lower() in exts:
                    self.dirs.append(p)
                    self.lst_dirs.insert("end", p)
                    added += 1
                else:
                    skipped += 1
                    self.log(f"跳过非字幕文件: {os.path.basename(p)}")
            else:
                skipped += 1
        msg = f"拖入 {len(paths)} 项，新增 {added} 项"
        if skipped:
            msg += f"，忽略 {skipped} 项"
        self.log(msg)

    def add_files(self) -> None:
        from tkinter import filedialog
        patterns = " ".join("*" + e for e in ALL_EXTS)
        files = filedialog.askopenfilenames(
            title="选择字幕文件（可多选）",
            filetypes=[("字幕文件", patterns), ("所有文件", "*.*")],
        )
        if files:
            self.on_drop(list(files))

    def pick_outdir(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_outdir.set(d)

    def open_outdir(self) -> None:
        d = (self.var_outdir.get() or "").strip()
        if not d:
            d = self.dirs[0] if self.dirs else ""
        if d and os.path.isdir(d):
            try:
                os.startfile(d)  # noqa: S606
            except Exception as e:  # noqa: BLE001
                self.log(f"打开目录失败: {e}")
        else:
            self.log("输出目录为空或不存在")

    # ---------- 事件 ----------
    def add_dir(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(title="选择字幕所在目录")
        if d:
            self.dirs.append(d)
            self.lst_dirs.insert("end", d)

    def remove_dir(self) -> None:
        for i in reversed(self.lst_dirs.curselection()):
            self.lst_dirs.delete(i)
            del self.dirs[i]

    def clear_dirs(self) -> None:
        self.lst_dirs.delete(0, "end")
        self.dirs.clear()

    def preview(self) -> None:
        files = scan_dirs(self.dirs, self.var_recursive.get(), ALL_EXTS)
        self.log(f"预览：找到 {len(files)} 个文件")
        for f in files[:200]:
            self.log(f"  {f}")
        if len(files) > 200:
            self.log(f"  … 其余 {len(files) - 200} 个省略")

    def on_preset(self, _e=None) -> None:
        name = self.var_preset.get()
        prov, url = PRESETS.get(name, (None, None))
        if prov:
            self.var_provider.set(prov)
        if url:
            self.var_base.set(url)

    def reset_prompt(self) -> None:
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", SYSTEM_PROMPT)

    def log(self, msg: str) -> None:
        self.q.put(("log", msg))

    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        cfg.update({
            "dirs": list(self.dirs),
            "provider": self.var_provider.get(),
            "base_url": self.var_base.get().strip(),
            "api_key": self.var_key.get().strip(),
            "model": self.var_model.get().strip(),
            "temperature": float(self.var_temp.get()),
            "src": self.code2name.get(self.cb_src.get(), "auto"),
            "dst": self.code2name.get(self.cb_dst.get(), "zh-CN"),
            "suffix": self.var_suffix.get().strip(),
            "outdir": self.var_outdir.get().strip(),
            "overwrite": bool(self.var_overwrite.get()),
            "recursive": bool(self.var_recursive.get()),
            "use_cache": bool(self.var_cache.get()),
            "batch_items": int(self.var_batch.get()),
            "workers": int(self.var_workers.get()),
            "system_prompt": self.txt_prompt.get("1.0", "end").strip() or SYSTEM_PROMPT,
        })
        return cfg

    def test_conn(self) -> None:
        cfg = self._collect()
        self.log(f"测试连接：{cfg['base_url']} / {cfg['model'] or '(未填模型)'}")

        def work():
            try:
                c = make_client(cfg)
                models = c.list_models()
                self.q.put(("log", f"连接成功，模型 {len(models)} 个：{', '.join(models[:8])}"))
                if not cfg["model"] and models:
                    self.q.put(("model", models[0]))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"连接失败: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def fetch_models(self) -> None:
        if self._models_busy:
            return
        self._models_busy = True
        self.btn_models.config(state="disabled", text="拉取中…")
        self.lbl_stat.config(text="正在拉取模型列表…")
        cfg = self._collect()

        def work():
            try:
                c = make_client(cfg)
                models = c.list_models()
                self.q.put(("models", models))
            except Exception as e:  # noqa: BLE001
                self.q.put(("models_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _open_model_dropdown(self) -> None:
        try:
            self.cb_model.focus_set()
            self.cb_model.event_generate("<Down>")
        except Exception:
            pass

    def start(self) -> None:
        if self.running:
            return
        cfg = self._collect()
        if not cfg["dirs"]:
            self.log("请先添加目录")
            return
        if not cfg["model"]:
            self.log("请先填模型名")
            return
        self.save_config()
        self.cancel.clear()
        self.running = True
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.prog["value"] = 0
        self.lbl_stat.config(text="运行中…")

        def work():
            try:
                run_pipeline(
                    cfg,
                    log=lambda m: self.q.put(("log", m)),
                    progress=lambda d, t: self.q.put(("prog", (d, t))),
                    cancel=self.cancel,
                )
            except Exception:  # noqa: BLE001
                self.q.put(("log", "异常:\n" + traceback.format_exc()))
            finally:
                self.q.put(("done", None))

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        self.cancel.set()
        self.log("正在停止…")

    def poll(self) -> None:
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.txt_log.config(state="normal")
                    self.txt_log.insert("end", f"[{now_str()}] {val}\n")
                    self.txt_log.see("end")
                    self.txt_log.config(state="disabled")
                elif kind == "prog":
                    d, t = val
                    self.prog["maximum"] = max(1, t)
                    self.prog["value"] = d
                    self.lbl_stat.config(text=f"{d}/{t} 文件")
                elif kind == "models":
                    self._models_busy = False
                    self.btn_models.config(state="normal", text="刷新模型")
                    if val:
                        self.cb_model["values"] = val
                        if not self.var_model.get().strip():
                            self.var_model.set(pick_default_model(val))
                        self.lbl_stat.config(text=f"模型 {len(val)} 个")
                        self.log(f"拉到 {len(val)} 个模型：" +
                                 ", ".join(val[:12]) + ("…" if len(val) > 12 else ""))
                        self.root.after(100, self._open_model_dropdown)
                    else:
                        self.lbl_stat.config(text="就绪")
                        from tkinter import messagebox
                        messagebox.showinfo(APP, "接口没有返回任何模型。\n请检查 Base URL 和协议是否正确。")
                elif kind == "models_err":
                    self._models_busy = False
                    self.btn_models.config(state="normal", text="刷新模型")
                    self.lbl_stat.config(text="拉取失败")
                    self.log(f"拉取模型失败: {val}")
                    from tkinter import messagebox
                    messagebox.showerror("拉取模型失败", val)
                elif kind == "model":
                    self.var_model.set(val)
                elif kind == "drop":
                    self.on_drop(val)
                elif kind == "done":
                    self.running = False
                    self.btn_run.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.lbl_stat.config(text="完成")
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.after(120, self.poll)
        except Exception:
            pass


def _fatal(msg: str) -> None:
    """打包成 --windowed exe 时没有控制台：先落盘日志，再弹窗。"""
    try:
        print(msg, file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        logp = Path(os.environ.get("TEMP") or ".") / "SubTranslate_error.log"
        logp.write_text(f"{APP} {VERSION}\n{msg}\n", encoding="utf-8")
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, f"{APP} {VERSION}", 0x10)
            return
    except Exception:
        pass


def main_gui() -> int:
    try:
        import tkinter as tk
    except Exception as e:  # noqa: BLE001
        _fatal(f"无法启动 GUI（{e}）\n请改用 --cli 模式。")
        return 2
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        _fatal(f"无法创建窗口（{e}）")
        return 2
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    try:
        App(root)
        root.mainloop()
    except Exception:  # noqa: BLE001
        _fatal("启动失败：\n" + traceback.format_exc(limit=6))
        return 1
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # PyInstaller --windowed 下 stdout/stderr 是 None，先兜住
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in argv:
        return selftest()
    ap = build_cli()
    a = ap.parse_args(argv)
    if a.cli or a.dirs or a.selftest or a.list_models or a.dry_run:
        return main_cli(a)
    return main_gui()


if __name__ == "__main__":
    raise SystemExit(main())
