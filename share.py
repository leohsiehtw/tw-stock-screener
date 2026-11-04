"""分享版：安全檢查，以及（選用）把今天的候選標的頁公開到 GitHub Pages。只用標準函式庫，不 import screener。

screener.py 的 scan 會產生「分享版_<日期>.html」（只含今天的候選標的與關卡說明），並先用這裡的 check_html
檢查一遍（路徑、總資金、部位試算、自選股、帳號名稱…都不可以出現）。公開則由 publish 負責，
用 GitHub 官方的 gh 指令（gh api）上傳 index.html 和 README.md 兩個檔案，不使用 git。

結束代碼（只出現在「公開」這一步，選股結果不受影響）：
  10 還沒有公開：第一次公開需要你確認
  11 找不到 gh（GitHub 官方指令列工具）
  12 gh 還沒有登入
  13 分享版沒有通過安全檢查，或沒有產生
  14 GitHub 那邊出錯（連不上、被限速、沒有權限…）
  15 設定或儲存庫不符合條件

設定檔 share_pwa=1 時，分享頁多了 PWA（可以加到手機主畫面、離線看）與每日歷史；公開改走 publish_pwa（Git Data API，一次發佈一個 commit）。
"""
import base64, bisect, functools, getpass, hashlib, html, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, time, unicodedata, zlib
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

E_CONFIRM, E_GH_MISSING, E_GH_AUTH, E_LEAK, E_GITHUB, E_SHARE = 10, 11, 12, 13, 14, 15
MAX_BYTES = 300_000
DISCLAIMER = '僅供研究，非投資建議'
CODE_RE = re.compile(r'[0-9]{4}[0-9A-Z]{0,2}')
SHARE_FILES = ('index.html', 'README.md')
FORBIDDEN_WORDS = ('部位', '總資金', '風險', '自選股', '觀察名單', '試算')
CONFIG_KEY_NAMES = ('capital', 'risk', 'mode', 'watchlist', 'output_dir', 'share', 'share_publish', 'share_repo', 'share_pwa')
REPO_DESC = '台股盤後選股分享版（僅供研究，非投資建議）'
TAKEDOWN_CMD = 'python screener.py share --config 設定.txt --takedown'

README_TEMPLATE = '''# 台股盤後選股（分享版）

最新的頁面：{url}

這個儲存庫只放兩個檔案：`index.html`（分享版頁面，每個交易日更新一次）和這份 README。
頁面只列出當天的候選標的與關卡說明，不含任何個人資料。

僅供研究，非投資建議。
'''


class ShareError(Exception):
    """給使用者看的失敗：代碼、原因、怎麼做"""

    def __init__(self, code, cause, todo):
        super().__init__(cause)
        self.code, self.cause, self.todo = code, cause, todo


