import os
import re
import time
from typing import Dict, List, Tuple, Optional

import requests
from requests.auth import HTTPBasicAuth

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "", "null", "None") else default


class HMIClient:
    def __init__(self, base_url: str, username: str, password: str):
        if not base_url.endswith("/"):
            base_url += "/"
        self.base = base_url
        self.sess = requests.Session()
        self.sess.auth = HTTPBasicAuth(username, password)

    def fetch(self, path: str) -> str:
        url = self.base + path
        r = self.sess.get(url, timeout=10)
        r.raise_for_status()
        r.encoding = 'utf-8'
        return r.text

    def fetch_bytes(self, path: str) -> bytes:
        url = self.base + path
        r = self.sess.get(url, timeout=10)
        r.raise_for_status()
        return r.content


LANG_RE = re.compile(r"var\s+languages(\d)\s*=\s*\{(.*?)\};", re.S)
KEY_RE = re.compile(r"\"([^\"]+)\"\s*:\s*\[(.*?)\]\s*,?\s*$", re.M)


def parse_languages(js_text: str) -> Dict[str, List[str]]:
    langs = {}
    for m in LANG_RE.finditer(js_text):
        block = m.group(2)
        for km in KEY_RE.finditer(block):
            key = km.group(1)
            arr_raw = km.group(2)
            items = []
            cur = ''
            in_q = False
            esc = False
            for ch in arr_raw:
                if not in_q:
                    if ch == '"':
                        in_q = True
                        cur = ''
                else:
                    if esc:
                        cur += ch
                        esc = False
                    elif ch == '\\':
                        esc = True
                    elif ch == '"':
                        items.append(cur)
                        in_q = False
                    else:
                        cur += ch
            langs[key] = items
    return langs


def build_languages(client: HMIClient) -> Dict[str, List[str]]:
    js_all = ""
    for name in ("HMILang1.js", "HMILang2.js", "HMILang3.js", "HMILang4.js"):
        try:
            js_all += client.fetch(name) + "\n\n"
        except Exception:
            continue
    return parse_languages(js_all)


def resolve_text_from_lg(languages: Dict[str, List[str]], key: Optional[str], lang_index: int = 0, fallback: str = "") -> str:
    if key:
        arr = languages.get(key)
        if arr and len(arr) > lang_index:
            return arr[lang_index].strip()
    return fallback


DIV_RE = re.compile(r"<div\s+id=['\"]d(\d+)['\"]>(.*?)</div>", re.S)
TD_LABEL_RE = re.compile(r"<td[^>]*id=['\"]l(\d+)['\"][^>]*>(.*?)</td>", re.S)
A_LINK_RE = re.compile(r"<a[^>]*id=['\"]a(\d+)['\"][^>]*href=\"([^\"]+)\"", re.S)
SPAN_VAL_RE = re.compile(r"<span[^>]*id=\"(o\d+)\"([^>]*)>(.*?)</span>", re.S)


def parse_page(client: HMIClient, languages: Dict[str, List[str]], page: str, lang_index: int = 0):
    html = client.fetch(page)
    # title
    title_text = ""
    m_title = re.search(r"<span[^>]*id=\"o002\"[^>]*([^>]*)>(.*?)</span>", html, re.S)
    if m_title:
        attrs = m_title.group(1)
        inner = m_title.group(2)
        m_lg = re.search(r"lg=\"([^\"]+)\"", attrs)
        title_text = resolve_text_from_lg(languages, m_lg.group(1) if m_lg else None, lang_index)

    # Read endpoint from GFR() if present
    m_gfr = re.search(r"function\s+GFR\(\)[^\{]*\{[^\"]*\(\"(HMI\d+Read\.cgi)\"\)\s*;\s*\}", html)
    read_ep = m_gfr.group(1) if m_gfr else page.replace('.cgi', 'Read.cgi')

    entries = []
    for m_div in DIV_RE.finditer(html):
        n = m_div.group(1)
        block = m_div.group(2)
        # label
        label_text = ""
        m_label = TD_LABEL_RE.search(block)
        if m_label:
            label_html = m_label.group(2)
            m_lgspan = re.search(r"<span[^>]*lg=\"([^\"]+)\"[^>]*>.*?</span>", label_html, re.S)
            if m_lgspan:
                label_text = resolve_text_from_lg(languages, m_lgspan.group(1), lang_index)
            if not label_text:
                # simple strip
                label_text = re.sub(r"<[^>]+>", " ", label_html).strip()
                label_text = re.sub(r"\s+", " ", label_text)

        # value span and attributes
        m_span = SPAN_VAL_RE.search(block)
        if not m_span:
            continue
        span_id = m_span.group(1)
        attrs = m_span.group(2)
        inner = m_span.group(3)

        def get_attr(name: str) -> Optional[str]:
            m = re.search(fr"{name}=\"([^\"]+)\"", attrs)
            return m.group(1) if m else None

        it = get_attr('it')  # e = enum, v = numeric value
        mi = get_attr('mi')  # write identifier
        enum_def = get_attr('e')
        unit = None
        # unit span sits commonly as <span id="uXYZ" class="u">X</span> nearby in same block
        m_unit = re.search(r"<span[^>]*class=\"u\"[^>]*>(.*?)</span>", block)
        if m_unit:
            unit = re.sub(r"<[^>]+>", " ", m_unit.group(1)).strip()

        enum_options: Optional[List[str]] = None
        if enum_def:
            # Enum def can be multiline with * separators
            enum_def = enum_def.replace("\r", " ").replace("\n", " ")
            enum_options = [p.strip() for p in enum_def.split('*') if p.strip()]

        entries.append({
            'n': int(n), 'id': span_id, 'label': label_text, 'it': it, 'mi': mi,
            'unit': unit, 'enum': enum_options,
        })

    return {
        'page': page,
        'title': title_text,
        'read': read_ep,
        'entries': entries,
    }


def read_values(client: HMIClient, read_endpoint: str) -> Dict[str, Tuple[str, str]]:
    """Return mapping id -> (type, value_str)."""
    text = client.fetch(read_endpoint)
    out: Dict[str, Tuple[str, str]] = {}
    # Format: id,type,\nvalue|
    # We'll accept compact 'id,type,value|' too
    i = 0
    while i < len(text):
        m = re.search(r"(o\d+),(\w),\s*\n?", text[i:])
        if not m:
            break
        id_ = m.group(1)
        typ = m.group(2)
        i += m.end()
        mval = re.search(r"(.*?)\|", text[i:], re.S)
        if not mval:
            break
        val = mval.group(1).strip()
        i += mval.end()
        out[id_] = (typ, val)
    return out


def write_value(client: HMIClient, mi: str, value: str) -> bool:
    """Best-effort write via HMIinput.cgi.
    mi is the raw name like 'val:0x2302 0x4E25516C 0x100'.
    """
    try:
        url = client.base + 'HMIinput.cgi'
        # Use params to get correct encoding
        r = client.sess.get(url, params={mi: value}, timeout=10)
        r.raise_for_status()
        # No explicit result; assume success if 200
        return True
    except Exception:
        return False

