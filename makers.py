"""Builds PowerPoint (.pptx) and Word (.docx) files using only Python's standard library.
No extra libraries needed: these files are just zipped XML."""

import re
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

# ---------- helpers ----------
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(text):
    """Make text safe for XML."""
    return escape(_BAD_CHARS.sub("", str(text or "")))


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

# ======================================================================
#  POWERPOINT
# ======================================================================
SLIDE_W, SLIDE_H = 12192000, 6858000      # 16:9
BG = "0F1020"
TEXT = "FFFFFF"
BODY = "E6E8FF"
MUTED = "A2A6C8"
PINK = "EC4899"
GRADIENT = (
    '<a:gradFill rotWithShape="1"><a:gsLst>'
    '<a:gs pos="0"><a:srgbClr val="6366F1"/></a:gs>'
    '<a:gs pos="50000"><a:srgbClr val="8B5CF6"/></a:gs>'
    '<a:gs pos="100000"><a:srgbClr val="EC4899"/></a:gs>'
    '</a:gsLst><a:lin ang="0" scaled="0"/></a:gradFill>'
)

_THEME = XML_HEAD + (
    f'<a:theme xmlns:a="{NS_A}" name="Assistant Theme"><a:themeElements>'
    '<a:clrScheme name="Assistant">'
    '<a:dk1><a:srgbClr val="000000"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="0F1020"/></a:dk2><a:lt2><a:srgbClr val="E6E8FF"/></a:lt2>'
    '<a:accent1><a:srgbClr val="8B5CF6"/></a:accent1><a:accent2><a:srgbClr val="EC4899"/></a:accent2>'
    '<a:accent3><a:srgbClr val="6366F1"/></a:accent3><a:accent4><a:srgbClr val="22C55E"/></a:accent4>'
    '<a:accent5><a:srgbClr val="F59E0B"/></a:accent5><a:accent6><a:srgbClr val="06B6D4"/></a:accent6>'
    '<a:hlink><a:srgbClr val="8B5CF6"/></a:hlink><a:folHlink><a:srgbClr val="EC4899"/></a:folHlink>'
    '</a:clrScheme>'
    '<a:fontScheme name="Assistant"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
    '<a:fmtScheme name="Assistant">'
    '<a:fillStyleLst>' + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3 + '</a:fillStyleLst>'
    '<a:lnStyleLst>'
    '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '</a:lnStyleLst>'
    '<a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3 + '</a:effectStyleLst>'
    '<a:bgFillStyleLst>' + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3 + '</a:bgFillStyleLst>'
    '</a:fmtScheme></a:themeElements></a:theme>'
)

_GROUP = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
)

_LVL = ('<a:lvl1pPr marL="0" algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1">'
        '<a:defRPr sz="1800" kern="1200"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'
        '<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr></a:lvl1pPr>')

_MASTER = XML_HEAD + (
    f'<p:sldMaster xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}">'
    f'<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="{BG}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
    f'<p:spTree>{_GROUP}</p:spTree></p:cSld>'
    '<p:clrMap bg1="dk2" tx1="lt1" bg2="dk1" tx2="lt2" accent1="accent1" accent2="accent2" accent3="accent3" '
    'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    f'<p:txStyles><p:titleStyle>{_LVL}</p:titleStyle><p:bodyStyle>{_LVL}</p:bodyStyle>'
    f'<p:otherStyle>{_LVL}</p:otherStyle></p:txStyles></p:sldMaster>'
)

_LAYOUT = XML_HEAD + (
    f'<p:sldLayout xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}" type="blank" preserve="1">'
    f'<p:cSld name="Blank"><p:spTree>{_GROUP}</p:spTree></p:cSld>'
    '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
)


def _rels(items):
    body = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{target}"/>' for i, t, target in items)
    return XML_HEAD + f'<Relationships xmlns="{PKG_REL}">{body}</Relationships>'


class _Ids:
    def __init__(self):
        self.n = 1

    def next(self):
        self.n += 1
        return self.n


