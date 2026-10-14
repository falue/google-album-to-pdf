#!/usr/bin/env python3
"""Turn a public Google Photos album into a clean PDF."""
import argparse
import functools
import io
import itertools
import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fpdf import FPDF

RPC_URL = "https://photos.google.com/_/PhotosUi/data/batchexecute"
ORDER_KEY = "101428965"  # position of photos and text in the album
TEXT_KEY = "99218341"  # text of an in-between title


@dataclass
class Photo:
    id: str
    url: str
    width: int
    height: int
    caption: str = ""  # reactions and comments

    @property
    def aspect(self):
        return self.width / self.height


@dataclass
class Section:
    title: str | None
    photos: list[Photo] = field(default_factory=list)


def rpc(rpc_id, args):
    req = json.dumps([[[rpc_id, json.dumps(args), None, "generic"]]])
    with urllib.request.urlopen(RPC_URL, urllib.parse.urlencode({"f.req": req}).encode()) as resp:
        line = next(l for l in resp.read().decode().splitlines() if l.startswith('[["wrb.fr"'))
    return json.loads(json.loads(line)[0][2])


def fetch_captions(album_id, key):
    """Return {photo id, or None for the album: caption}, from the album's activity feed."""
    reactions, comments = defaultdict(list), defaultdict(list)
    for a in reversed(rpc("wQ6iqd", [[album_id], None, key, [1]])[1]):  # feed is newest first
        if a[0] == 2:  # comment: [comment, 1, photo] or [comment, 2] for the album
            c = a[4][0]
            text = "".join(seg[1] for seg in c[2][0][0])
            comments[a[4][2][0] if a[4][1] == 1 else None].append(f"💬 {c[5][3][0]}: {text}")
        elif a[0] == 5:  # reaction: [reaction, 1, photo]
            r = a[7][0]
            reactions[a[7][2][0] if a[7][1] == 1 else None].append(f"{r[3]} {r[1][11][0]}")
    # U+FE0F (emoji style selector) has no glyph and would print as a gap
    return {t: "\n".join(filter(None, ["   ".join(reactions[t]), *comments[t]])).replace("\ufe0f", "")
            for t in reactions.keys() | comments.keys()}


def fetch_album(url, comments=True):
    """Return (album title, album caption, sections) of a public album link."""
    with urllib.request.urlopen(url) as resp:  # follows photos.app.goo.gl redirect
        share = urllib.parse.urlparse(resp.geturl())
    album_id = share.path.rstrip("/").split("/")[-1]
    key = urllib.parse.parse_qs(share.query)["key"][0]
    captions = fetch_captions(album_id, key) if comments else {}

    entries, texts, token = [], {}, None
    while True:
        data = rpc("snAcKc", [album_id, token, None, key])
        title = data[3][1]
        for item in data[1]:  # videos included: their url serves the thumbnail
            url, w, h = item[1][:3]
            photo = Photo(item[0], url, w, h, captions.get(item[0], ""))
            entries.append((item[-1].get(ORDER_KEY, [0, ""])[1], photo))
        for text_id, meta in data[4] or []:
            if TEXT_KEY in meta:
                texts[text_id] = (meta[ORDER_KEY][1], "".join(seg[1][0] for seg in meta[TEXT_KEY]))
        token = data[2]
        if not token:
            break

    sections = [Section(None)]
    for _, entry in sorted(entries + list(texts.values()), key=lambda e: e[0]):
        if isinstance(entry, str):
            sections.append(Section(entry))
        else:
            sections[-1].photos.append(entry)
    return title, captions.get(None, ""), [s for s in sections if s.title or s.photos]