def _say(*a):
    line = ' '.join(str(x) for x in a)
    print(line, file=sys.stderr, flush=True)
    p = os.environ.get('TWSTOCK_LOG')
    if p:
        try:
            with open(p, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


# 測試用的接縫：測試會換掉這幾個，才不會真的執行 gh、等待或等使用者輸入
_run = subprocess.run
_which = shutil.which
_sleep = time.sleep


def _ask(prompt):
    return input(prompt)


def _isatty():
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# ---------- 安全檢查 ----------
def _nf(s):
    """Unicode 正規化（NFKC）：全形英數與符號、相容字元都換成一般寫法（１→1、＠→@、：→:）"""
    return unicodedata.normalize('NFKC', s)


def _fold(s):
    """NFKC＋大小寫折疊，給不分大小寫的規則用"""
    return _nf(_nf(s).casefold())


def _strip_cf(s):
    """拿掉看不見的格式字元（類別 Cf：零寬字元 U+200B～U+200D、U+2060、U+FEFF、軟連字號 U+00AD、方向控制…）"""
    bad = {ord(c): None for c in set(s) if unicodedata.category(c) == 'Cf'}
    return s.translate(bad) if bad else s


ODD_CATS = {'Cf', 'Co', 'Cs', 'Cn', 'Zl', 'Zp'}


def _odd_chars(s):
    """看不見或不該出現的字元：格式字元、私用區、未定義、行／段分隔符號，以及 \\t \\n \\r 以外的控制字元"""
    return any(unicodedata.category(c) in ODD_CATS or (unicodedata.category(c) == 'Cc' and c not in '\t\n\r')
               for c in set(s))


def _uniq(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


# 中文數字（NFKC 之後的寫法；含大寫與簡體的萬、億）
CN_DIGITS = dict(zip('零一二三四五六七八九', range(10)))
CN_DIGITS.update({'〇': 0, '兩': 2, '两': 2, '壹': 1, '貳': 2, '贰': 2, '參': 3, '叄': 3, '叁': 3, '肆': 4, '伍': 5, '陸': 6, '陆': 6,
                  '柒': 7, '捌': 8, '玖': 9})
CN_SMALL = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}
CN_BIG = {'萬': 10 ** 4, '万': 10 ** 4, '億': 10 ** 8, '亿': 10 ** 8}
CN_CHARS = ''.join(CN_DIGITS) + ''.join(CN_SMALL) + ''.join(CN_BIG)
_KK = '[%s](?:[^\\S\\r\\n]{0,2}[%s])*' % (CN_CHARS, CN_CHARS)
CN_RUN_RE = re.compile('(?:(?<![0-9.,])[0-9]+)?%s(?:[0-9]+%s)*(?:[0-9]+(?![0-9]|[.,][0-9]))?' % (_KK, _KK))
CN_TOKEN_RE = re.compile(r'[0-9]+|\S')
DIGIT_RUN_RE = re.compile(r'[0-9]+(?:(?:[,.]\s?|[\s_]{1,3})[0-9]+)*')
BLANK_RE = re.compile(r'[\s_]+')
SCI_RE = re.compile(r'(?<![0-9A-Za-z#._-])([0-9]+(?:\.[0-9]+)?)[eE]\+?([0-9]{1,2})(?![0-9A-Za-z])')
UNIT_RE = re.compile(r'(?<![0-9.,])([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s{0,3}(?:([百佰千仟])\s{0,3})?([萬万億亿])')
CTX_MASK_RE = re.compile(r'[\d%s]' % CN_CHARS)
_CN_LIMIT = 10 ** 16


def _cn_values(run):
    """把一段中文數字（可夾阿拉伯數字，例如 100萬、3萬4千）換成整數。每讀一個字就產生一次目前的值
    （一百二十三萬四千五百 → …、1230000、…、1234500），所以後面多接的字不會讓前面的金額漏掉。"""
    total = section = 0
    num, prev = None, None
    for tok in CN_TOKEN_RE.findall(run):
        if tok[0].isdigit():
            if len(tok) > 16:
                return
            num, prev = int(tok), 'a'
        elif tok in CN_DIGITS:
            d = CN_DIGITS[tok]
            if prev == 'd':
                num = num * 10 + d                  # 一〇〇〇〇〇〇 這種逐位寫法
            elif prev == 'a':
                total = section = 0                 # 阿拉伯數字後面直接接中文數字：當作另一個數
                num = d
            else:
                num = d
            prev = 'd'
        elif tok in CN_SMALL:
            u = CN_SMALL[tok]
            section += (num if num is not None else (1 if u == 10 else 0)) * u
            num, prev = None, 'u'
        elif tok in CN_BIG:
            if CN_BIG[tok] == 10 ** 8:
                total = (total + section + (num or 0)) * 10 ** 8
            else:
                total += (section + (num or 0)) * 10 ** 4
            section, num, prev = 0, None, 'u'
        else:
            continue
        cur = total + section + (num or 0)
        if cur > _CN_LIMIT:
            return
        yield cur


def _digit_hits(t, targets):
    """找出寫成 1 000 000、1_000_000、1,000,000、1.000.000、1000000.00 的數字（目標值的數字串完全一樣、前後不接別的數字）。
    逗號、點只能在千分位上（點要至少兩個、每段 3 位），空白與底線可以在任何位置。回傳 [(值, 位置)]"""
    if not targets:
        return []
    strs = {str(v): v for v in targets}
    prefixes = {s[:i] for s in strs for i in range(1, len(s) + 1)}
    out = []
    for m in DIGIT_RUN_RE.finditer(t):
        parts = re.split(r'([^0-9]+)', m.group())
        g, s = parts[0::2], parts[1::2]
        n = len(g)
        pos = [m.start()]
        for i in range(n - 1):
            pos.append(pos[-1] + len(g[i]) + len(s[i]))
        for k in range(n):
            if k and not BLANK_RE.fullmatch(s[k - 1]):
                continue                            # 前面是逗號或點：是更大的數字的一部分
            cur, dots, other, len3 = '', 0, 0, True
            for j in range(k, n):
                if j > k:
                    sep = s[j - 1]
                    if ',' in sep:
                        if len(g[j]) != 3:
                            break
                        other += 1
                    elif '.' in sep:
                        if sep != '.':
                            break
                        dots += 1
                    else:
                        other += 1
                    len3 = len3 and len(g[j]) == 3
                cur += g[j]
                if cur not in prefixes:
                    break
                if cur not in strs:
                    continue
                if dots and not (dots >= 2 and not other and len3 and len(g[k]) <= 3):
                    continue
                if j < n - 1:
                    nxt = s[j]
                    if ',' in nxt or '.' in nxt:
                        tail_ok = (nxt == '.' and not dots and not g[j + 1].strip('0')
                                   and (j + 1 == n - 1 or BLANK_RE.fullmatch(s[j + 1])))
                        if not tail_ok:
                            continue
                out.append((strs[cur], pos[k]))
    return out


def _value_hits(t, targets):
    """所有寫法的金額：分隔符號、科學記號（1e6）、阿拉伯數字＋萬／億（1,000萬、0.01億）、中文數字（一百萬、壹佰萬）"""
    if not targets:
        return []
    out = _digit_hits(t, targets)
    for m in SCI_RE.finditer(t):
        try:
            v = Decimal(m.group(1)).scaleb(int(m.group(2)))
        except (InvalidOperation, ValueError):
            continue
        if v == v.to_integral_value() and int(v) in targets:
            out.append((int(v), m.start()))
    for m in UNIT_RE.finditer(t):
        try:
            v = Decimal(m.group(1).replace(',', ''))
        except InvalidOperation:
            continue
        v *= (CN_SMALL[m.group(2)] if m.group(2) else 1) * CN_BIG[m.group(3)]
        if v == v.to_integral_value() and int(v) in targets:
            out.append((int(v), m.start()))
    for m in CN_RUN_RE.finditer(t):
        for v in _cn_values(m.group()):
            if v in targets:
                out.append((v, m.start()))
                break
    return sorted(out, key=lambda x: x[1])


def _person_names(extra=()):
    names = list(extra)
    for k in ('USERNAME', 'USER', 'LOGNAME', 'COMPUTERNAME', 'HOSTNAME'):
        names.append(os.environ.get(k, ''))
    for fn in (getpass.getuser, socket.gethostname):
        try:
            names.append(fn())
        except Exception:
            pass
    out = []
    for n in names:
        n = _fold((n or '').strip())
        if len(n) >= 2 and n not in out:
            out.append(n)
    return out


CAPITAL_LABEL, AMOUNT_LABEL = '總資金的數字', '部位試算的股數或金額'


def make_rules(capital=None, amounts=(), paths=(), watchlist=None, repo=None, people=(), sectors=()):
    """產生檢查規則：{'patterns': [(說明, 正規式)], 'values': {說明: {金額}}, 'watchlist': {代號: 名稱}}。
    比對的文字會先做 NFKC 正規化，所以這裡的值也都先正規化；不分大小寫的規則再做大小寫折疊。"""
    pats = []
    values = {CAPITAL_LABEL: set(), AMOUNT_LABEL: set()}

    def add(label, pattern, flags=0):
        pats.append((label, re.compile(pattern, flags)))

    if capital and capital >= 10000:
        c = int(capital)
        values[CAPITAL_LABEL].add(c)          # 1000000、1,000,000、1 000 000、1e6、一百萬…都由 _value_hits 找
        wan = str(c // 10000) if c % 10000 == 0 else format(c / 10000, '.10g')
        add('總資金的數字（萬）', r'(?<![0-9,.])' + re.escape(wan) + r'\s*萬')
        add('總資金的數字（億）', r'(?<![0-9,.])' + re.escape(format(c / 1e8, '.10g')) + r'\s*億')
    seen = set()
    for n in amounts or ():
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n >= 10000 and n not in seen:
            seen.add(n)
            values[AMOUNT_LABEL].add(n)
    add('有千分位逗號的數字（可能是金額）', r'(?<![0-9,])[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])')
    forms = set()
    for p in paths or ():
        p = _fold(str(p or '').strip())
        if not p:
            continue
        for q in {p, p.rstrip('\\/')}:
            for f in (q, q.replace('/', '\\'), q.replace('\\', '/'), q.replace('\\', '\\\\')):
                forms.add(f)
                if re.match(r'[a-z]:', f):
                    forms.add(f[2:])
    for f in sorted(forms, key=len, reverse=True):
        if len(f) >= 4:
            add('本機的檔案路徑', re.escape(f), re.I)
    add('磁碟機路徑', r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]')
    add('網路磁碟路徑', r'\\\\[A-Za-z0-9_.$-]+\\[A-Za-z0-9_.$-]')
    add('使用者資料夾路徑', r'[\\/](?:users|home)[\\/][^\\/\s<>"\']+', re.I)
    add('電子郵件地址', r'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
    for k in CONFIG_KEY_NAMES:
        add('設定檔的項目（%s=）' % k, r'(?<![A-Za-z0-9_-])' + k + r'\s*[=＝]', re.I)
    for w in FORBIDDEN_WORDS:
        add('不該出現的詞（%s）' % w, re.escape(_nf(w)))
    for n in _person_names(people):
        add('電腦或使用者名稱', r'(?<![A-Za-z0-9_-])' + re.escape(n) + r'(?![A-Za-z0-9_-])', re.I)
    if repo:
        repo = _fold(repo)
        add('GitHub 儲存庫名稱', re.escape(repo), re.I)
        owner = repo.split('/')[0]
        if len(owner) >= 4:
            add('GitHub 帳號名稱', r'(?<![A-Za-z0-9-])' + re.escape(owner) + r'(?![A-Za-z0-9-])', re.I)
    add('網址', r'(?:https?|ftp|wss?|file)://', re.I)
    add('網址（省略通訊協定）', r'//[A-Za-z0-9-]+\.[A-Za-z]{2,}', re.I)
    add('javascript: 連結', r'javascript\s*:', re.I)
    add('data: 連結', r'(?<![A-Za-z0-9_-])data\s*:', re.I)
    add('@import', r'@import', re.I)
    add('url( 外部資源', r'url\s*\(', re.I)
    wl = {_nf(str(k)).strip(): _nf(str(v or '')).strip() for k, v in (watchlist or {}).items() if _nf(str(k)).strip()}
    return dict(patterns=pats, values=values, watchlist=wl, sectors=frozenset(_nf(str(x)) for x in sectors if x))


ALLOWED_TAGS = {'html', 'head', 'meta', 'title', 'style', 'body', 'div', 'span', 'main', 'header', 'section', 'footer',
                'nav', 'h1', 'h2', 'h3', 'p', 'br', 'ul', 'li', 'a', 'dl', 'dt', 'dd', 'small', 'table', 'caption',
                'thead', 'tbody', 'tr', 'th', 'td', 'fieldset', 'legend', 'label', 'input', 'select', 'option',
                'button', 'script'}
VOID_TAGS = {'meta', 'br', 'input'}
ALLOWED_IDS = {'top', 'cands', 'cands-h', 'help', 'help-h', 'ct', 'cq', 'csort', 'cstatus', 'cnone', 'csec', 'csub'}
SECTOR_RE = re.compile(r'(?:ETF|[\u4e00-\u9fff]{1,8})(?:-[\u4e00-\u9fff]{1,8})?')     # 類股標籤：「航運」「科技-半導體」「ETF-債券」
REQUIRED_IDS = ('cands', 'help')
DATA_ATTRS = {'data-i', 'data-type', 'data-kind', 'data-code', 'data-name', 'data-close', 'data-chg', 'data-vr',
              'data-stop', 'data-dist', 'data-label', 'data-key', 'data-num', 'data-sec', 'data-set'}
PLAIN_ATTRS = {'lang', 'aria-hidden', 'aria-label', 'aria-labelledby', 'aria-live', 'role', 'scope', 'colspan',
               'for', 'autocomplete', 'placeholder', 'value', 'checked', 'hidden', 'selected', 'aria-pressed'}
STYLE_RE = re.compile(r'(?:(?:left|width):-?[0-9]+(?:\.[0-9]+)?%;?|gap:8px;?)+')
CLASS_RE = re.compile(r'[A-Za-z0-9 _-]*')
META_OK = {('viewport', 'width=device-width, initial-scale=1'), ('color-scheme', 'light dark'),
           ('robots', 'noindex, nofollow')}
NOSTYLE_LABELS = {'有千分位逗號的數字（可能是金額）'}      # 這條規則不檢查 <style> 裡的 CSS（rgba(255,255,255,…) 之類）
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


CHART_RE = re.compile(r'[A-Za-z]{8,8000}')           # 線圖資料：只有英文字母（沒有數字，不會和金額混在一起）


# ---------- PWA 與每日歷史（設定檔 share_pwa=1 才會用到；預設關閉，行為和以前完全一樣） ----------
HISTORY_DAYS = 120                     # 本機和遠端都只留最新這麼多個交易日
PWA_SCHEMA = 1
PWA_STATIC = ('manifest.webmanifest', 'sw.js', 'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png',
              'icons/maskable-512.png', 'icons/apple-touch-icon.png')
HIST_INDEX = 'history/index.json'
HIST_RE = r'history/([0-9]{8})\.json'
LIMITS = dict(day=40_000, index=16_000, manifest=4_000, sw=16_000, png=64_000, svg=4_000)
THEME_LIGHT, THEME_DARK = '#ffffff', '#000000'          # 和 screener.PALETTE 的 page 色一樣（測試會比對）
PWA_NAME, PWA_SHORT = '台股盤後選股', '台股選股'
PWA_IDS = {'hist', 'hist-h', 'hbar', 'hview', 'htrend', 'bnav', 'a2hs'}
PWA_VIEWPORT = 'width=device-width, initial-scale=1, viewport-fit=cover'
# 分享頁 <head> 裡的 PWA 標籤：screener 產生頁面和這裡的檢查共用這一份（<link> 是 void 標籤，不寫 />）
PWA_HEAD_TAGS = '''<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icons/icon.svg" type="image/svg+xml">
<link rel="icon" href="icons/icon-192.png" type="image/png" sizes="192x192">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-title" content="台股選股">'''


class _TagScan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.metas = [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'link':
            self.links.append(frozenset(attrs))
        elif tag == 'meta':
            self.metas.append(dict(attrs))


def _scan_head_tags():
    p = _TagScan()
    p.feed(PWA_HEAD_TAGS)
    p.close()
    return p


_HEAD = _scan_head_tags()
LINK_OK = frozenset(_HEAD.links)
THEME_OK = frozenset((m['name'], m['content'], m['media']) for m in _HEAD.metas if 'media' in m)
META_OK_PWA = (META_OK - {('viewport', 'width=device-width, initial-scale=1')}) | {('viewport', PWA_VIEWPORT)} | \
    {(m['name'], m['content']) for m in _HEAD.metas if 'media' not in m}


class _Checker(HTMLParser):
    def __init__(self, pwa=False):
        super().__init__(convert_charrefs=True)
        self.pwa, self.links, self.pmeta = pwa, set(), set()
        self.problems, self.counts, self.ids = [], {}, set()
        self.cands, self.meta = [], set()
        self.secs, self.in_head, self.head_scripts = set(), False, 0

    def bad(self, msg):
        if msg not in self.problems:
            self.problems.append(msg)

    def handle_decl(self, decl):
        if decl.strip().lower() != 'doctype html':
            self.bad('頁面裡有不認得的宣告')

    def unknown_decl(self, data):
        self.bad('頁面裡有不認得的宣告')

    def handle_comment(self, data):
        self.bad('頁面裡有註解（<!-- -->）')

    def handle_pi(self, data):
        self.bad('頁面裡有處理指令（<? ?>）')

    def handle_starttag(self, tag, attrs):
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag == 'link' and self.pwa:
            self.check_link(attrs)
            return
        if tag not in ALLOWED_TAGS:
            self.bad('頁面裡有不允許的標籤 <%s>' % tag[:20])
            return
        if tag == 'head':
            self.in_head = True
        elif tag == 'script' and self.in_head:
            self.head_scripts += 1
        amap, full = {}, dict(attrs)
        for k, v in attrs:
            amap[k] = v
            self.attr(tag, k, v, full)
        if tag in ('script', 'style', 'title') and amap:
            self.bad('<%s> 不可以有屬性' % tag)
        if tag == 'meta':
            self.check_meta(amap)
        if tag == 'input' and amap.get('type') not in ('search', 'radio'):
            self.bad('<input> 的 type 只能是 search 或 radio')
        if tag == 'button' and amap.get('type') != 'button':
            self.bad('<button> 的 type 只能是 button')
        if tag == 'tbody' and 'cand' in (amap.get('class') or '').split():
            self.cands.append((amap.get('data-code') or '', amap.get('data-name') or ''))
        elif tag == 'tbody' and 'data-ch' in amap:
            self.bad('data-ch 的內容不合規則')

    def handle_endtag(self, tag):
        if tag == 'head':
            self.in_head = False

    def check_link(self, attrs):
        a = frozenset(attrs)
        if len(a) != len(attrs) or a not in LINK_OK:
            self.bad('頁面裡有不允許的 <link>')
        elif not self.in_head:
            self.bad('<link> 只能放在 <head> 裡')
        elif a in self.links:
            self.bad('頁面裡有重複的 <link>')
        else:
            self.links.add(a)

    def check_meta(self, amap):
        keys = set(amap)
        ok = META_OK_PWA if self.pwa else META_OK
        if keys == {'charset'}:
            if (amap['charset'] or '').lower() != 'utf-8':
                self.bad('<meta charset> 不是 utf-8')
        elif keys == {'name', 'content'} and (amap['name'], amap['content']) in ok:
            self.meta.add(amap['name'])
            self.pwa_meta((amap['name'], amap['content']))
        elif self.pwa and keys == {'name', 'content', 'media'} and (amap['name'], amap['content'], amap['media']) in THEME_OK:
            self.pwa_meta((amap['name'], amap['content'], amap['media']))
        else:
            self.bad('頁面裡有不允許的 <meta>')

    def pwa_meta(self, key):
        if self.pwa:
            if key in self.pmeta:
                self.bad('頁面裡有重複的 <meta>')
            self.pmeta.add(key)

    def attr(self, tag, k, v, amap=None):
        k = k.lower()
        if k.startswith('on'):
            self.bad('頁面裡有事件屬性（%s）' % k[:20])
        elif k == 'id':
            if v not in ALLOWED_IDS and not (self.pwa and v in PWA_IDS):
                self.bad('頁面裡有不允許的 id')
            elif v in self.ids:
                self.bad('頁面裡有重複的 id')
            self.ids.add(v)
        elif k == 'class':
            if not CLASS_RE.fullmatch(v or ''):
                self.bad('class 的內容不合規則')
        elif k == 'style':
            if not STYLE_RE.fullmatch(v or ''):
                self.bad('style 的內容不在允許的範圍')
        elif k == 'href':
            if tag != 'a' or not re.fullmatch(r'#[A-Za-z0-9_-]+', v or ''):
                self.bad('連結只能是本頁的 #錨點')
        elif k == 'type':
            if v not in ('search', 'radio', 'button'):
                self.bad('type 屬性的值不合規則')
        elif k == 'name':
            if tag == 'meta' or (tag == 'input' and v in ('ft', 'fk')):
                pass
            else:
                self.bad('name 屬性不合規則')
        elif k in ('content', 'charset'):
            if tag != 'meta':
                self.bad('%s 屬性只能用在 <meta>' % k)
        elif k == 'data-code':
            if not CODE_RE.fullmatch(v or ''):
                self.bad('data-code 不是股票代號的格式')
        elif k == 'data-sec':
            if v and not SECTOR_RE.fullmatch(v):
                self.bad('data-sec 不是類股標籤的格式')
            self.secs.add(v or '')
        elif k == 'data-sub':                        # ETF 子類按鈕：all 或類股標籤（同樣要在允許的類股清單裡）
            if tag != 'button' or (v != 'all' and not SECTOR_RE.fullmatch(v or '')):
                self.bad('data-sub 的值不合規則')
            elif v != 'all':
                self.secs.add(v)
        elif k == 'data-mkt':
            if v not in ('', '上市', '上櫃'):
                self.bad('data-mkt 的值不合規則')
        elif k == 'data-set':
            if v not in ('auto', 'light', 'dark'):
                self.bad('data-set 的值不合規則')
        elif k == 'aria-pressed':
            if v not in ('true', 'false'):
                self.bad('aria-pressed 的值不合規則')
        elif k == 'data-ch':
            if tag != 'tbody' or not CHART_RE.fullmatch(v or ''):
                self.bad('data-ch 的內容不合規則')
        elif k in DATA_ATTRS:
            if v is None or len(v) > 100 or CTRL_RE.search(v):
                self.bad('%s 的內容不合規則' % k)
        elif k in PLAIN_ATTRS:
            if v is not None and (len(v) > 100 or CTRL_RE.search(v)):
                self.bad('%s 的內容不合規則' % k)
        elif k == 'media' and self.pwa and tag == 'meta':
            pass                                     # 值由 check_meta 對照
        elif k == 'data-day' and self.pwa:
            if tag != 'div' or (amap or {}).get('id') != 'hbar' or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', v or ''):
                self.bad('data-day 的內容不合規則')
        else:
            self.bad('頁面裡有不允許的屬性（%s）' % re.sub(r'[^A-Za-z0-9_-]', '?', k)[:20])


def _outside_labels(text, s, words):
    """s 在 text 裡有沒有任何一次出現在「類股字詞」的範圍以外。只看包含 s 的字詞；沒有字詞包含 s 就等於 s in text"""
    ws = [w for w in words if s in w]
    if not ws:
        return s in text
    spans = []
    for w in ws:
        i = text.find(w)
        while i >= 0:
            spans.append((i, i + len(w)))
            i = text.find(w, i + 1)
    spans.sort()
    starts, best, top = [], [], -1
    for a, b in spans:
        starts.append(a)
        top = max(top, b)
        best.append(top)
    i = text.find(s)
    while i >= 0:
        k = bisect.bisect_right(starts, i)
        if k == 0 or best[k - 1] < i + len(s):
            return True
        i = text.find(s, i + 1)
    return False


def _mask_watch_text(text, cand_codes, cand_names):
    text = re.sub(r'<style>.*?</style>', ' ', text, flags=re.S)
    text = re.sub(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', ' ', text)
    text = re.sub(r'(?<![0-9])[0-9]+(?=\s*檔)', ' ', text)      # 開頭有 (?<![0-9]) 擋住，不會從數字串中間重來，所以是線性的；不用佔有量詞（3.9／3.10 不支援）
    for s in sorted(set(cand_names) | set(cand_codes), key=len, reverse=True):
        if s:
            text = text.replace(s, ' ')
    return text


def _text_problems(raw, rules, codes, names, labels=None):
    """文字規則（各種寫法的比對、金額、自選股）。codes／names 是頁面上合法的候選標的；labels 有給的話，自選股只比對這些字串。
    回傳問題清單，問題文字不會寫出被找到的內容。"""
    problems = []
    # 比對用的幾種寫法：原文、解開 &#…; 之後、NFKC 正規化（全形→半形）、再拿掉零寬字元、再做大小寫折疊
    un = html.unescape(raw)
    sq = _strip_cf(_nf(un))
    texts = _uniq([raw, un, _nf(un), sq, _nf(sq.casefold())])
    # 千分位規則：中文的全形逗號「，」正規化後會變成「,」，這條規則不把它當成千分位
    thou = _uniq([raw, un, _strip_cf(_nf(un.replace('，', '、')))])
    done = set()

    def hit(label, t, start):
        done.add(label)
        ctx = CTX_MASK_RE.sub('#', t[max(0, start - 15):start].replace('\n', ' ').replace('\r', ' '))
        problems.append('%s（在「…%s」的後面）' % (label, ctx) if ctx.strip() else label)

    for label, rx in rules['patterns']:
        if label in done:
            continue
        for t in ([re.sub(r'<style>.*?</style>', ' ', x, flags=re.S) for x in thou] if label in NOSTYLE_LABELS else texts):
            m = rx.search(t)
            if m:
                hit(label, t, m.start())
                break
    for label, vals in (rules.get('values') or {}).items():
        if label not in done and vals:
            found = _value_hits(sq, vals)
            if found:
                hit(label, sq, found[0][1])
    wl = rules.get('watchlist') or {}
    if wl:
        if labels is None:
            wun, wsq = un, sq
        else:
            wun = html.unescape('\n'.join(labels))
            wsq = _strip_cf(_nf(wun))
        found = False
        sec = rules.get('sectors') or ()
        words = set(sec) | {x.split('-')[0] for x in sec if '-' in x}
        for text in _uniq([_mask_watch_text(wun, codes, names),
                           _mask_watch_text(wsq, [_nf(c) for c in codes], [_strip_cf(_nf(n)) for n in names])]):
            for code, name in wl.items():
                if re.search(r'(?<![0-9A-Za-z.])' + re.escape(code) + r'(?![0-9A-Za-z]|\.[0-9])', text):
                    found = True
                elif len(name) >= 2 and name not in names and _nf(name) not in [_nf(n) for n in names] and _outside_labels(text, name, words):
                    found = True
        if found:
            problems.append('頁面裡出現了自選股清單裡、但不是今天候選標的的股票')
    return problems


def inspect_html(data, rules, pwa=False):
    """檢查要公開的位元組。回傳 (問題清單, 資訊)；問題清單空 = 通過。問題文字不會寫出被找到的內容本身。
    pwa=True 是 share_pwa 的頁面（多了 manifest 連結、theme-color、歷史日期列、底部列）；pwa=False 的規則完全不變。"""
    problems, info = [], dict(cands=0)
    if len(data) > MAX_BYTES:
        problems.append('檔案太大（%d KB，上限 %d KB）' % (len(data) // 1000, MAX_BYTES // 1000))
    if data.startswith(b'\xef\xbb\xbf'):
        problems.append('檔案開頭有 BOM')
    try:
        raw = data.decode('utf-8')
    except UnicodeDecodeError:
        return problems + ['檔案不是 UTF-8 編碼'], info
    ck = _Checker(pwa)
    try:
        ck.feed(raw)
        ck.close()
    except Exception:
        ck.bad('頁面結構無法解析')
    problems += ck.problems
    for tag, want in (('script', 2), ('style', 1), ('title', 1)):
        if ck.counts.get(tag, 0) != want:
            problems.append('<%s> 要剛好 %d 個，現在是 %d 個' % (tag, want, ck.counts.get(tag, 0)))
    if ck.head_scripts != 1:
        problems.append('<head> 裡的 <script> 要剛好 1 個，現在是 %d 個' % ck.head_scripts)
    allowed = rules.get('sectors') or ()
    if allowed and any(x and x not in allowed for x in ck.secs):
        problems.append('頁面裡有不在對照表裡的類股')
    for i in REQUIRED_IDS + (('hist', 'hbar', 'hview', 'htrend', 'bnav') if pwa else ()):
        if i not in ck.ids:
            problems.append('缺少必要的區塊（%s）' % i)
    if pwa:
        if ck.links != set(LINK_OK):
            problems.append('缺少 PWA 需要的 <link>')
        if not {('viewport', PWA_VIEWPORT)} <= ck.pmeta or len([k for k in ck.pmeta if len(k) == 3]) != len(THEME_OK):
            problems.append('缺少 PWA 需要的 <meta>（viewport 或 theme-color）')
    if 'robots' not in ck.meta:
        problems.append('缺少 noindex 的 robots 設定')
    if DISCLAIMER not in raw:
        problems.append('缺少免責聲明「%s」' % DISCLAIMER)
    info['cands'] = len(ck.cands)
    if _odd_chars(raw):
        problems.append('頁面裡有看不見的控制字元（零寬字元、方向控制、私用區或未定義的字元）')
    problems += _text_problems(raw, rules, [c for c, _ in ck.cands], [n for _, n in ck.cands])
    return problems[:30], info


def check_html(data, rules, pwa=False):
    return inspect_html(data, rules, pwa)[0]


# ---------- 每日歷史與 PWA 檔案的檢查 ----------
DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
ROW_NUM = (('close', r'[0-9]{1,6}(?:\.[0-9]{1,4})?'), ('chg', r'-?[0-9]{1,3}(?:\.[0-9]{1,4})?'),
           ('vr', r'[0-9]{1,5}(?:\.[0-9]{1,4})?'), ('stop', r'[0-9]{1,6}(?:\.[0-9]{1,4})?'))
HIST_KEYS = ['v', 'date', 'scanned', 'valid', 'n', 'nb', 'np', 'rows']
K_RE = re.compile(r'[1-9][0-9]{0,3}')


def _is_int(x):
    return type(x) is int and 0 <= x <= 10 ** 7


def _valid_date(s):
    if not isinstance(s, str) or not DATE_RE.fullmatch(s):
        return False
    try:
        import datetime
        datetime.date.fromisoformat(s)
    except ValueError:
        return False
    return True


def _no_dups(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('重複的欄位')
    return dict(pairs)


def _no_const(name):
    raise ValueError('不允許的數值')


def _load_json(data, limit, problems):
    """位元組 → (原文, 物件)；失敗回傳 (None, None) 並把原因加進 problems"""
    if len(data) > limit:
        problems.append('檔案太大（%d 位元組，上限 %d）' % (len(data), limit))
    if data.startswith(b'\xef\xbb\xbf'):
        problems.append('檔案開頭有 BOM')
    try:
        raw = data.decode('utf-8')
        obj = json.loads(raw, object_pairs_hook=_no_dups, parse_constant=_no_const)
    except (UnicodeDecodeError, ValueError, RecursionError):
        problems.append('不是合規的 UTF-8 JSON')
        return None, None
    if json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8') != data:
        problems.append('JSON 不是標準的緊密寫法')
    return raw, obj


def check_history(data, rules, tag):
    """檢查一個日檔（history/YYYYMMDD.json）。tag 是檔名的日期。回傳問題清單，問題文字不會寫出被找到的內容。"""
    p = []
    raw, obj = _load_json(data, LIMITS['day'], p)
    if obj is None:
        return p
    if not isinstance(obj, dict) or list(obj) != HIST_KEYS:
        return p + ['欄位不對']
    date = obj['date']
    if obj['v'] != 1 or type(obj['v']) is not int:
        p.append('版本不對')
    if not _valid_date(date) or date.replace('-', '') != tag:
        p.append('日期不對')
    for k in ('scanned', 'valid', 'n', 'nb', 'np'):
        if not _is_int(obj[k]):
            p.append('%s 不是整數' % k)
    rows = obj['rows']
    if not isinstance(rows, list) or len(rows) > 5000:
        return p + ['rows 不是清單']
    sectors = rules.get('sectors') or ()
    codes, names, labels, seen, nb = [], [], [], set(), 0
    for r in rows:
        if not isinstance(r, list) or len(r) != 10 or any(not isinstance(x, str) for x in r):
            p.append('有一列的格式不對（要剛好 10 個字串）')
            break
        code, name, typ, close, chg, vr, stop, sec, mkt, k = r
        bad = []
        if not CODE_RE.fullmatch(code):
            bad.append('代號')
        elif code in seen:
            bad.append('代號重複')
        seen.add(code)
        if not 1 <= len(name) <= 60 or _odd_chars(name):
            bad.append('名稱')
        if typ not in ('突破型', '回測型'):
            bad.append('型態')
        for (label, rx), v in zip(ROW_NUM, (close, chg, vr, stop)):
            if not re.fullmatch(rx, v):
                bad.append(label)
        for label, v in (('close', close), ('stop', stop)):
            if re.fullmatch(ROW_NUM[0][1], v) and Decimal(v) <= 0:
                bad.append(label)
        if sec and not (SECTOR_RE.fullmatch(sec) and (not sectors or sec in sectors)):
            bad.append('類股')
        if mkt not in ('', '上市', '上櫃'):
            bad.append('市場')
        if not K_RE.fullmatch(k):
            bad.append('連續天數')
        if any(c != code and re.search(r'(?<![0-9A-Za-z.])' + re.escape(c) + r'(?![0-9A-Za-z]|\.[0-9])', _nf(name))
               for c in (rules.get('watchlist') or {})):
            bad.append('名稱')
        if bad:
            p.append('有一列的內容不合規則（%s）' % '、'.join(_uniq(bad)))
            break
        nb += typ == '突破型'
        codes.append(code)
        names.append(name)
        labels += [name, sec, mkt, typ]
    else:
        n = len(rows)
        if all(_is_int(obj[k]) for k in ('scanned', 'valid', 'n', 'nb', 'np')):
            if obj['n'] != n or obj['nb'] != nb or obj['np'] != n - nb or not n <= obj['valid'] <= obj['scanned']:
                p.append('檔數對不上')
    if raw is not None:
        p += _text_problems(raw, rules, codes, names, labels)
    return p[:30]


def check_hist_index(data):
    p = []
    raw, obj = _load_json(data, LIMITS['index'], p)
    if obj is None:
        return p
    if not isinstance(obj, dict) or list(obj) != ['v', 'days'] or obj['v'] != 1 or not isinstance(obj['days'], list):
        return p + ['欄位不對']
    if len(obj['days']) > HISTORY_DAYS:
        p.append('超過 %d 筆' % HISTORY_DAYS)
    last = None
    for e in obj['days']:
        if not isinstance(e, dict) or list(e) != ['d', 'n', 'b'] or not _valid_date(e['d']) or not _is_int(e['n']) \
                or not _is_int(e['b']) or e['b'] > e['n']:
            return p + ['有一筆的格式不對']
        if last is not None and e['d'] >= last:
            return p + ['日期沒有由新到舊排列']
        last = e['d']
    return p


MANIFEST_KEYS = ['name', 'short_name', 'lang', 'start_url', 'scope', 'id', 'display', 'background_color', 'theme_color', 'icons']
ICON_SRCS = ('icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/maskable-512.png')


def check_manifest(data, rules=None):
    p = []
    raw, obj = _load_json(data, LIMITS['manifest'], p)
    if obj is None:
        return p
    if not isinstance(obj, dict) or list(obj) != MANIFEST_KEYS:
        return p + ['欄位不對']
    for k in ('start_url', 'scope', 'id'):
        if obj[k] != './':
            p.append('%s 要是 ./' % k)
    if obj['display'] != 'standalone':
        p.append('display 要是 standalone')
    for k in ('background_color', 'theme_color'):
        if not isinstance(obj[k], str) or not re.fullmatch(r'#[0-9a-f]{6}', obj[k]):
            p.append('%s 不是顏色' % k)
    for k in ('name', 'short_name', 'lang'):
        if not isinstance(obj[k], str) or not 1 <= len(obj[k]) <= 30 or _odd_chars(obj[k]):
            p.append('%s 不合規則' % k)
    icons = obj['icons']
    if not isinstance(icons, list) or not icons:
        return p + ['icons 不對']
    for i in icons:
        if not isinstance(i, dict) or not set(i) <= {'src', 'sizes', 'type', 'purpose'} or i.get('src') not in ICON_SRCS:
            p.append('icons 裡有不允許的項目')
            break
    if raw is not None and rules:
        p += _text_problems(raw, rules, [], [], [])
    return p


PNG_SIG = b'\x89PNG\r\n\x1a\n'


def check_png(data, size):
    """簽章、逐個 chunk 驗 CRC、只能 IHDR → IDAT+ → IEND、尺寸等於 size、8 bit、color type 2、IEND 後面不能有東西"""
    p = []
    if len(data) > LIMITS['png']:
        p.append('檔案太大')
    if data[:8] != PNG_SIG:
        return p + ['不是 PNG']
    pos, chunks = 8, []
    while pos < len(data):
        if pos + 12 > len(data):
            return p + ['PNG 結構不完整']
        n = struct.unpack('>I', data[pos:pos + 4])[0]
        typ, body, crc = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n], data[pos + 8 + n:pos + 12 + n]
        if len(body) != n or len(crc) != 4:
            return p + ['PNG 結構不完整']
        if zlib.crc32(typ + body) & 0xffffffff != struct.unpack('>I', crc)[0]:
            return p + ['PNG 的 CRC 不對']
        chunks.append((typ, body))
        pos += 12 + n
        if typ == b'IEND':
            break
    if pos < len(data):
        p.append('IEND 後面還有東西')
    kinds = [t for t, _ in chunks]
    if not (len(kinds) >= 3 and kinds[0] == b'IHDR' and kinds[-1] == b'IEND' and all(k == b'IDAT' for k in kinds[1:-1])):
        return p + ['PNG 的 chunk 只能是 IHDR、IDAT、IEND']
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        return p + ['IHDR 不對']
    w, h, depth, ctype, comp, filt, inter = struct.unpack('>IIBBBBB', ihdr)
    if (w, h) != (size, size):
        p.append('尺寸不對')
    if (depth, ctype, comp, filt, inter) != (8, 2, 0, 0, 0):
        p.append('PNG 格式不對（要 8 bit、RGB、不交錯）')
    return p


# ---------- PWA 檔案的產生器（內容都由常數產生，不讀任何使用者檔案） ----------
ICON_BLUE, ICON_WHITE = (0x00, 0x71, 0xe3), (0xff, 0xff, 0xff)
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect width="512" height="512" fill="#0071e3"/>'
            '<rect x="77" y="294" width="90" height="141" fill="#ffffff"/>'
            '<rect x="211" y="205" width="90" height="230" fill="#ffffff"/>'
            '<rect x="346" y="96" width="90" height="339" fill="#ffffff"/></svg>\n').encode('ascii')
README_PWA = '''# 台股盤後選股（分享版）

最新的頁面：{url}

這個儲存庫放的是網頁本身：`index.html`（分享版頁面，每個交易日更新一次）、`manifest.webmanifest`、`sw.js`、`icons/`
（讓網頁可以加到手機主畫面，看過的頁面沒有網路也能開），以及 `history/`（最近 120 個交易日，每天一個檔案，只有當天的候選標的）。
頁面只列出候選標的與關卡說明，不含任何個人資料。

僅供研究，非投資建議。
'''
README_GONE = '''# 台股盤後選股（分享版）

這個分享頁已經下架，網頁檔案都已刪除。
'''


def readme_pwa(url):
    return README_PWA.format(url=url).encode('utf-8')


def manifest_bytes():
    icons = [dict(src='icons/icon.svg', sizes='any', type='image/svg+xml', purpose='any'),
             dict(src='icons/icon-192.png', sizes='192x192', type='image/png', purpose='any'),
             dict(src='icons/icon-512.png', sizes='512x512', type='image/png', purpose='any'),
             dict(src='icons/maskable-512.png', sizes='512x512', type='image/png', purpose='maskable')]
    obj = dict(name=PWA_NAME, short_name=PWA_SHORT, lang='zh-Hant', start_url='./', scope='./', id='./',
               display='standalone', background_color=THEME_LIGHT, theme_color=THEME_LIGHT, icons=icons)
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _png_chunk(typ, body):
    return struct.pack('>I', len(body)) + typ + body + struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff)


@functools.lru_cache(maxsize=None)
def png_icon(size, maskable=False):
    """藍底、三根遞增的白色長條。maskable 版圖形縮在中央（長條範圍約 56%，在安全圈 80% 以內）；一般版放大到約 70%。"""
    s = 1.0 if maskable else 1.25
    u = lambda v: 0.5 + (v - 0.5) * s
    bars = []                                                   # (x0, x1, 頂端 y)，底邊同一條線
    bottom = round(u(0.78) * size)
    for x0, h in ((0.22, 0.22), (0.43, 0.36), (0.64, 0.53)):
        bars.append((round(u(x0) * size), round(u(x0 + 0.14) * size), bottom - round(h * s * size)))
    blue, white = bytes(ICON_BLUE), bytes(ICON_WHITE)
    cache, rows = {}, []
    for y in range(size):
        key = tuple(b[:2] for b in bars if b[2] <= y < bottom)
        if key not in cache:
            row, pos = bytearray(), 0
            for x0, x1 in key:
                row += blue * (x0 - pos) + white * (x1 - x0)
                pos = x1
            row += blue * (size - pos)
            cache[key] = b'\x00' + bytes(row)
        rows.append(cache[key])
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    return (PNG_SIG + _png_chunk(b'IHDR', ihdr) + _png_chunk(b'IDAT', zlib.compress(b''.join(rows), 9))
            + _png_chunk(b'IEND', b''))


# Service worker：只用相對路徑（網頁在 https://帳號.github.io/儲存庫/ 這種子路徑下）。
# 導覽 network-first（3 秒逾時就用快取的 ./；首頁回 404／410 就自毀，只有首頁會被存成 ./）、manifest 和圖示 cache-first、
# index.json network-first、日檔 stale-while-revalidate。@@VER@@ 和 @@DAYS@@ 是唯一的兩個洞。
SW_TEMPLATE = '''var VER = '@@VER@@';
var SHELL = 'twss-shell-' + VER;
var DATA = 'twss-data';
var PRE = ['./', 'manifest.webmanifest', 'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];
var DAYS = @@DAYS@@;
function warm(name, list) {
  return caches.open(name).then(function (c) {
    return Promise.all(list.map(function (u) { return c.add(new Request(u, { cache: 'reload' })).catch(function () { }); }));
  });
}
self.addEventListener('install', function (e) {
  e.waitUntil(Promise.all([warm(SHELL, PRE), warm(DATA, ['history/index.json'].concat(DAYS))]).then(function () { return self.skipWaiting(); }));
});
self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k.indexOf('twss-') === 0 && k !== SHELL && k !== DATA; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return caches.open(DATA); }).then(function (c) {
    return c.keys().then(function (rs) {
      var old = rs.filter(function (r) { return r.url.indexOf('index.json') < 0; });
      return Promise.all(old.slice(0, Math.max(0, old.length - 130)).map(function (r) { return c.delete(r); }));
    });
  }).then(function () { return self.clients.claim(); }));
});
function bye() {
  caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k.indexOf('twss-') === 0; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.registration.unregister(); });
}
function store(name, r, res) {
  if (res.ok) { var cp = res.clone(); caches.open(name).then(function (c) { return c.put(r, cp); }); }
  return res;
}
function nav(r, shell) {
  var timer = 0;
  var net = fetch(r).then(function (res) {
    clearTimeout(timer);
    if (!shell) { return res; }
    if (res.status === 404 || res.status === 410) { bye(); return res; }
    return store(SHELL, './', res);
  });
  var slow = new Promise(function (ok, no) {
    timer = setTimeout(function () { caches.match('./').then(function (m) { if (m) { ok(m); } else { no(); } }); }, 3e3);
  });
  return Promise.race([net, slow]).catch(function () {
    return caches.match('./').then(function (m) { return m || net; });
  });
}
function netFirst(r) {
  return fetch(r).then(function (res) { return store(DATA, r, res); }).catch(function () {
    return caches.match(r).then(function (m) { return m || Response.error(); });
  });
}
function stale(r) {
  return caches.open(DATA).then(function (c) {
    return c.match(r).then(function (m) {
      var net = fetch(r).then(function (res) { return store(DATA, r, res); });
      net.catch(function () { });
      return m || net;
    });
  });
}
function first(r) {
  return caches.match(r).then(function (m) { return m || fetch(r).then(function (res) { return store(SHELL, r, res); }); });
}
self.addEventListener('fetch', function (e) {
  var r = e.request;
  if (r.method !== 'GET') { return; }
  var sc = self.registration.scope;
  if (r.url.indexOf(sc) !== 0) { return; }
  var p = r.url.slice(sc.length).split('?')[0].split('#')[0];
  if (r.destination === 'document' || p === '' || p === 'index.html') { e.respondWith(nav(r, p === '' || p === 'index.html')); }
  else if (p === 'history/index.json') { e.respondWith(netFirst(r)); }
  else if (/^history\\/[0-9]{8}\\.json$/.test(p)) { e.respondWith(stale(r)); }
  else if (p === 'manifest.webmanifest' || p.indexOf('icons/') === 0) { e.respondWith(first(r)); }
});
'''


def sw_bytes(ver, days):
    """ver：版本字串（日期＋index.html 的指紋）；days：要在安裝時預存的日檔路徑（history/YYYYMMDD.json）"""
    assert re.fullmatch(r'[0-9]{8}-[0-9a-f]{8}', ver) and all(re.fullmatch(HIST_RE, d) for d in days)
    return SW_TEMPLATE.replace('@@VER@@', ver).replace('@@DAYS@@', json.dumps(list(days))).encode('utf-8')


def check_svg(data):
    return [] if data == ICON_SVG else ['icon.svg 和產生器的輸出不一樣']


def check_readme(data, url):
    return [] if data == readme_pwa(url) else ['README 和產生器的輸出不一樣']


def check_sw(data):
    """sw.js：從檔案裡讀出版本和預存的日檔，用產生器重做一份，位元組要完全一樣（所以改一個位元組就會被擋）"""
    if len(data) > LIMITS['sw']:
        return ['檔案太大']
    try:
        t = data.decode('utf-8')
        ver = re.search(r"^var VER = '([^']*)';$", t, re.M).group(1)
        days = json.loads(re.search(r'^var DAYS = (\[.*\]);$', t, re.M).group(1))
        return [] if sw_bytes(ver, days) == data else ['sw.js 和產生器的輸出不一樣']
    except (UnicodeDecodeError, AttributeError, ValueError, AssertionError, TypeError):
        return ['sw.js 和產生器的輸出不一樣']


def _day_entry(data):
    o = json.loads(data.decode('utf-8'))
    return dict(d=o['date'], n=o['n'], b=o['nb'])


def hist_index_bytes(entries):
    """entries：[{'d','n','b'}]，新到舊"""
    return json.dumps(dict(v=1, days=entries), separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def local_days(hist_dir, upto=None):
    """本機的日檔：{YYYYMMDD: 路徑}；upto 有給時只取日期 <= upto 的"""
    out = {}
    try:
        names = os.listdir(hist_dir)
    except OSError:
        return out
    for n in names:
        m = re.fullmatch(r'([0-9]{8})\.json', n)
        if m and (upto is None or m.group(1) <= upto):
            out[m.group(1)] = os.path.join(hist_dir, n)
    return out


def pwa_bundle(index_bytes, tag, hist_dir, rules, extra=None):
    """組出要公開的所有檔案並逐一檢查。回傳 ([(路徑, 位元組)], 問題清單)。
    順序：日檔（舊→新）→ icons → manifest → README → index.json → sw.js → index.html。
    extra：dict，可有 'url'（寫進 README 的網址）和 'days'（{YYYYMMDD: 位元組}，不在本機資料夾裡的日檔）。"""
    extra = extra or {}
    problems, days = [], {}
    for d, path in local_days(hist_dir, tag).items():
        try:
            with open(path, 'rb') as f:
                days[d] = f.read()
        except OSError:
            problems.append('history/%s.json：讀不到' % d)
    for d, b in (extra.get('days') or {}).items():
        if d <= tag:
            days.setdefault(d, b)
    keep = sorted(days)[-HISTORY_DAYS:]
    for d in keep:
        problems += ['history/%s.json：%s' % (d, x) for x in check_history(days[d], rules, d)[:3]]
    if not keep or keep[-1] != tag:
        problems.append('今天（%s）的歷史紀錄還沒有產生' % tag)
    if problems:
        return [], problems
    entries = [_day_entry(days[d]) for d in reversed(keep)]
    idx = hist_index_bytes(entries)
    ver = '%s-%s' % (tag, hashlib.sha1(index_bytes).hexdigest()[:8])
    files = [('history/%s.json' % d, days[d]) for d in keep]
    files += [('icons/icon.svg', ICON_SVG), ('icons/icon-192.png', png_icon(192)), ('icons/icon-512.png', png_icon(512)),
              ('icons/maskable-512.png', png_icon(512, True)), ('icons/apple-touch-icon.png', png_icon(180)),
              ('manifest.webmanifest', manifest_bytes()), ('README.md', readme_pwa(extra.get('url') or '')),
              (HIST_INDEX, idx), ('sw.js', sw_bytes(ver, ['history/%s.json' % d for d in reversed(keep)][:5])),
              ('index.html', index_bytes)]
    got = dict(files)
    for path, size in (('icons/icon-192.png', 192), ('icons/icon-512.png', 512), ('icons/maskable-512.png', 512),
                       ('icons/apple-touch-icon.png', 180)):
        problems += ['%s：%s' % (path, x) for x in check_png(got[path], size)]
    if len(ICON_SVG) > LIMITS['svg']:
        problems.append('icons/icon.svg：檔案太大')
    problems += ['icons/icon.svg：%s' % x for x in check_svg(got['icons/icon.svg'])]
    problems += ['README.md：%s' % x for x in check_readme(got['README.md'], extra.get('url') or '')]
    problems += ['manifest.webmanifest：%s' % x for x in check_manifest(got['manifest.webmanifest'], rules)]
    problems += ['%s：%s' % (HIST_INDEX, x) for x in check_hist_index(idx)]
    problems += ['sw.js：%s' % x for x in check_sw(got['sw.js'])]
    swp = _text_problems(got['sw.js'].decode('utf-8'), rules, [], [], [])
    problems += ['sw.js：%s' % x for x in swp[:3]]
    return files, problems


def is_pwa_path(path):
    return path in PWA_STATIC or path in SHARE_FILES or path == HIST_INDEX or re.fullmatch(HIST_RE, path) is not None


# ---------- 檔案與狀態 ----------
def blob_sha(data):
    """和 git／GitHub 的檔案 sha 一樣的算法"""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def latest_files(out_dir):
    """回傳 (最新的分享版路徑或 None, 它的日期 YYYYMMDD, 最新網頁報告的日期)"""
    share, report = {}, set()
    try:
        names = os.listdir(out_dir)
    except OSError:
        names = []
    for n in names:
        m = re.fullmatch(r'分享版_([0-9]{8})\.html', n)
        if m:
            share[m.group(1)] = os.path.join(out_dir, n)
        m = re.fullmatch(r'盤後選股_([0-9]{8})\.html', n)
        if m:
            report.add(m.group(1))
    tag = max(share) if share else None
    return (share[tag] if tag else None), tag, (max(report) if report else None)


STATE_FIELDS = ('repo', 'confirmed', 'confirmed_at', 'login', 'last_date', 'last_blob', 'pages_url')
PWA_STATE_FIELDS = ('pwa_repo', 'pwa_confirmed', 'pwa_confirmed_at', 'pwa_schema', 'last_bundle')


def load_state(path):
    try:
        with open(path, 'rb') as f:
            d = json.loads(f.read().decode('utf-8'))
    except (OSError, ValueError):
        return {}
    return {k: d[k] for k in STATE_FIELDS + PWA_STATE_FIELDS if k in d} if isinstance(d, dict) else {}


def save_state(path, st):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(json.dumps({k: st[k] for k in STATE_FIELDS + PWA_STATE_FIELDS if k in st}, ensure_ascii=False, indent=1).encode('utf-8'))
    os.replace(tmp, path)


def pages_url(owner, name):
    o = owner.lower()
    if name.lower() == o + '.github.io':
        return 'https://%s.github.io/' % o
    return 'https://%s.github.io/%s/' % (o, name)


# ---------- gh ----------
def _redact(text):
    text = re.sub(r'github_pat_[A-Za-z0-9_]{10,}', '***', text)
    text = re.sub(r'gh[pousr]_[A-Za-z0-9]{10,}', '***', text)
    text = re.sub(r'://[^/\s:@]+:[^@\s/]+@', '://***@', text)
    text = re.sub(r'(?i)\bauthorization\b\s*[:=]?\s*(?:(?:bearer|token|basic)\s+)?\S+', 'authorization: ***', text)
    text = re.sub(r'(?i)\b(token|bearer)\b\s*[:=]?\s*\S+', r'\1: ***', text)
    return text


def _clean(text):
    text = _redact(text or '').strip().replace('\r', '')
    return text if len(text) <= 300 else text[:300] + '…'


def _gh_exe():
    p = _which('gh.exe')
    if not p:
        q = _which('gh')
        if q and not q.lower().endswith(('.cmd', '.bat', '.ps1')) and (os.name != 'nt' or q.lower().endswith('.exe')):
            p = q
    if not p or p.lower().endswith(('.cmd', '.bat')):
        raise ShareError(E_GH_MISSING, '找不到 gh（GitHub 官方的指令列工具），或找到的不是正式的 gh.exe。',
                         '安裝 GitHub CLI（到 GitHub 官方網站搜尋「GitHub CLI」下載安裝），裝好後關掉視窗再執行一次。')
    return p


def _gh(args, body=None):
    exe = _gh_exe()
    env = dict(os.environ)
    env.update(GH_HOST='github.com', GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1', NO_COLOR='1',
               GH_SPINNER_DISABLED='1')
    try:
        p = _run([exe] + list(args), input=body, capture_output=True, timeout=60, cwd=tempfile.gettempdir(),
                 env=env, shell=False)
    except subprocess.TimeoutExpired:
        raise ShareError(E_GITHUB, 'gh 等了 60 秒都沒有回應。', '確認網路正常後再執行一次。')
    except OSError as e:
        raise ShareError(E_GH_MISSING, 'gh 沒辦法執行（%s）。' % _clean(str(e)), '重新安裝 GitHub CLI 後再執行一次。')
    dec = lambda b: b.decode('utf-8', 'replace') if isinstance(b, (bytes, bytearray)) else (b or '')
    return p.returncode, dec(p.stdout), dec(p.stderr)


def _call(args, body=None, allow=()):
    """執行 gh；回傳 (HTTP 代碼, stdout)。成功但沒有代碼時當作 200。其他情況丟 ShareError（代碼 12 或 14）"""
    rc, out, err = _gh(args, body)
    if rc == 0:
        return 200, out
    m = re.search(r'\(HTTP ([0-9]{3})\)', err) or re.search(r'\(HTTP ([0-9]{3})\)', out)
    code = int(m.group(1)) if m else None
    low = (err + out).lower()
    if rc == 4 or 'gh auth login' in low or code == 401:
        raise ShareError(E_GH_AUTH, 'gh 還沒有登入 GitHub（或登入已經過期）。', '在命令列視窗執行 gh auth login，照畫面登入後再執行一次。')
    if code in allow:
        return code, out
    snippet = _clean(err or out)
    if code == 429 or (code == 403 and 'rate limit' in low):
        raise ShareError(E_GITHUB, 'GitHub 說呼叫太頻繁（被限速）：%s' % snippet, '等 10～60 分鐘再執行一次。')
    if code == 403:
        raise ShareError(E_GITHUB, 'GitHub 拒絕了這個操作（403，可能是 gh 登入的權限不夠）：%s' % snippet,
                         '執行 gh auth status 看登入的帳號與權限，必要時重新 gh auth login。')
    if code is None:
        raise ShareError(E_GITHUB, 'gh 執行失敗，也沒有收到 GitHub 的回應代碼（可能連不上 GitHub，或 gh 本身有問題）：%s' % snippet,
                         '確認網路正常後再執行一次。')
    raise ShareError(E_GITHUB, 'GitHub 回應了錯誤（HTTP %d）：%s' % (code, snippet), '稍後再執行一次；持續發生的話請看上面的說明。')


def _api(path, method=None, body=None, fields=(), allow=()):
    args = ['api']
    if method:
        args += ['-X', method]
    args.append(path)
    if body is not None:
        args += ['--input', '-']
    for f in fields:
        args += ['-f', f]
    code, out = _call(args, body, allow)
    try:
        obj = json.loads(out) if out.strip() else None
    except ValueError:
        obj = None
    return code, obj


def _put(owner, name, path, data, ident, branch, sha, message):
    assert path in SHARE_FILES, path
    who = dict(name=ident['login'], email=ident['email'])
    body = dict(message=message, content=base64.b64encode(data).decode('ascii'), committer=who, author=who)
    if sha:
        body['sha'] = sha
    if branch:
        body['branch'] = branch
    return _api('repos/%s/%s/contents/%s' % (owner, name, path), 'PUT', json.dumps(body).encode('utf-8'))


def _reset_confirmed(state_path, st):
    st = dict(st, confirmed=False)
    if 'pwa_confirmed' in st:
        st['pwa_confirmed'] = False
    save_state(state_path, st)


def _whoami(owner):
    _, u = _api('user')
    login = (u or {}).get('login') if isinstance(u, dict) else None
    if not login:
        raise ShareError(E_GITHUB, 'GitHub 沒有告訴我們登入的是哪個帳號。', '執行 gh auth login 重新登入後再試。')
    if login.lower() != owner.lower():
        raise ShareError(E_SHARE, 'gh 登入的帳號是「%s」，但 share_repo 的帳號是「%s」，兩個要一樣。' % (login, owner),
                         '把 設定.txt 的 share_repo 改成 %s/儲存庫名稱，或用 gh auth login 換成正確的帳號。' % login)
    uid = u.get('id')
    email = '%s+%s@users.noreply.github.com' % (uid, login) if uid else '%s@users.noreply.github.com' % login
    return dict(login=login, email=email)


def _preview(say, repo, owner, name, date, idx_size, ncand, share_path, st):
    say('')
    say('【第一次公開前的確認】還沒有公開任何東西，也還沒有連到 GitHub。')
    say('要公開到：https://github.com/%s/%s（還不存在的話會自動建立，並設成公開）' % (owner, name))
    say('網頁網址：%s' % pages_url(owner, name))
    say('只會上傳 2 個檔案：')
    say('  - index.html（%.1f KB；候選標的 %d 檔；資料日期 %s）' % (idx_size / 1000, ncand, date))
    say('  - README.md（說明文字，%d 位元組）' % len(README_TEMPLATE.format(url=pages_url(owner, name)).encode('utf-8')))
    say('不會上傳：總資金、部位試算、觀察名單、自選股、各關明細、本機路徑、設定檔、data、csv、md、json。')
    say('安全檢查：通過（%s）' % share_path)
    say('注意：公開後任何人都看得到；網址被別人存檔、截圖、搜尋引擎收錄或轉貼後，無法收回。')
    old = st.get('repo')
    if old and old.lower() != repo.lower():
        say('提醒：之前確認過的是另一個儲存庫（%s），這次是新的，需要重新確認。' % old)
    say('之後要下架：%s' % TAKEDOWN_CMD)


def publish(repo, mode, out_dir, state_path, rules, ask=False, token=None, say=_say):
    """把最新的分享版公開到 repo（OWNER/NAME）。回傳 0 成功（或不用做事）、10 等待確認；失敗丟 ShareError。"""
    if mode != 'full':
        raise ShareError(E_SHARE, '分享版只支援全市場（mode=full），現在是自選股清單模式。',
                         '把 設定.txt 的 mode 改成 full，或把 share_publish 改成 0。')
    if not repo or '/' not in repo:
        raise ShareError(E_SHARE, '設定檔還沒有寫 share_repo（要公開到哪個 GitHub 儲存庫）。',
                         '在 設定.txt 加一行 share_repo=帳號/儲存庫名稱（先在 GitHub 準備一個專用的新儲存庫，名稱可以自己取）。')
    owner, name = repo.split('/', 1)
    share_path, tag, report_tag = latest_files(out_dir)
    if share_path is None or (report_tag and report_tag > tag):
        raise ShareError(E_LEAK, '今天沒有產生分享版，所以沒有公開任何東西（通常是掃描時的安全檢查沒有通過，原因會寫在選股的訊息裡）。',
                         '重新執行 run.bat 看選股時印出的說明；沒有問題的話分享版會自動產生。')
    with open(share_path, 'rb') as f:
        data = f.read()                   # 只讀一次：檢查的和上傳的是同一份位元組
    problems, info = inspect_html(data, rules)
    if problems:
        raise ShareError(E_LEAK, '分享版沒有通過安全檢查，所以沒有公開任何東西：\n  - ' + '\n  - '.join(problems[:8]),
                         '把 分享版 檔案刪掉再重新選股；問題一直出現的話，先不要公開（把 share_publish 改成 0）。')
    date = '%s-%s-%s' % (tag[:4], tag[4:6], tag[6:])
    blob = blob_sha(data)
    st = load_state(state_path)
    confirmed = bool(st.get('confirmed')) and (st.get('repo') or '').lower() == repo.lower()
    if confirmed:
        if st.get('last_blob') == blob:
            say('分享頁已經是最新的（資料日期 %s），不用更新。' % date)
            return 0
        if st.get('last_date') and date < st['last_date']:
            say('分享頁上的資料（%s）比這份（%s）還新，不用更新。' % (st['last_date'], date))
            return 0
    else:
        _preview(say, repo, owner, name, date, len(data), info['cands'], share_path, st)
        if token is not None:
            if token.strip().lower() != repo.lower():
                raise ShareError(E_SHARE, '--confirm-first-publish 後面寫的（%s）和 share_repo（%s）不一樣。' % (token, repo),
                                 '兩個要完全一樣，才表示你確認的是這個儲存庫。')
        elif ask and _isatty():
            try:
                ans = _ask('確定要公開嗎？要公開請輸入 Y 後按 Enter（直接按 Enter = 先不要）：')
            except (EOFError, KeyboardInterrupt):
                ans = ''
            if ans.strip().upper() not in ('Y', 'YES'):
                say('好的，沒有公開。要公開時再執行一次 run.bat；不想公開的話，把 設定.txt 的 share_publish 改成 0。')
                return E_CONFIRM
        else:
            say('')
            say('還沒有公開（等你確認）。要公開的話，請用其中一種方式：')
            say('  1. 雙擊 run.bat，看到問題時輸入 Y')
            say('  2. 在程式資料夾執行：python screener.py share --config 設定.txt --confirm-first-publish %s' % repo)
            say('不想公開的話，把 設定.txt 的 share_publish 改成 0。')
            return E_CONFIRM
    _gh_exe()
    ident = _whoami(owner)
    code, info_r = _api('repos/%s/%s' % (owner, name), allow=(404,))
    created = False
    if code == 404:
        if confirmed:
            _reset_confirmed(state_path, st)
            raise ShareError(E_SHARE, '儲存庫 %s 不見了（可能已經被刪除或下架），為了安全，程式不會自動重建。' % repo,
                             '要重新公開的話，再執行一次 run.bat 並重新確認；不想公開就把 share_publish 改成 0。')
        _call(['repo', 'create', repo, '--public', '--description', REPO_DESC, '--disable-issues', '--disable-wiki'])
        created = True
        code, info_r = _api('repos/%s/%s' % (owner, name), allow=(404,))
        if code == 404:
            _sleep(3)
            code, info_r = _api('repos/%s/%s' % (owner, name), allow=(404,))
        if code == 404:
            raise ShareError(E_GITHUB, '儲存庫剛建立，但 GitHub 還查不到它。', '等一兩分鐘再執行一次。')
    info_r = info_r if isinstance(info_r, dict) else {}
    if not created:
        if info_r.get('private') is not False:
            raise ShareError(E_SHARE, '儲存庫 %s 不是公開的（或查不到它的狀態），不會上傳。' % repo,
                             '分享版要用公開的儲存庫才能用 GitHub Pages；請換一個專用的公開儲存庫。')
        if info_r.get('archived'):
            raise ShareError(E_SHARE, '儲存庫 %s 已經封存（唯讀），不會上傳。' % repo, '換一個專用的儲存庫。')
        code, items = _api('repos/%s/%s/contents/' % (owner, name), allow=(404,))
        extra = sorted(str(i.get('name')) for i in items if isinstance(i, dict) and i.get('name') not in SHARE_FILES) \
            if isinstance(items, list) else []
        if code != 404 and not isinstance(items, list):
            raise ShareError(E_SHARE, '看不懂儲存庫 %s 的內容清單，不會上傳。' % repo, '換一個專用的新儲存庫。')
        if extra:
            raise ShareError(E_SHARE, '儲存庫 %s 裡有別的檔案（%s），不會上傳，以免跟它們混在一起。' % (repo, '、'.join(extra[:5])),
                             '請準備一個只放分享頁的專用新儲存庫（裡面只能有 index.html 和 README.md）。')
    branch = info_r.get('default_branch') or None
    url = pages_url(owner, name)
    c, cur = _api('repos/%s/%s/contents/README.md' % (owner, name), allow=(404,))
    if c == 404:
        _put(owner, name, 'README.md', README_TEMPLATE.format(url=url).encode('utf-8'), ident, branch, None, '新增 README')
    c, cur = _api('repos/%s/%s/contents/index.html' % (owner, name), allow=(404,))
    rsha = cur.get('sha') if c != 404 and isinstance(cur, dict) else None
    if rsha != blob:
        _put(owner, name, 'index.html', data, ident, branch, rsha, '更新分享頁 %s' % date)
    c, _ = _api('repos/%s/%s/pages' % (owner, name), allow=(404,))
    if c == 404:
        if confirmed:
            _reset_confirmed(state_path, st)
            raise ShareError(E_SHARE, '%s 的 GitHub Pages 已經被關掉了（可能是你下架過），為了安全，程式不會自動重開。' % repo,
                             '要重新公開的話，再執行一次 run.bat 並重新確認；不想公開就把 share_publish 改成 0。')
        fields = ['source[branch]=%s' % (branch or 'main'), 'source[path]=/']
        c, _ = _api('repos/%s/%s/pages' % (owner, name), 'POST', fields=fields, allow=(409, 422, 404))
        if c in (422, 404):
            _sleep(3)
            _api('repos/%s/%s/pages' % (owner, name), 'POST', fields=fields, allow=(409,))
    save_state(state_path, dict({k: st[k] for k in PWA_STATE_FIELDS if k in st},
                                repo=repo, confirmed=True, confirmed_at=st.get('confirmed_at') if confirmed
                                else time.strftime('%Y-%m-%d %H:%M:%S'), login=ident['login'], last_date=date,
                                last_blob=blob, pages_url=url))
    say('分享頁已經公開（資料日期 %s）：%s' % (date, url))
    if not confirmed:
        say('第一次建立網頁，GitHub 可能要 1～10 分鐘才打得開；之後每次選股完成會自動更新。')
    return 0


def _iso(d):
    return '%s-%s-%s' % (d[:4], d[4:6], d[6:])


def _fmt_size(n):
    return '%.1f KB' % (n / 1000)


def _preview_pwa(say, repo, owner, name, date, files, ncand, st):
    days = [f for f in files if re.fullmatch(HIST_RE, f[0])]
    say('')
    say('【第一次公開 PWA 版前的確認】還沒有公開任何東西，也還沒有連到 GitHub。')
    say('要公開到：https://github.com/%s/%s（還不存在的話會自動建立，並設成公開）' % (owner, name))
    say('網頁網址：%s' % pages_url(owner, name))
    say('會上傳 %d 個檔案（網頁本體、可以加到手機主畫面所需的檔案、每天的歷史紀錄）：' % len(files))
    for path, data in files:
        if re.fullmatch(HIST_RE, path) and len(days) > 10 and days[3][0] < path < days[-3][0]:
            if path == days[3][0]:
                say('  - …（中間的日檔省略）')
            continue
        extra = '；候選標的 %d 檔；資料日期 %s' % (ncand, date) if path == 'index.html' else ''
        say('  - %s（%s%s）' % (path, _fmt_size(len(data)), extra))
    if days:
        say('歷史紀錄：%d 個日檔，從 %s 到 %s；每個日檔只有當天候選標的的代號、名稱、型態、收盤、漲跌、量比、建議停損、類股、市場和連續天數。' % (
            len(days), _iso(days[0][0][8:16]), _iso(days[-1][0][8:16])))
    say('之後每天自動新增一天，並刪掉超過 %d 個交易日的舊檔。' % HISTORY_DAYS)
    say('不會上傳：總資金、部位試算、觀察名單、自選股、各關明細、本機路徑、設定檔、data、csv、md、json（歷史日檔除外）。')
    say('注意：公開後任何人都看得到；網址被別人存檔、截圖、搜尋引擎收錄或轉貼後，無法收回。已經安裝的人要等下次連網才會清掉離線資料。')
    old = st.get('pwa_repo') or st.get('repo')
    if old and old.lower() != repo.lower():
        say('提醒：之前確認過的是另一個儲存庫（%s），這次是新的，需要重新確認。' % old)
    elif st.get('confirmed') and not st.get('pwa_confirmed'):
        say('提醒：這個儲存庫之前只公開 index.html 和 README.md 兩個檔案，PWA 版會多出上面這些檔案，所以需要重新確認。')
    say('之後要下架：%s' % TAKEDOWN_CMD)


def _tree_check(owner, name, entries, repo):
    """遠端的檔案清單必須全部在白名單裡，否則什麼都不寫"""
    bad, remote = [], {}
    for e in entries:
        if not isinstance(e, dict):
            bad.append('?')
            continue
        path, typ, mode = str(e.get('path')), e.get('type'), e.get('mode')
        if typ == 'tree' and path in ('icons', 'history'):
            continue
        if typ == 'blob' and mode == '100644' and is_pwa_path(path) and path.encode('utf-8', 'replace').isascii():
            remote[path] = e.get('sha')
        else:
            bad.append(_clean(path)[:40])
    if bad:
        raise ShareError(E_SHARE, '儲存庫 %s 裡有不是這個程式放的檔案（%s），不會上傳，以免跟它們混在一起。' % (repo, '、'.join(bad[:5])),
                         '請準備一個只放分享頁的專用新儲存庫。')
    return remote


def publish_pwa(repo, mode, out_dir, hist_dir, state_path, rules, ask=False, token=None, say=_say):
    """PWA 版的公開：用 Git Data API，一次發佈只做一個 commit（中途失敗時頁面不會壞）。回傳 0 成功（或不用做事）、10 等待確認。"""
    if mode != 'full':
        raise ShareError(E_SHARE, '分享版只支援全市場（mode=full），現在是自選股清單模式。',
                         '把 設定.txt 的 mode 改成 full，或把 share_publish 改成 0。')
    if not repo or '/' not in repo:
        raise ShareError(E_SHARE, '設定檔還沒有寫 share_repo（要公開到哪個 GitHub 儲存庫）。',
                         '在 設定.txt 加一行 share_repo=帳號/儲存庫名稱（先在 GitHub 準備一個專用的新儲存庫，名稱可以自己取）。')
    owner, name = repo.split('/', 1)
    url = pages_url(owner, name)
    share_path, tag, report_tag = latest_files(out_dir)
    if share_path is None or (report_tag and report_tag > tag):
        raise ShareError(E_LEAK, '今天沒有產生分享版，所以沒有公開任何東西（通常是掃描時的安全檢查沒有通過，原因會寫在選股的訊息裡）。',
                         '重新執行 run.bat 看選股時印出的說明；沒有問題的話分享版會自動產生。')
    with open(share_path, 'rb') as f:
        data = f.read()                   # 只讀一次：檢查的和上傳的是同一份位元組
    problems, info = inspect_html(data, rules, pwa=True)
    if problems:
        raise ShareError(E_LEAK, '分享版沒有通過安全檢查，所以沒有公開任何東西：\n  - ' + '\n  - '.join(problems[:8]),
                         '把 分享版 檔案刪掉再重新選股；問題一直出現的話，先不要公開（把 share_publish 改成 0）。')
    files, problems = pwa_bundle(data, tag, hist_dir, rules, dict(url=url))
    if problems:
        raise ShareError(E_LEAK, '歷史紀錄或 PWA 檔案沒有通過安全檢查，所以沒有公開任何東西：\n  - ' + '\n  - '.join(problems[:8]),
                         '把 cache\\share\\history 裡有問題的那個日檔刪掉（下次選股會重新產生今天的），再試一次。')
    date = '%s-%s-%s' % (tag[:4], tag[4:6], tag[6:])
    finger = hashlib.sha1('\n'.join('%s %s' % (p, blob_sha(b)) for p, b in files).encode('utf-8')).hexdigest()
    st = load_state(state_path)
    confirmed = (bool(st.get('pwa_confirmed')) and (st.get('pwa_repo') or '').lower() == repo.lower()
                 and st.get('pwa_schema') == PWA_SCHEMA)
    if confirmed:
        if st.get('last_bundle') == finger:
            say('分享頁已經是最新的（資料日期 %s），不用更新。' % date)
            return 0
        if st.get('last_date') and date < st['last_date']:
            say('分享頁上的資料（%s）比這份（%s）還新，不用更新。' % (st['last_date'], date))
            return 0
    else:
        _preview_pwa(say, repo, owner, name, date, files, info['cands'], st)
        if token is not None:
            if token.strip().lower() != repo.lower():
                raise ShareError(E_SHARE, '--confirm-pwa 後面寫的（%s）和 share_repo（%s）不一樣。' % (token, repo),
                                 '兩個要完全一樣，才表示你確認的是這個儲存庫。')
        elif ask and _isatty():
            try:
                ans = _ask('確定要公開 PWA 版嗎？要公開請輸入 Y 後按 Enter（直接按 Enter = 先不要）：')
            except (EOFError, KeyboardInterrupt):
                ans = ''
            if ans.strip().upper() not in ('Y', 'YES'):
                say('好的，沒有公開。要公開時再執行一次 run.bat；不想公開的話，把 設定.txt 的 share_pwa 改回 0。')
                return E_CONFIRM
        else:
            say('')
            say('還沒有公開（等你確認）。要公開的話，請用其中一種方式：')
            say('  1. 雙擊 run.bat，看到問題時輸入 Y')
            say('  2. 在程式資料夾執行：python screener.py share --config 設定.txt --confirm-pwa %s' % repo)
            say('不想公開的話，把 設定.txt 的 share_pwa 改回 0。')
            return E_CONFIRM
    _gh_exe()
    ident = _whoami(owner)
    code, info_r = _api('repos/%s/%s' % (owner, name), allow=(404,))
    created = False
    if code == 404:
        if confirmed:
            _reset_confirmed(state_path, st)
            raise ShareError(E_SHARE, '儲存庫 %s 不見了（可能已經被刪除或下架），為了安全，程式不會自動重建。' % repo,
                             '要重新公開的話，再執行一次 run.bat 並重新確認；不想公開就把 share_pwa 改回 0。')
        _call(['repo', 'create', repo, '--public', '--description', REPO_DESC, '--disable-issues', '--disable-wiki'])
        created = True
        code, info_r = _api('repos/%s/%s' % (owner, name), allow=(404,))
        if code == 404:
            _sleep(3)
            code, info_r = _api('repos/%s/%s' % (owner, name), allow=(404,))
        if code == 404:
            raise ShareError(E_GITHUB, '儲存庫剛建立，但 GitHub 還查不到它。', '等一兩分鐘再執行一次。')
    info_r = info_r if isinstance(info_r, dict) else {}
    if not created:
        if info_r.get('private') is not False:
            raise ShareError(E_SHARE, '儲存庫 %s 不是公開的（或查不到它的狀態），不會上傳。' % repo,
                             '分享版要用公開的儲存庫才能用 GitHub Pages；請換一個專用的公開儲存庫。')
        if info_r.get('archived'):
            raise ShareError(E_SHARE, '儲存庫 %s 已經封存（唯讀），不會上傳。' % repo, '換一個專用的儲存庫。')
    branch = info_r.get('default_branch') or 'main'
    api = 'repos/%s/%s/git' % (owner, name)
    c, ref = _api('%s/ref/heads/%s' % (api, branch), allow=(404, 409))
    if c in (404, 409) or not isinstance(ref, dict):                 # 空的儲存庫：先用 contents API 放一個 README，才有第一個 commit
        _put(owner, name, 'README.md', readme_pwa(url), ident, branch, None, '新增 README')
        c, ref = _api('%s/ref/heads/%s' % (api, branch))
    head = ((ref or {}).get('object') or {}).get('sha') if isinstance(ref, dict) else None
    if not head:
        raise ShareError(E_GITHUB, 'GitHub 沒有告訴我們 %s 分支現在的版本。' % branch, '稍後再執行一次。')
    _, cm = _api('%s/commits/%s' % (api, head))
    base_tree = ((cm or {}).get('tree') or {}).get('sha') if isinstance(cm, dict) else None
    _, tr = _api('%s/trees/%s?recursive=1' % (api, base_tree))
    if not base_tree or not isinstance(tr, dict) or not isinstance(tr.get('tree'), list):
        raise ShareError(E_GITHUB, '看不懂儲存庫 %s 的檔案清單。' % repo, '稍後再執行一次。')
    if tr.get('truncated'):
        raise ShareError(E_SHARE, '儲存庫 %s 的檔案太多，清單被截斷，不會上傳。' % repo, '請準備一個只放分享頁的專用新儲存庫。')
    remote = _tree_check(owner, name, tr['tree'], repo)
    # 遠端有、本機沒有的日期：驗證後還原到本機（驗證不過的在同一個 commit 刪掉）
    mine = set(local_days(hist_dir))
    restored, bad_days = {}, set()
    for path in sorted(remote):
        m = re.fullmatch(HIST_RE, path)
        if not m or m.group(1) in mine:
            continue
        d = m.group(1)
        _, bl = _api('%s/blobs/%s' % (api, remote[path]))
        try:
            raw = base64.b64decode((bl or {}).get('content') or '')
        except (ValueError, TypeError, AttributeError):
            raw = b''
        if blob_sha(raw) == remote[path] and not check_history(raw, rules, d):
            restored[d] = raw
        else:
            bad_days.add(d)
    if restored:
        os.makedirs(hist_dir, exist_ok=True)
        window = set(sorted(mine | set(restored))[-HISTORY_DAYS:])
        for d, raw in restored.items():
            if d <= tag and d in window:
                tmp = os.path.join(hist_dir, d + '.json.tmp')
                with open(tmp, 'wb') as f:
                    f.write(raw)
                os.replace(tmp, os.path.join(hist_dir, d + '.json'))
        files, problems = pwa_bundle(data, tag, hist_dir, rules, dict(url=url))
        if problems:
            raise ShareError(E_LEAK, '歷史紀錄或 PWA 檔案沒有通過安全檢查，所以沒有公開任何東西：\n  - ' + '\n  - '.join(problems[:8]),
                             '把 cache\\share\\history 裡有問題的那個日檔刪掉，再試一次。')
        finger = hashlib.sha1('\n'.join('%s %s' % (p, blob_sha(b)) for p, b in files).encode('utf-8')).hexdigest()
    keep_paths = {p for p, _ in files}
    changed = [(p, b) for p, b in files if remote.get(p) != blob_sha(b)]
    gone = sorted(p for p in remote if re.fullmatch(HIST_RE, p) and p not in keep_paths)
    if changed or gone:
        who = dict(name=ident['login'], email=ident['email'])
        entries = []
        for i, (p, b) in enumerate(changed):
            if i and len(changed) > 30:
                _sleep(1)
            _, bo = _api('%s/blobs' % api, 'POST', json.dumps(dict(content=base64.b64encode(b).decode('ascii'), encoding='base64')).encode('utf-8'))
            sha = (bo or {}).get('sha') if isinstance(bo, dict) else None
            if sha != blob_sha(b):
                raise ShareError(E_GITHUB, 'GitHub 收到的 %s 和本機的不一樣，沒有更新任何東西。' % p, '稍後再執行一次。')
            entries.append(dict(path=p, mode='100644', type='blob', sha=sha))
        entries += [dict(path=p, mode='100644', type='blob', sha=None) for p in gone]
        _, nt = _api('%s/trees' % api, 'POST', json.dumps(dict(base_tree=base_tree, tree=entries)).encode('utf-8'))
        _, nc = _api('%s/commits' % api, 'POST', json.dumps(dict(
            message='更新分享頁 %s' % date, tree=(nt or {}).get('sha'), parents=[head], author=who, committer=who)).encode('utf-8'))
        _api('%s/refs/heads/%s' % (api, branch), 'PATCH', json.dumps(dict(sha=(nc or {}).get('sha'), force=False)).encode('utf-8'))
    c, _ = _api('repos/%s/%s/pages' % (owner, name), allow=(404,))
    if c == 404:
        if confirmed:
            _reset_confirmed(state_path, st)
            raise ShareError(E_SHARE, '%s 的 GitHub Pages 已經被關掉了（可能是你下架過），為了安全，程式不會自動重開。' % repo,
                             '要重新公開的話，再執行一次 run.bat 並重新確認；不想公開就把 share_pwa 改回 0。')
        fields = ['source[branch]=%s' % branch, 'source[path]=/']
        c, _ = _api('repos/%s/%s/pages' % (owner, name), 'POST', fields=fields, allow=(409, 422, 404))
        if c in (422, 404):
            _sleep(3)
            _api('repos/%s/%s/pages' % (owner, name), 'POST', fields=fields, allow=(409,))
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    save_state(state_path, dict(st, repo=repo, confirmed=True, confirmed_at=st.get('confirmed_at') or now, login=ident['login'],
                                last_date=date, last_blob=blob_sha(data), pages_url=url, pwa_repo=repo, pwa_confirmed=True,
                                pwa_confirmed_at=st.get('pwa_confirmed_at') if confirmed else now, pwa_schema=PWA_SCHEMA,
                                last_bundle=finger))
    say('分享頁已經公開（資料日期 %s，歷史 %d 天）：%s' % (date, sum(1 for p in keep_paths if re.fullmatch(HIST_RE, p)), url))
    if not confirmed:
        say('第一次建立網頁，GitHub 可能要 1～10 分鐘才打得開；之後每次選股完成會自動更新。')
    return 0


def _pwa_takedown_commit(repo, say):
    """PWA 版下架的第一步：一個 commit 刪掉 README 以外的所有檔案，README 換成下架說明。失敗只說明，不中斷後面關 Pages。"""
    owner, name = repo.split('/', 1)
    try:
        ident = _whoami(owner)
        api = 'repos/%s/%s/git' % (owner, name)
        _, info_r = _api('repos/%s/%s' % (owner, name))
        branch = (info_r or {}).get('default_branch') or 'main' if isinstance(info_r, dict) else 'main'
        _, ref = _api('%s/ref/heads/%s' % (api, branch))
        head = ref['object']['sha']
        _, cm = _api('%s/commits/%s' % (api, head))
        base_tree = cm['tree']['sha']
        _, tr = _api('%s/trees/%s?recursive=1' % (api, base_tree))
        gone = [e['path'] for e in tr['tree'] if e.get('type') == 'blob' and e.get('path') != 'README.md']
        body = README_GONE.encode('utf-8')
        _, bo = _api('%s/blobs' % api, 'POST', json.dumps(dict(content=base64.b64encode(body).decode('ascii'), encoding='base64')).encode('utf-8'))
        entries = [dict(path='README.md', mode='100644', type='blob', sha=bo['sha'])]
        entries += [dict(path=p, mode='100644', type='blob', sha=None) for p in gone]
        who = dict(name=ident['login'], email=ident['email'])
        _, nt = _api('%s/trees' % api, 'POST', json.dumps(dict(base_tree=base_tree, tree=entries)).encode('utf-8'))
        _, nc = _api('%s/commits' % api, 'POST', json.dumps(dict(
            message='下架分享頁', tree=nt['sha'], parents=[head], author=who, committer=who)).encode('utf-8'))
        _api('%s/refs/heads/%s' % (api, branch), 'PATCH', json.dumps(dict(sha=nc['sha'], force=False)).encode('utf-8'))
        say('已經用一個 commit 刪掉網頁檔案（共 %d 個），README 換成下架說明。' % len(gone))
        return True
    except (ShareError, KeyError, TypeError, ValueError, AttributeError) as e:
        say('沒辦法刪除網頁檔案（%s），繼續關閉 GitHub Pages。' % _clean(getattr(e, 'cause', None) or repr(e)))
        return False


def takedown(repo, state_path, say=_say):
    """下架：列出指令；在互動視窗裡輸入儲存庫名稱確認後才會真的執行（先關 Pages，再另外確認要不要刪儲存庫）"""
    if not repo or '/' not in repo:
        raise ShareError(E_SHARE, '不知道要下架哪個儲存庫。', '用 --takedown 帳號/儲存庫名稱 指定，或先在 設定.txt 寫 share_repo。')
    say('【下架分享頁】儲存庫：%s' % repo)
    say('  關閉網頁：gh api -X DELETE repos/%s/pages' % repo)
    say('  刪除整個儲存庫：gh repo delete %s --yes（需要先執行 gh auth refresh -h github.com -s delete_repo）' % repo)
    say('  或執行：%s' % TAKEDOWN_CMD)
    st0 = load_state(state_path)
    pwa = bool(st0.get('pwa_confirmed')) and (st0.get('pwa_repo') or '').lower() == repo.lower()
    if pwa:
        say('  PWA 版會先用一個 commit 刪掉 README.md 以外的所有檔案（README 換成下架說明），再關閉網頁。')
        say('  git 的歷史紀錄裡還留著舊檔，只有刪除整個儲存庫才會完全消失。')
    say('下架後請把 設定.txt 的 share_publish 改成 0，不然下次選股完成會再問你要不要公開。')
    if not _isatty():
        say('（現在不是在互動視窗裡，只列出指令，沒有執行任何事。）')
        return 0
    try:
        ans = _ask('要關閉網頁請輸入儲存庫名稱「%s」後按 Enter（直接按 Enter = 先不要）：' % repo)
    except (EOFError, KeyboardInterrupt):
        ans = ''
    if ans.strip().lower() != repo.lower():
        say('沒有做任何事。')
        return 0
    _gh_exe()
    if pwa:
        _pwa_takedown_commit(repo, say)
    _api('repos/%s/pages' % repo, 'DELETE', allow=(404,))
    st = load_state(state_path)
    if st:
        _reset_confirmed(state_path, st)
    say('已經關閉 GitHub Pages，網頁會打不開（搜尋引擎或別人存的備份不在我們控制內）。')
    try:
        ans = _ask('也要刪除整個儲存庫嗎？要刪請再輸入一次儲存庫名稱「%s」（直接按 Enter = 不刪）：' % repo)
    except (EOFError, KeyboardInterrupt):
        ans = ''
    if ans.strip().lower() == repo.lower():
        _call(['repo', 'delete', repo, '--yes'])
        say('已經刪除儲存庫 %s。' % repo)
    return 0
