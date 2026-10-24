"""從證交所、櫃買中心抓全市場日 K，建立 data/<代號>.csv 給 screener.py 用。只用 Python 內建模組。

用法：
  python fetch_data.py backfill [--start YYYYMMDD] [--end YYYYMMDD]   補歷史（預設今天往前 430 天，可中斷續跑）
  python fetch_data.py daily                                          每天收盤後增量更新（需先 backfill 過）
  python fetch_data.py build                                          只用 cache 重建 data/*.csv（離線）

設計：cache/ 放原始每日快照（未還原），data/*.csv 每次都由 cache + 除權息事件整批重建，
還原價不會被重複套用。csv 欄位 date,open,high,low,close,volume,raw_close,name。

結束代碼：0 成功；3 連不上官方網站；4 官方網站回應異常或資料格式不對；
5 官方還沒提供完整資料（上市有、上櫃還沒有）；6 還沒有任何股價資料（要先 backfill）；
1 程式發生未預期的錯誤。
"""
import argparse, csv, datetime as dt, glob, http.client, json, os, re, ssl, sys, time, traceback
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'cache')
DATA = os.path.join(HERE, 'data')

# ---- 資料來源（2026-10-24 實測）----
# 上市每日收盤行情：date=YYYYMMDD；回傳 {"stat","tables":[...]}，找 fields 含「證券代號」「收盤價」的 table
#   （舊格式為 fields9/data9）。成交股數單位為「股」。休市日或當日尚未公布 stat="很抱歉，沒有符合條件的資料!"。
TWSE_DAY = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json'
# 上櫃每日收盤行情：date=YYYY/MM/DD（%2F）；tables[0] 的 fields 含「代號 名稱 收盤 開盤 最高 最低 成交股數」
#   （欄名帶空白要 strip），成交股數單位為「股」。休市日 totalCount=0、data=[]。
#   官方 OpenAPI 只有最新一天，不能回補，所以用這個網站內部端點。
TPEX_DAY = 'https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash}&type=EW&response=json'
# 上市除權息計算結果表：startDate/endDate=YYYYMMDD；日期為民國「115年09月01日」；欄 股票代號、除權息前收盤價、除權息參考價
TWSE_EX = 'https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={s}&endDate={e}&response=json'
# 上櫃除權息：startDate/endDate=YYYY/MM/DD；日期為民國「115/09/01」；欄 代號、除權息前收盤價、除權息參考價
TPEX_EX = 'https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={s}&endDate={e}&response=json'

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
GAP = 3.5                 # 任兩次請求最短間隔（秒）
RETRY = (10, 30, 90)      # 失敗後等待秒數再重試
KEEP = re.compile(r'^(?:[1-9]\d{3}|00\d{2,4}[A-Z]?)$')   # 一般股 + ETF
NODATA = '沒有符合條件'

E_NETWORK, E_SITE, E_NODATA, E_NEED_DATA = 3, 4, 5, 6

MSG_NETWORK = ('連不上證交所或櫃買中心的網站（可能沒有網路，或被公司網路、防毒軟體擋住）。',
               '確認瀏覽器打得開 https://www.twse.com.tw/ 之後再執行一次；已經抓到的資料會保留。')
MSG_SITE = ('官方網站暫時沒有正常回應（可能在維護、太忙，或暫時擋住太頻繁的連線）{days}。',
            '等 30 分鐘以上再執行一次，只會補抓缺的日期；不要連續重跑。')
MSG_FORMAT = ('官方網站回傳的資料格式和程式認得的不一樣（網站可能改版了）。',
              '請到 GitHub 專案頁看有沒有新版本；沒有的話請開 Issue 並附上 logs 資料夾裡的紀錄。')
MSG_NODATA = ('官方還沒有提供完整資料：{days} 證交所有、櫃買中心還沒有。',
              '通常是官方還在更新，請 30 分鐘後再執行一次；隔天還是這樣的話，請看 README 的常見問題。')
MSG_NEED = ('還沒有任何股價資料（cache 資料夾是空的）。',
            '請先雙擊 setup.bat，問到「要現在下載嗎」時選 Y；或在程式資料夾執行 python fetch_data.py backfill。')

_last = [0.0]


class FetchError(Exception):
    """網路／HTTP／非 JSON／暫時性錯誤，可重試。kind：network 連不上；site 官方回應異常"""

    def __init__(self, msg, kind='site'):
        super().__init__(msg)
        self.kind = kind