def arrange(photos, w, h, gap):
    """Best uncropped layout inside a w*h box: (score, [(x, y, w, h)]).

    photos: [(aspect, caption height for a width, or None)]; captions sit below their photo.
    Score is the covered fraction of the box, lowered when photo sizes are uneven.
    """

    def place(groups, by_rows, s):
        """Lines of photos (rows or columns) at scale s: (boxes, fits)."""
        main_room, cross_room = (w, h) if by_rows else (h, w)
        lines = []
        for g in groups:
            # line thickness at scale 1: photos fill the line's length exactly
            t = s * (main_room - gap * (len(g) - 1)) / sum(a if by_rows else 1 / a for a, _ in g)
            cells = [(a * t, t) if by_rows else (t, t / a) for a, _ in g]
            caps = [cap(bw) if cap else 0 for (_, cap), (bw, _) in zip(g, cells)]
            # (main, cross) size of each photo plus caption
            lines.append([((bw, bh + c) if by_rows else (bh + c, bw), bw, bh) for (bw, bh), c in zip(cells, caps)])
        lengths = [sum(m for (m, _), _, _ in l) + gap * (len(l) - 1) for l in lines]
        thick = [max(c for (_, c), _, _ in l) for l in lines]
        total = sum(thick) + gap * (len(lines) - 1)
        boxes, cross = [], (cross_room - total) / 2
        for l, length, t in zip(lines, lengths, thick):
            main = (main_room - length) / 2
            for (m, _), bw, bh in l:
                boxes.append((main, cross, bw, bh) if by_rows else (cross, main, bw, bh))
                main += m + gap
            cross += t + gap
        return boxes, max(lengths) <= main_room + 1e-6 and total <= cross_room + 1e-6

    def fit(groups, by_rows):
        """Largest layout that fits; captions don't scale, so search when they overflow."""
        main_room, cross_room = (w, h) if by_rows else (h, w)
        base = sum((main_room - gap * (len(g) - 1)) / sum(a if by_rows else 1 / a for a, _ in g) for g in groups)
        hi = min(1, (cross_room - gap * (len(groups) - 1)) / base)
        boxes, fits = place(groups, by_rows, hi)
        if fits:
            return boxes
        lo = 0.05  # with endless comments, rather overflow than shrink to nothing
        for _ in range(12):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if place(groups, by_rows, mid)[1] else (lo, mid)
        return place(groups, by_rows, lo)[0]

    best = (-1, [])
    n = len(photos)
    for cut in itertools.product((False, True), repeat=n - 1):
        splits = [i + 1 for i, c in enumerate(cut) if c]
        groups = [photos[a:b] for a, b in zip((0, *splits), (*splits, n))]
        for by_rows in (True, False):
            boxes = fit(groups, by_rows)
            score = sum((bw * bh) ** 0.5 for _, _, bw, bh in boxes) ** 2 / (n * w * h)
            if score > best[0]:
                best = (score, boxes)
    return best


def paginate(photos, w, first_h, h, gap, min_per_page=3, max_per_page=5):
    """Split photos into pages, maximizing layout scores; returns [(start, end, boxes)]."""
    n = len(photos)
    cost = [0.0] + [float("inf")] * n
    choice = [None] * (n + 1)
    for end in range(1, n + 1):
        for start in range(max(0, end - max_per_page), end):
            score, boxes = arrange(photos[start:end], w, first_h if start == 0 else h, gap)
            c = cost[start] + (1 - score) + (end - start < min_per_page)
            if c < cost[end]:
                cost[end], choice[end] = c, (start, boxes)
    pages, end = [], n
    while end:
        start, boxes = choice[end]
        pages.append((start, end, boxes))
        end = start
    return pages[::-1]