def _run(text, size, color, bold=False):
    b = ' b="1"' if bold else ""
    return (f'<a:r><a:rPr lang="en-US" sz="{size}"{b} dirty="0"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f'</a:rPr><a:t>{clean(text)}</a:t></a:r>')


def _para(text, size, color, bold=False, align="l", bullet=False, space_before=0):
    spc = f'<a:spcBef><a:spcPts val="{space_before}"/></a:spcBef>' if space_before else ""
    if bullet:
        ppr = (f'<a:pPr marL="342900" indent="-342900" algn="{align}">{spc}'
               f'<a:buClr><a:srgbClr val="{PINK}"/></a:buClr><a:buFont typeface="Arial"/><a:buChar char="&#8226;"/></a:pPr>')
    else:
        ppr = f'<a:pPr marL="0" indent="0" algn="{align}">{spc}<a:buNone/></a:pPr>'
    return f'<a:p>{ppr}{_run(text, size, color, bold)}</a:p>'


def _textbox(ids, name, x, y, w, h, paragraphs, anchor="t"):
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{ids.next()}" name="{name}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="{anchor}"><a:noAutofit/></a:bodyPr>'
        f'<a:lstStyle/>{paragraphs}</p:txBody></p:sp>'
    )


def _shape(ids, name, x, y, w, h, fill_xml, geom="rect"):
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{ids.next()}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        f'<a:prstGeom prst="{geom}"><a:avLst/></a:prstGeom>{fill_xml}<a:ln><a:noFill/></a:ln></p:spPr></p:sp>'
    )


def _slide_xml(shapes):
    return XML_HEAD + (
        f'<p:sld xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}">'
        f'<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="{BG}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
        f'<p:spTree>{_GROUP}{shapes}</p:spTree></p:cSld>'
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
    )


def _title_slide(title, subtitle):
    ids = _Ids()
    soft_purple = '<a:solidFill><a:srgbClr val="8B5CF6"><a:alpha val="22000"/></a:srgbClr></a:solidFill>'
    soft_pink = '<a:solidFill><a:srgbClr val="EC4899"><a:alpha val="18000"/></a:srgbClr></a:solidFill>'
    shapes = (
        _shape(ids, "Circle 1", 8200000, -1400000, 5200000, 5200000, soft_purple, "ellipse")
        + _shape(ids, "Circle 2", 9600000, 3600000, 3600000, 3600000, soft_pink, "ellipse")
        + _textbox(ids, "Title", 900000, 1700000, 9400000, 2100000, _para(title, 5000, TEXT, bold=True), anchor="b")
        + _shape(ids, "Accent", 900000, 4000000, 2400000, 70000, GRADIENT)
        + _textbox(ids, "Subtitle", 900000, 4300000, 9000000, 1200000, _para(subtitle, 2400, MUTED))
    )
    return _slide_xml(shapes)


def _content_slide(title, bullets, number, deck_title):
    ids = _Ids()
    bullets = [b for b in bullets if str(b).strip()] or [""]
    total = sum(len(str(b)) for b in bullets)
    size = 2400 if total <= 330 else 2000 if total <= 520 else 1700
    paras = "".join(_para(b, size, BODY, bullet=True, space_before=1400 if i else 0) for i, b in enumerate(bullets))
    shapes = (
        _shape(ids, "Side bar", 0, 0, 180000, SLIDE_H, GRADIENT)
        + _textbox(ids, "Title", 750000, 430000, 10700000, 950000, _para(title, 3600, TEXT, bold=True), anchor="ctr")
        + _shape(ids, "Accent", 750000, 1430000, 1500000, 55000, GRADIENT)
        + _textbox(ids, "Body", 750000, 1800000, 10700000, 4150000, paras)
        + _textbox(ids, "Footer", 750000, 6270000, 8000000, 300000, _para(deck_title, 1100, MUTED), anchor="ctr")
        + _textbox(ids, "Number", 10450000, 6270000, 1000000, 300000, _para(str(number), 1100, MUTED, align="r"), anchor="ctr")
    )
    return _slide_xml(shapes)


