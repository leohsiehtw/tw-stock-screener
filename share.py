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
"""
import base64, getpass, hashlib, html, json, os, re, shutil, socket, subprocess, sys, tempfile, time, unicodedata
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

E_CONFIRM, E_GH_MISSING, E_GH_AUTH, E_LEAK, E_GITHUB, E_SHARE = 10, 11, 12, 13, 14, 15
MAX_BYTES = 300_000
DISCLAIMER = '僅供研究，非投資建議'
CODE_RE = re.compile(r'[0-9]{4}[0-9A-Z]{0,2}')
SHARE_FILES = ('index.html', 'README.md')
FORBIDDEN_WORDS = ('部位', '總資金', '風險', '自選股', '觀察名單', '試算')
CONFIG_KEY_NAMES = ('capital', 'risk', 'mode', 'watchlist', 'output_dir', 'share', 'share_publish', 'share_repo')
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


def make_rules(capital=None, amounts=(), paths=(), watchlist=None, repo=None, people=()):
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
    return dict(patterns=pats, values=values, watchlist=wl)


ALLOWED_TAGS = {'html', 'head', 'meta', 'title', 'style', 'body', 'div', 'span', 'main', 'header', 'section', 'footer',
                'nav', 'h1', 'h2', 'h3', 'p', 'br', 'ul', 'li', 'a', 'dl', 'dt', 'dd', 'small', 'table', 'caption',
                'thead', 'tbody', 'tr', 'th', 'td', 'fieldset', 'legend', 'label', 'input', 'select', 'option',
                'button', 'script'}
VOID_TAGS = {'meta', 'br', 'input'}
ALLOWED_IDS = {'top', 'cands', 'cands-h', 'help', 'help-h', 'ct', 'cq', 'csort', 'cstatus', 'cnone'}
REQUIRED_IDS = ('cands', 'help')
DATA_ATTRS = {'data-i', 'data-type', 'data-kind', 'data-code', 'data-name', 'data-close', 'data-chg', 'data-vr',
              'data-stop', 'data-dist', 'data-label', 'data-key', 'data-num'}
PLAIN_ATTRS = {'lang', 'aria-hidden', 'aria-label', 'aria-labelledby', 'aria-live', 'role', 'scope', 'colspan',
               'for', 'autocomplete', 'placeholder', 'value', 'checked', 'hidden', 'selected'}
STYLE_RE = re.compile(r'(?:(?:left|width):-?[0-9]+(?:\.[0-9]+)?%;?|gap:8px;?)+')
CLASS_RE = re.compile(r'[A-Za-z0-9 _-]*')
META_OK = {('viewport', 'width=device-width, initial-scale=1'), ('color-scheme', 'light dark'),
           ('robots', 'noindex, nofollow')}
NOSTYLE_LABELS = {'有千分位逗號的數字（可能是金額）'}      # 這條規則不檢查 <style> 裡的 CSS（rgba(255,255,255,…) 之類）
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems, self.counts, self.ids = [], {}, set()
        self.cands, self.meta = [], set()

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
        if tag not in ALLOWED_TAGS:
            self.bad('頁面裡有不允許的標籤 <%s>' % tag[:20])
            return
        amap = {}
        for k, v in attrs:
            amap[k] = v
            self.attr(tag, k, v)
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

    def check_meta(self, amap):
        keys = set(amap)
        if keys == {'charset'}:
            if (amap['charset'] or '').lower() != 'utf-8':
                self.bad('<meta charset> 不是 utf-8')
        elif keys == {'name', 'content'} and (amap['name'], amap['content']) in META_OK:
            self.meta.add(amap['name'])
        else:
            self.bad('頁面裡有不允許的 <meta>')

    def attr(self, tag, k, v):
        k = k.lower()
        if k.startswith('on'):
            self.bad('頁面裡有事件屬性（%s）' % k[:20])
        elif k == 'id':
            if v not in ALLOWED_IDS:
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
        elif k in DATA_ATTRS:
            if v is None or len(v) > 100 or CTRL_RE.search(v):
                self.bad('%s 的內容不合規則' % k)
        elif k in PLAIN_ATTRS:
            if v is not None and (len(v) > 100 or CTRL_RE.search(v)):
                self.bad('%s 的內容不合規則' % k)
        else:
            self.bad('頁面裡有不允許的屬性（%s）' % re.sub(r'[^A-Za-z0-9_-]', '?', k)[:20])


def _mask_watch_text(text, cand_codes, cand_names):
    text = re.sub(r'<style>.*?</style>', ' ', text, flags=re.S)
    text = re.sub(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', ' ', text)
    text = re.sub(r'(?<![0-9])[0-9]++(?=\s*+檔)', ' ', text)      # 佔有量詞：很長的數字串也不會變慢
    for s in sorted(set(cand_names) | set(cand_codes), key=len, reverse=True):
        if s:
            text = text.replace(s, ' ')
    return text


def inspect_html(data, rules):
    """檢查要公開的位元組。回傳 (問題清單, 資訊)；問題清單空 = 通過。問題文字不會寫出被找到的內容本身。"""
    problems, info = [], dict(cands=0)
    if len(data) > MAX_BYTES:
        problems.append('檔案太大（%d KB，上限 %d KB）' % (len(data) // 1000, MAX_BYTES // 1000))
    if data.startswith(b'\xef\xbb\xbf'):
        problems.append('檔案開頭有 BOM')
    try:
        raw = data.decode('utf-8')
    except UnicodeDecodeError:
        return problems + ['檔案不是 UTF-8 編碼'], info
    ck = _Checker()
    try:
        ck.feed(raw)
        ck.close()
    except Exception:
        ck.bad('頁面結構無法解析')
    problems += ck.problems
    for tag in ('script', 'style', 'title'):
        if ck.counts.get(tag, 0) != 1:
            problems.append('<%s> 要剛好 1 個，現在是 %d 個' % (tag, ck.counts.get(tag, 0)))
    for i in REQUIRED_IDS:
        if i not in ck.ids:
            problems.append('缺少必要的區塊（%s）' % i)
    if 'robots' not in ck.meta:
        problems.append('缺少 noindex 的 robots 設定')
    if DISCLAIMER not in raw:
        problems.append('缺少免責聲明「%s」' % DISCLAIMER)
    info['cands'] = len(ck.cands)
    if _odd_chars(raw):
        problems.append('頁面裡有看不見的控制字元（零寬字元、方向控制、私用區或未定義的字元）')
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
        codes = [c for c, _ in ck.cands]
        names = [n for _, n in ck.cands]
        found = False
        for text in _uniq([_mask_watch_text(un, codes, names),
                           _mask_watch_text(sq, [_nf(c) for c in codes], [_strip_cf(_nf(n)) for n in names])]):
            for code, name in wl.items():
                if re.search(r'(?<![0-9A-Za-z.])' + re.escape(code) + r'(?![0-9A-Za-z]|\.[0-9])', text):
                    found = True
                elif len(name) >= 2 and name not in names and _nf(name) not in [_nf(n) for n in names] and name in text:
                    found = True
        if found:
            problems.append('頁面裡出現了自選股清單裡、但不是今天候選標的的股票')
    return problems[:30], info


def check_html(data, rules):
    return inspect_html(data, rules)[0]


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


def load_state(path):
    try:
        with open(path, 'rb') as f:
            d = json.loads(f.read().decode('utf-8'))
    except (OSError, ValueError):
        return {}
    return {k: d[k] for k in STATE_FIELDS if k in d} if isinstance(d, dict) else {}


def save_state(path, st):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(json.dumps({k: st[k] for k in STATE_FIELDS if k in st}, ensure_ascii=False, indent=1).encode('utf-8'))
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
    save_state(state_path, dict(repo=repo, confirmed=True, confirmed_at=st.get('confirmed_at') if confirmed
                                else time.strftime('%Y-%m-%d %H:%M:%S'), login=ident['login'], last_date=date,
                                last_blob=blob, pages_url=url))
    say('分享頁已經公開（資料日期 %s）：%s' % (date, url))
    if not confirmed:
        say('第一次建立網頁，GitHub 可能要 1～10 分鐘才打得開；之後每次選股完成會自動更新。')
    return 0


def takedown(repo, state_path, say=_say):
    """下架：列出指令；在互動視窗裡輸入儲存庫名稱確認後才會真的執行（先關 Pages，再另外確認要不要刪儲存庫）"""
    if not repo or '/' not in repo:
        raise ShareError(E_SHARE, '不知道要下架哪個儲存庫。', '用 --takedown 帳號/儲存庫名稱 指定，或先在 設定.txt 寫 share_repo。')
    say('【下架分享頁】儲存庫：%s' % repo)
    say('  關閉網頁：gh api -X DELETE repos/%s/pages' % repo)
    say('  刪除整個儲存庫：gh repo delete %s --yes（需要先執行 gh auth refresh -h github.com -s delete_repo）' % repo)
    say('  或執行：%s' % TAKEDOWN_CMD)
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