def download(url):
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def build_pdf(title, album_caption, sections, out, font, page="A4", landscape=False, dpi=200, compression=10,
              fallback_fonts=("/System/Library/Fonts/Apple Color Emoji.ttc",
                              "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
              margin=12, gap=4, title_size=32, heading_size=20, caption_size=7, caption_gap=1.5):
    pdf = FPDF(orientation="L" if landscape else "P", unit="mm", format=page)
    pdf.set_auto_page_break(False)
    pdf.set_margins(margin, margin)
    pdf.add_font("text", fname=font)
    for i, f in enumerate(fallback_fonts):
        pdf.add_font(f"fallback{i}", fname=f)
    pdf.set_fallback_fonts([f"fallback{i}" for i in range(len(fallback_fonts))])
    content_w, content_h = pdf.w - 2 * margin, pdf.h - 2 * margin
    lines = lambda text, width: len(pdf.multi_cell(width, text=text, dry_run=True, output="LINES"))

    pdf.add_page()
    pdf.set_font("text", size=title_size)
    title_h = lines(title, content_w) * pdf.font_size * 1.3
    pdf.set_font_size(caption_size + 2)
    notes_h = lines(album_caption, content_w) * pdf.font_size * 1.4 + 3 * gap if album_caption else 0
    pdf.set_y((pdf.h - title_h - notes_h) / 2)
    pdf.set_font_size(title_size)
    pdf.multi_cell(content_w, pdf.font_size * 1.3, title, align="C")
    if album_caption:
        pdf.set_font_size(caption_size + 2)
        pdf.set_text_color(90)
        pdf.set_y(pdf.get_y() + 3 * gap)
        pdf.multi_cell(content_w, pdf.font_size * 1.4, album_caption, align="C")

    pdf.set_font_size(caption_size)
    caption_line_h = pdf.font_size * 1.3

    @functools.cache
    def caption_height(text, width):
        return caption_gap + lines(text, width) * caption_line_h

    def caption_fn(text):
        # floor the width to 0.5 mm so the cache hits; never underestimates the line count
        return (lambda width: caption_height(text, max(1, int(width * 2)) / 2)) if text else None

    # Lay out every page first, so each photo is downloaded at the size it is printed.
    pages = []  # (heading or None, [(photo, x, y, w, h)])
    pdf.set_font_size(heading_size)
    heading_line_h = pdf.font_size * 1.3
    for section in sections:
        top = lines(section.title, content_w) * heading_line_h + gap if section.title else 0
        pdf.set_font_size(caption_size)
        layout = paginate([(p.aspect, caption_fn(p.caption)) for p in section.photos],
                          content_w, content_h - top, content_h, gap)
        pdf.set_font_size(heading_size)
        if not layout:
            pages.append((section.title, []))
        for i, (start, end, boxes) in enumerate(layout):
            dy = margin + (top if i == 0 else 0)
            placed = [(p, x + margin, y + dy, bw, bh) for p, (x, y, bw, bh) in zip(section.photos[start:end], boxes)]
            pages.append((section.title if i == 0 else None, placed))

    px = lambda mm: max(1, round(mm / 25.4 * dpi))
    # -no: no play button on video thumbnails, -l: JPEG quality
    urls = [f"{p.url}=w{px(bw)}-h{px(bh)}-no-l{min(100, max(1, 100 - compression))}" for _, placed in pages for p, _, _, bw, bh in placed]
    with ThreadPoolExecutor(8) as pool:
        images = pool.map(download, urls)
        for heading, placed in pages:
            pdf.add_page()
            if heading:
                pdf.set_font_size(heading_size)
                pdf.set_text_color(0)
                pdf.multi_cell(content_w, heading_line_h, heading, align="C")
            pdf.set_font_size(caption_size)
            pdf.set_text_color(70)
            for p, x, y, bw, bh in placed:
                pdf.image(io.BytesIO(next(images)), x, y, bw, bh)
                if p.caption:
                    pdf.set_xy(x, y + bh + caption_gap)
                    pdf.multi_cell(bw, caption_line_h, p.caption)
    pdf.output(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="public album link (photos.app.goo.gl/... or photos.google.com/share/...)")
    parser.add_argument("-o", "--output", help="PDF name or path, relative to out/ (default: album title)")
    parser.add_argument("-p", "--page", default="A4", help="page size: A3, A4, A5, Letter, Legal (default A4)")
    parser.add_argument("-l", "--landscape", action="store_true", help="landscape instead of portrait")
    parser.add_argument("-q", "--quality", type=int, default=200, help="photo resolution in DPI (default 200)")
    parser.add_argument("-c", "--compression", type=int, default=10,
                        help="JPEG compression 0-99, higher = smaller file (default 10)")
    parser.add_argument("-i", "--ignore-comments", action="store_true", help="leave out comments and reactions")
    parser.add_argument("--font", default="/System/Library/Fonts/Supplemental/Arial.ttf", help="TTF text font")
    args = parser.parse_args()

    title, album_caption, sections = fetch_album(args.url, not args.ignore_comments)
    name = args.output or (re.sub(r"[^\w\- ]+", "", title).strip() or "album") + ".pdf"
    out = Path(__file__).parent / "out" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"{title}: {sum(len(s.photos) for s in sections)} photos, {len(sections)} sections -> {out}")
    build_pdf(title, album_caption, sections, out, args.font, args.page, args.landscape, args.quality, args.compression)


if __name__ == "__main__":
    main()