def build_presentation(deck, path):
    """deck = {"title": str, "subtitle": str, "slides": [{"title": str, "bullets": [str, ...]}, ...]}"""
    title = deck.get("title") or "Presentation"
    slides_xml = [_title_slide(title, deck.get("subtitle") or "")]
    for number, slide in enumerate(deck.get("slides", []), start=2):
        slides_xml.append(_content_slide(slide.get("title", ""), slide.get("bullets", []), number, title))

    count = len(slides_xml)
    content_types = XML_HEAD + (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        + "".join(
            f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            for i in range(1, count + 1))
        + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    root_rels = _rels([
        ("rId1", f"{REL}/officeDocument", "ppt/presentation.xml"),
        ("rId2", f"{PKG_REL}/metadata/core-properties", "docProps/core.xml"),
        ("rId3", f"{REL}/extended-properties", "docProps/app.xml"),
    ])
    presentation = XML_HEAD + (
        f'<p:presentation xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        '<p:sldIdLst>' + "".join(f'<p:sldId id="{255 + i}" r:id="rId{2 + i}"/>' for i in range(1, count + 1)) + '</p:sldIdLst>'
        f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
    )
    presentation_rels = _rels(
        [("rId1", f"{REL}/slideMaster", "slideMasters/slideMaster1.xml"),
         ("rId2", f"{REL}/theme", "theme/theme1.xml")]
        + [(f"rId{2 + i}", f"{REL}/slide", f"slides/slide{i}.xml") for i in range(1, count + 1)]
    )
    master_rels = _rels([("rId1", f"{REL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
                         ("rId2", f"{REL}/theme", "../theme/theme1.xml")])
    layout_rels = _rels([("rId1", f"{REL}/slideMaster", "../slideMasters/slideMaster1.xml")])
    slide_rels = _rels([("rId1", f"{REL}/slideLayout", "../slideLayouts/slideLayout1.xml")])
    core = XML_HEAD + (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{clean(title)}</dc:title><dc:creator>AI Research Assistant</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{_now()}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{_now()}</dcterms:modified></cp:coreProperties>'
    )
    app = XML_HEAD + (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f'<Application>AI Research Assistant</Application><Slides>{count}</Slides></Properties>'
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", presentation_rels)
        z.writestr("ppt/slideMasters/slideMaster1.xml", _MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", _LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels)
        z.writestr("ppt/theme/theme1.xml", _THEME)
        for i, xml in enumerate(slides_xml, start=1):
            z.writestr(f"ppt/slides/slide{i}.xml", xml)
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", slide_rels)
    return count


# ======================================================================
#  WORD
# ======================================================================
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_STYLES = XML_HEAD + (
    f'<w:styles xmlns:w="{NS_W}">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="288" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:spacing w:before="0" w:after="120"/></w:pPr><w:rPr><w:b/><w:color w:val="4F46E5"/><w:sz w:val="56"/><w:szCs w:val="56"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:spacing w:after="360"/></w:pPr><w:rPr><w:color w:val="6B7280"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="360" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>'
    '<w:rPr><w:b/><w:color w:val="6D28D9"/><w:sz w:val="34"/><w:szCs w:val="34"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr>'
    '<w:rPr><w:b/><w:color w:val="DB2777"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>'
    '</w:styles>'
)


def _wp(text, style=None, bullet=False):
    ppr = ""
    if style:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
    elif bullet:
        ppr = '<w:pPr><w:spacing w:after="80"/><w:ind w:left="720" w:hanging="360"/></w:pPr>'
    if bullet:
        runs = ('<w:r><w:t xml:space="preserve">&#8226;</w:t></w:r>'
                f'<w:r><w:tab/><w:t xml:space="preserve">{clean(text)}</w:t></w:r>')
    else:
        runs = f'<w:r><w:t xml:space="preserve">{clean(text)}</w:t></w:r>'
    return f"<w:p>{ppr}{runs}</w:p>"


def build_document(doc, path):
    """doc = {"title": str, "subtitle": str, "sections": [{"heading": str, "paragraphs": [str], "bullets": [str]}]}"""
    title = doc.get("title") or "Document"
    body = _wp(title, "Title")
    if doc.get("subtitle"):
        body += _wp(doc["subtitle"], "Subtitle")
    for section in doc.get("sections", []):
        if section.get("heading"):
            body += _wp(section["heading"], "Heading1")
        for paragraph in section.get("paragraphs", []):
            if str(paragraph).strip():
                body += _wp(paragraph)
        for item in section.get("bullets", []):
            if str(item).strip():
                body += _wp(item, bullet=True)
    body += ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
             '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')

    document = XML_HEAD + f'<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}"><w:body>{body}</w:body></w:document>'
    content_types = XML_HEAD + (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '</Types>'
    )
    root_rels = _rels([
        ("rId1", f"{REL}/officeDocument", "word/document.xml"),
        ("rId2", f"{PKG_REL}/metadata/core-properties", "docProps/core.xml"),
    ])
    doc_rels = _rels([("rId1", f"{REL}/styles", "styles.xml")])
    core = XML_HEAD + (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{clean(title)}</dc:title><dc:creator>AI Research Assistant</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{_now()}</dcterms:created></cp:coreProperties>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", _STYLES)