class Structure(Exception):
    """回傳結構認不得，不重試"""


class UserError(Exception):
    """給使用者看的失敗：代碼、原因、怎麼做"""

    def __init__(self, code, cause, todo):
        super().__init__(cause)
        self.code, self.cause, self.todo = code, cause, todo


def _tee(line):
    p = os.environ.get('TWSTOCK_LOG')
    if p:
        try:
            with open(p, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


def log(*a):
    line = ' '.join([dt.datetime.now().strftime('%H:%M:%S')] + [str(x) for x in a])
    print(line, flush=True)
    _tee(line)


def fail(code, cause, todo):
    for line in ('', '【沒有完成】', '原因：' + cause, '怎麼做：' + todo):
        print(line, flush=True)
        _tee(line)
    sys.exit(code)


def fmt_days(ds):
    return '、'.join(ds[:5]) + (' 等 %d 天' % len(ds) if len(ds) > 5 else '')


# ---------- 小工具 ----------
def num(s):
    if s is None:
        return None
    s = str(s).replace(',', '').strip()
    if s in ('', '--', '---', 'X', 'x', 'N/A', '-'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_iso(s):
    s = str(s).strip()
    m = re.match(r'^(\d{2,4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})', s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r'^(\d{4})(\d{2})(\d{2})$', s) or re.match(r'^(\d{3})(\d{2})(\d{2})$', s)
        if not m:
            return None
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if y < 1911:
        y += 1911
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _ctx():
    c = ssl.create_default_context()
    if hasattr(ssl, 'VERIFY_X509_STRICT'):
        c.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return c


CTX = _ctx()


def http_json(url):
    wait = GAP - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:          # HTTPError 是 URLError 的子類，要放最前面
        raise FetchError('%s: %s' % (url, e), 'site')
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise FetchError('%s: %s' % (url, e), 'network')
    except ValueError as e:
        raise FetchError('%s: %s' % (url, e), 'site')
    finally:
        _last[0] = time.time()
    try:
        return json.loads(raw.decode('utf-8-sig'))
    except ValueError:
        raise FetchError('non-JSON response: ' + url, 'site')


def retrying(fn):
    for i in range(len(RETRY) + 1):
        try:
            return fn()
        except FetchError as e:
            if i == len(RETRY):
                raise
            log('連線失敗，%d 秒後重試（%s）' % (RETRY[i], str(e)[:120]))
            time.sleep(RETRY[i])


def col(fields, *names):
    fs = [re.sub(r'<.*?>', '', str(f)).strip() for f in fields]
    for n in names:
        if n in fs:
            return fs.index(n)
    raise Structure('missing column %s in %s' % (names, fs))


def keep(code, name):
    return bool(KEEP.match(code)) and not name.strip().upper().endswith('-DR')


# ---------- 每日行情 ----------
def parse_twse(d):
    stat = str(d.get('stat', ''))
    if NODATA in stat:
        return None
    if not stat.startswith('OK'):
        raise FetchError('TWSE stat=' + stat)
    tab = None
    for t in d.get('tables') or []:
        f = [str(x).strip() for x in t.get('fields') or []]
        if '證券代號' in f and '收盤價' in f:
            tab = (t['fields'], t.get('data') or [])
            break
    if tab is None and d.get('fields9') and '證券代號' in [str(x).strip() for x in d['fields9']]:
        tab = (d['fields9'], d.get('data9') or [])
    if tab is None:
        raise Structure('TWSE: 找不到含證券代號與收盤價的表')
    f, data = tab
    ic, inm = col(f, '證券代號'), col(f, '證券名稱')
    iv, io, ih, il, icl = col(f, '成交股數'), col(f, '開盤價'), col(f, '最高價'), col(f, '最低價'), col(f, '收盤價')
    rows = []
    for r in data:
        code, name = str(r[ic]).strip(), str(r[inm]).strip()
        if not keep(code, name):
            continue
        o, h, l, c, v = num(r[io]), num(r[ih]), num(r[il]), num(r[icl]), num(r[iv])
        if None in (o, h, l, c, v) or v <= 0 or c <= 0:
            continue
        rows.append([code, name, o, h, l, c, int(v)])
    if not rows:
        raise Structure('TWSE: 表存在但沒有可用資料列')
    return rows


def parse_tpex(d, iso):
    if str(d.get('stat', '')).lower() != 'ok':
        raise FetchError('TPEx stat=%s' % d.get('stat'))
    tabs = d.get('tables') or []
    if not tabs:
        raise Structure('TPEx: 沒有 tables')
    t = tabs[0]
    data = t.get('data') or []
    if not data:
        return None
    if t.get('date') and to_iso(t['date']) != iso:
        raise Structure('TPEx 回傳日期 %s 與要求 %s 不符' % (t.get('date'), iso))
    f = t['fields']
    ic, inm = col(f, '代號'), col(f, '名稱')
    iv, io, ih, il, icl = col(f, '成交股數'), col(f, '開盤'), col(f, '最高'), col(f, '最低'), col(f, '收盤')
    rows = []
    for r in data:
        code, name = str(r[ic]).strip(), str(r[inm]).strip()
        if not keep(code, name):
            continue
        o, h, l, c, v = num(r[io]), num(r[ih]), num(r[il]), num(r[icl]), num(r[iv])
        if None in (o, h, l, c, v) or v <= 0 or c <= 0:
            continue
        rows.append([code, name, o, h, l, c, int(v)])
    if not rows:
        raise Structure('TPEx: 表存在但沒有可用資料列')
    return rows


def fetch_twse(iso):
    url = TWSE_DAY.format(ymd=iso.replace('-', ''))
    return retrying(lambda: parse_twse(http_json(url)))


def fetch_tpex(iso):
    url = TPEX_DAY.format(slash=iso.replace('-', '%2F'))
    return retrying(lambda: parse_tpex(http_json(url), iso))


# ---------- cache ----------
def cpath(mkt, iso):
    return os.path.join(CACHE, mkt, iso.replace('-', '') + '.json')


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    os.replace(tmp, path)


def write_day(mkt, iso, rows):
    obj = {'date': iso, 'rows': rows or []}
    if rows is None:
        obj['holiday'] = True
    write_atomic(cpath(mkt, iso), json.dumps(obj, ensure_ascii=False, separators=(',', ':')))


def read_day(mkt, iso):
    try:
        with open(cpath(mkt, iso), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fetch_day(iso, today):
    """回傳 ok / skipped / holiday / empty / tpex-missing"""
    tw = read_day('twse', iso)
    new = False
    if tw is None:
        rows = fetch_twse(iso)
        if rows is None:
            if iso < today:
                write_day('twse', iso, None)
                write_day('tpex', iso, None)
                return 'holiday'
            return 'empty'
        write_day('twse', iso, rows)
        new = True
    elif tw.get('holiday'):
        if read_day('tpex', iso) is None:
            write_day('tpex', iso, None)
        return 'skipped'
    if read_day('tpex', iso) is None:
        rows = fetch_tpex(iso)
        if rows is None:
            return 'tpex-missing'
        write_day('tpex', iso, rows)
        new = True
    return 'ok' if new else 'skipped'


def weekdays(a, b):
    d = a
    while d <= b:
        if d.weekday() < 5:
            yield d.isoformat()
        d += dt.timedelta(days=1)


# ---------- 除權息 ----------
def exr_path(mkt, ym):
    return os.path.join(CACHE, 'exright', '%s_%s.json' % (mkt, ym))


def parse_ex(d, mkt):
    if mkt == 'twse':
        if NODATA in str(d.get('stat', '')):
            return []
        if not str(d.get('stat', '')).startswith('OK'):
            raise FetchError('TWT49U stat=' + str(d.get('stat')))
        f, data = d['fields'], d.get('data') or []
        idt, ic, ip, ir = col(f, '資料日期'), col(f, '股票代號'), col(f, '除權息前收盤價'), col(f, '除權息參考價')
    else:
        if str(d.get('stat', '')).lower() != 'ok':
            raise FetchError('exDailyQ stat=' + str(d.get('stat')))
        tabs = d.get('tables') or []
        if not tabs:
            raise Structure('exDailyQ: 沒有 tables')
        f, data = tabs[0]['fields'], tabs[0].get('data') or []
        idt, ic, ip, ir = col(f, '除權息日期'), col(f, '代號'), col(f, '除權息前收盤價'), col(f, '除權息參考價')
    out = []
    for r in data:
        iso = to_iso(r[idt])
        if not iso:
            raise Structure('除權息日期格式不認得: %r' % r[idt])
        out.append([str(r[ic]).strip(), iso, num(r[ip]), num(r[ir])])
    return out


def sync_exright(start, end):
    """抓 start~end 涵蓋月份的除權息；當月與上月每次都重抓"""
    today = dt.date.today()
    cur = today.strftime('%Y%m')
    prev = (today.replace(day=1) - dt.timedelta(days=1)).strftime('%Y%m')
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        ym = '%04d%02d' % (y, m)
        first = dt.date(y, m, 1)
        last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        last = min(last, today)
        if first <= today:
            for mkt in ('twse', 'tpex'):
                p = exr_path(mkt, ym)
                if os.path.exists(p) and ym not in (cur, prev):
                    continue
                if mkt == 'twse':
                    url = TWSE_EX.format(s=first.strftime('%Y%m%d'), e=last.strftime('%Y%m%d'))
                else:
                    url = TPEX_EX.format(s=first.strftime('%Y/%m/%d').replace('/', '%2F'),
                                         e=last.strftime('%Y/%m/%d').replace('/', '%2F'))
                ev = retrying(lambda: parse_ex(http_json(url), mkt))
                write_atomic(p, json.dumps(ev, ensure_ascii=False, separators=(',', ':')))
                log('exright %s %s: %d events' % (mkt, ym, len(ev)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def load_events():
    ev, seen = {}, set()
    for p in sorted(glob.glob(os.path.join(CACHE, 'exright', '*.json'))):
        with open(p, encoding='utf-8') as f:
            for code, iso, prev, ref in json.load(f):
                if not prev or not ref or prev <= 0:
                    continue
                fac = ref / prev
                if not 0 < fac <= 1.05:
                    continue
                if (code, iso) in seen:
                    continue
                seen.add((code, iso))
                ev.setdefault(code, []).append((iso, fac))
    for v in ev.values():
        v.sort(reverse=True)
    return ev


# ---------- 重建 data/*.csv ----------
def build():
    bars, dates = {}, set()
    for mkt in ('twse', 'tpex'):
        for p in sorted(glob.glob(os.path.join(CACHE, mkt, '*.json'))):
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
            if d.get('holiday'):
                continue
            dates.add(d['date'])
            for code, name, o, h, l, c, v in d['rows']:
                bars.setdefault(code, []).append((d['date'], o, h, l, c, v, name))
    if not dates:
        raise UserError(E_NEED_DATA, *MSG_NEED)
    latest = max(dates)
    ev = load_events()
    os.makedirs(DATA, exist_ok=True)
    written = set()
    for code, bs in bars.items():
        bs.sort()
        if bs[-1][0] != latest:
            continue
        name = bs[-1][6]
        e = ev.get(code, [])
        out, k, cum = [], 0, 1.0
        # 由新往舊走，遇到日期晚於該根 K 的除權息事件就累乘
        for b in reversed(bs):
            while k < len(e) and e[k][0] > b[0]:
                cum *= e[k][1]
                k += 1
            out.append([b[0], round(b[1] * cum, 4), round(b[2] * cum, 4), round(b[3] * cum, 4),
                        round(b[4] * cum, 4), b[5], b[4], name])
        out.reverse()
        path = os.path.join(DATA, code + '.csv')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'name'])
            w.writerows(out)
        os.replace(tmp, path)
        written.add(code)
    removed = 0
    for p in glob.glob(os.path.join(DATA, '*.csv')):
        code = os.path.splitext(os.path.basename(p))[0]
        if KEEP.match(code) and code not in written:
            os.remove(p)
            removed += 1
    write_atomic(os.path.join(CACHE, 'latest.txt'), latest.replace('-', ''))
    log('build: %d stocks, latest %s, removed stale %d' % (len(written), latest, removed))


# ---------- 指令 ----------
def parse_ymd(s):
    return dt.datetime.strptime(s, '%Y%m%d').date()


def _site_cause(site_days):
    days = '，沒抓到：' + fmt_days(site_days) if site_days else ''
    return MSG_SITE[0].format(days=days), MSG_SITE[1]


def _incomplete(site_days, nodata):
    """資料不完整時丟出對應的 UserError；都沒問題就什麼都不做"""
    if site_days:
        raise UserError(E_SITE, *_site_cause(site_days))
    if nodata:
        raise UserError(E_NODATA, MSG_NODATA[0].format(days=fmt_days(nodata)), MSG_NODATA[1])


def cmd_backfill(a):
    today = dt.date.today()
    end = parse_ymd(a.end) if a.end else today
    start = parse_ymd(a.start) if a.start else today - dt.timedelta(days=430)
    days = list(weekdays(start, end))
    cnt, kinds, site_days, nodata = {}, set(), [], []
    log('backfill %s ~ %s, %d weekdays' % (start, end, len(days)))
    for i, iso in enumerate(days, 1):
        try:
            st = fetch_day(iso, today.isoformat())
        except FetchError as e:
            st = 'error'
            kinds.add(e.kind)
            if e.kind != 'network':
                site_days.append(iso)
            log('ERROR', iso, str(e)[:200])
        cnt[st] = cnt.get(st, 0) + 1
        if st not in ('skipped',):
            log('[%d/%d] %s %s' % (i, len(days), iso, st))
        if st == 'tpex-missing':
            nodata.append(iso)
        if 'network' in kinds:
            break
    if 'network' in kinds:
        raise UserError(E_NETWORK, *MSG_NETWORK)
    sync_exright(start, end)
    build()
    log('summary', cnt)
    _incomplete(site_days, nodata)
    return 0


def cmd_daily(a):
    today = dt.date.today()
    cached = [os.path.basename(p)[:8] for m in ('twse', 'tpex') for p in glob.glob(os.path.join(CACHE, m, '*.json'))]
    if not cached:
        raise UserError(E_NEED_DATA, *MSG_NEED)
    last = parse_ymd(max(cached))
    start = last - dt.timedelta(days=14)
    deadline = dt.datetime.combine(today, dt.time(16, 0))
    open_t = dt.datetime.combine(today, dt.time(13, 30))
    kinds, site_days, nodata = set(), [], []
    for iso in weekdays(start, today):
        while True:
            try:
                st = fetch_day(iso, today.isoformat())
            except FetchError as e:
                st = 'error'
                kinds.add(e.kind)
                if e.kind != 'network':
                    site_days.append(iso)
                log('ERROR', iso, str(e)[:200])
            if iso != today.isoformat() or st not in ('empty', 'tpex-missing'):
                break
            now = dt.datetime.now()
            if now < open_t:
                log('今天還沒收盤或資料還沒公布（建議 14:30 之後再執行），這次先用前一個交易日的資料。')
                break
            if now >= deadline:
                break
            log('官方還沒公布 %s 的資料（%s），10 分鐘後自動再試，最晚等到 16:00；不想等可以先關掉視窗，晚點再執行。' % (iso, st))
            time.sleep(600)
        if 'network' in kinds:
            break
        if st != 'skipped':
            log(iso, st)
        if st == 'empty':
            if dt.datetime.now() >= deadline:
                log('官方今天沒有資料，視為休市。')
        elif st == 'tpex-missing':
            nodata.append(iso)
    if 'network' in kinds:
        raise UserError(E_NETWORK, *MSG_NETWORK)
    if site_days or nodata:
        log('資料不完整，這次不重建 data/。')
        _incomplete(site_days, nodata)
    sync_exright(start, today)
    build()
    return 0


def cmd_build(a):
    build()
    return 0


def main():
    ap = argparse.ArgumentParser(description='抓台股全市場日 K，重建 data/*.csv')
    sp = ap.add_subparsers(dest='cmd', required=True)
    b = sp.add_parser('backfill', help='補歷史（可續跑）')
    b.add_argument('--start')
    b.add_argument('--end')
    sp.add_parser('daily', help='每日增量')
    sp.add_parser('build', help='用 cache 重建 data/*.csv（離線）')
    a = ap.parse_args()
    try:
        sys.exit({'backfill': cmd_backfill, 'daily': cmd_daily, 'build': cmd_build}[a.cmd](a))
    except UserError as e:
        fail(e.code, e.cause, e.todo)
    except Structure as e:
        log('FAIL:', e)
        fail(E_SITE, *MSG_FORMAT)
    except FetchError as e:
        log('FAIL:', e)
        if e.kind == 'network':
            fail(E_NETWORK, *MSG_NETWORK)
        fail(E_SITE, *_site_cause([]))
    except Exception as e:
        text = traceback.format_exc()
        print(text, flush=True, end='')
        _tee(text.rstrip())
        fail(1, '程式發生未預期的錯誤：%s: %s' % (type(e).__name__, e),
             '請把上面的錯誤訊息（或 logs 資料夾裡今天的紀錄檔）貼到 GitHub 專案的 Issues 回報。')


if __name__ == '__main__':
    main()
