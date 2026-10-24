"""易用性測試：設定檔、自選股清單、HTML／CSV 輸出、錯誤代碼、.bat 檔格式。只用 unittest，不連網。

執行：python -m unittest discover -s tests -v
"""
import argparse, contextlib, csv, datetime as dt, io, json, os, re, subprocess, sys, tempfile, unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fetch_data, screener  # noqa: E402


def write(path, data, enc='utf-8'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data if isinstance(data, bytes) else data.encode(enc))
    return path


def readb(path):
    with open(path, 'rb') as f:
        return f.read()


def quiet(fn, *a, **k):
    """執行 fn，回傳 (結果, stderr 文字)"""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = fn(*a, **k)
    return out, err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix='測試 資料夾 (x86) ')
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)


class TestThresholds(unittest.TestCase):
    def test_P_unchanged(self):
        self.assertEqual(screener.P, dict(
            wk_ma=(13, 26), vol_mult=1.3, vol_days=5, slope_days=5, cross_within=3, box_days=20, box_range=0.12,
            squeeze_lookback=120, squeeze_pct=0.25, long_red=0.03, ride_days=20, ride_ratio=0.8, pull_near=0.02,
            pull_vol=0.8, expand=1.2))


class TestConfig(TempCase):
    def cfg(self, text, enc='utf-8', name='設定.txt'):
        return write(os.path.join(self.tmp, name), text, enc)

    def test_basic_variants(self):
        text = ('\ufeff# 註解\r\n\r\n  capital = 1,000,000  \r\n;分號註解\r\nrisk = 3%  # c\r\n'
                'MODE=watchlist\r\noutput_dir="報告 輸出"\r\nwatchlist=my.csv\r\n')
        c, err = quiet(screener.load_config, self.cfg(text.encode('utf-8')))
        self.assertEqual(c['capital'], 1000000)
        self.assertEqual(c['risk'], 0.03)
        self.assertEqual(c['mode'], 'watchlist')
        self.assertEqual(c['output_dir'], os.path.join(self.tmp, '報告 輸出'))
        self.assertEqual(c['watchlist'], os.path.join(self.tmp, 'my.csv'))
        self.assertEqual(err, '')

    def test_defaults_and_fullwidth_equals(self):
        c, _ = quiet(screener.load_config, self.cfg('capital＝500000\n'))
        self.assertEqual((c['capital'], c['risk'], c['mode']), (500000, 0.02, 'full'))
        self.assertEqual(c['output_dir'], os.path.join(self.tmp, '結果'))

    def test_unknown_key_and_garbage_line_warn(self):
        c, err = quiet(screener.load_config, self.cfg('foo=1\nthis is junk\ncapital=100\n'))
        self.assertEqual(c['capital'], 100)
        self.assertIn('foo', err)
        self.assertIn('第 2 行看不懂', err)
        self.assertIn('capital', err)

    def test_duplicate_last_wins_and_empty_unset(self):
        c, _ = quiet(screener.load_config, self.cfg('capital=100\ncapital=200\nrisk=4%\nrisk=\n'))
        self.assertEqual(c['capital'], 200)
        self.assertEqual(c['risk'], 0.02)

    def test_cp950_ok_with_warning(self):
        c, err = quiet(screener.load_config, self.cfg('# 中文註解 台積電\ncapital=300000\n'.encode('cp950')))
        self.assertEqual(c['capital'], 300000)
        self.assertIn('Big5', err)

    def test_utf16_is_encoding_error(self):
        p = self.cfg('capital=1\n'.encode('utf-16'))
        with self.assertRaises(screener.UserError) as cm:
            quiet(screener.load_config, p)
        self.assertEqual(cm.exception.code, 8)
        self.assertIn('UTF-8', cm.exception.cause + cm.exception.todo)

    def test_bad_risk_reports_line(self):
        p = self.cfg('capital=1\nrisk=8%\n')
        with self.assertRaises(screener.UserError) as cm:
            screener.load_config(p)
        self.assertEqual(cm.exception.code, 9)
        self.assertIn('第 2 行', cm.exception.cause)
        self.assertIn('設定.範例.txt', cm.exception.todo)

    def test_missing_config(self):
        with self.assertRaises(screener.UserError) as cm:
            screener.load_config(os.path.join(self.tmp, 'nope.txt'))
        self.assertEqual(cm.exception.code, 9)

    def test_parse_capital(self):
        pc = screener.parse_capital
        self.assertEqual(pc('1,000,000'), 1000000)
        self.assertEqual(pc(' 1000000 '), 1000000)
        self.assertIsNone(pc(''))
        self.assertIsNone(pc('0'))
        for bad in ('abc', '"', '100萬', '-5'):
            with self.assertRaises(ValueError, msg=bad):
                pc(bad)

    def test_parse_risk(self):
        pr = screener.parse_risk
        for v, want in (('', 0.02), ('2', 0.02), ('2%', 0.02), ('0.03', 0.03), ('2.5％', 0.025), ('5', 0.05)):
            self.assertEqual(pr(v), want, v)
        for bad in ('1', '8', '1%', 'abc', '!PATH!'):
            with self.assertRaises(ValueError, msg=bad) as cm:
                pr(bad)
            self.assertNotIn('unsupported', str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            pr('8')
        self.assertIn('2% 到 5%', str(cm.exception))

    def test_parse_mode(self):
        pm = screener.parse_mode
        for v in ('', 'full', 'ALL', '1', '全市場'):
            self.assertEqual(pm(v), 'full')
        for v in ('watchlist', 'list', '2', '自選股', '自選'):
            self.assertEqual(pm(v), 'watchlist')
        with self.assertRaises(ValueError):
            pm('3')

    def test_write_config_preserves_other_lines(self):
        p = self.cfg('# 我的設定\r\ncapital=1\r\nrisk=2%\r\nmode=full\r\noutput_dir=D:\\選股結果\r\n')
        screener.write_config(p, 2500000, 0.03, 'watchlist')
        raw = readb(p)
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(raw.count(b'\r\n'), raw.count(b'\n'))
        text = raw.decode('utf-8')
        self.assertIn('# 我的設定', text)
        self.assertIn('output_dir=D:\\選股結果', text)
        c, _ = quiet(screener.load_config, p)
        self.assertEqual((c['capital'], c['risk'], c['mode']), (2500000, 0.03, 'watchlist'))

    def test_write_config_from_template_when_missing(self):
        p = os.path.join(self.tmp, '設定.txt')
        screener.write_config(p, None, 0.02, 'full')
        c, _ = quiet(screener.load_config, p)
        self.assertEqual((c['capital'], c['risk'], c['mode']), (None, 0.02, 'full'))
        self.assertIn('#', readb(p).decode('utf-8'))


class TestWatchlist(TempCase):
    def lst(self, data, enc='utf-8', name='自選股.csv'):
        return write(os.path.join(self.tmp, name), data, enc)

    def code_of(self, fn):
        with self.assertRaises(screener.UserError) as cm:
            quiet(fn)
        return cm.exception

    def test_normal_and_bom(self):
        p = self.lst('\ufeff代號,名稱,備註\r\n2330,台積電,\r\n0050,元大台灣50,x\r\n')
        wl, _ = quiet(screener.load_list, p)
        self.assertEqual(wl, {'2330': '台積電', '0050': '元大台灣50'})

    def test_cp950(self):
        p = self.lst('代號,名稱\n2330,台積電\n'.encode('cp950'))
        wl, err = quiet(screener.load_list, p)
        self.assertEqual(wl, {'2330': '台積電'})
        self.assertIn('Big5', err)

    def test_excel_text_formula(self):
        p = self.lst('代號,名稱\n="0050",元大\n"=""0056""",高股息\n')
        wl, _ = quiet(screener.load_list, p)
        self.assertEqual(sorted(wl), ['0050', '0056'])

    def test_none_path(self):
        self.assertIsNone(screener.load_list(None))

    def test_bad_header(self):
        e = self.code_of(lambda: screener.load_list(self.lst('code,name\n2330,x\n')))
        self.assertEqual(e.code, 7)
        self.assertIn('代號', e.cause)

    def test_empty_list(self):
        e = self.code_of(lambda: screener.load_list(self.lst('代號,名稱\n')))
        self.assertEqual(e.code, 7)

    def test_missing(self):
        e = self.code_of(lambda: screener.load_list(os.path.join(self.tmp, '自選股.csv')))
        self.assertEqual(e.code, 7)
        self.assertIn('自選股.範例.csv', e.todo)

    def test_missing_but_dot_csv_dot_txt_exists(self):
        self.lst('代號\n2330\n', name='自選股.csv.txt')
        e = self.code_of(lambda: screener.load_list(os.path.join(self.tmp, '自選股.csv')))
        self.assertEqual(e.code, 7)
        self.assertIn('自選股.csv.txt', e.todo)

    def test_utf16(self):
        e = self.code_of(lambda: screener.load_list(self.lst('代號,名稱\n2330,x\n'.encode('utf-16'))))
        self.assertEqual(e.code, 8)


def fake_rec(code, name, close, chg, vr, typ=None, stop=None, watch=False, stages=None, date='2026-10-22'):
    st = {0: True, 1: True, 2: bool(watch or typ), 3: bool(watch or typ), 'A': typ == '突破型', 'B': typ == '回測型'}
    st.update(stages or {})
    r = dict(code=code, name=name, date=date, close=close, chg=chg, stages=st, notes=[], vol_ratio=vr,
             osc=[-0.5, 0.7], watch=watch)
    if typ:
        r.update(type=typ, stop=stop, setup='近 20 日沿月線 & <測試>', stop_why='跌破 MA20 <b>x</b>')
    return r


def fake_results(capital=1000000, risk=0.02):
    res = [fake_rec('1101', '台泥', 120.0, 3.1, 2.4, '突破型', 117.0),
           fake_rec('2330', '台積電', 2385.0, -0.8, 0.6, '回測型', 2300.0),
           fake_rec('00646', '元大S&P500', 78.95, -0.32, 0.6, '回測型', 77.76),
           fake_rec('6874', '寬停損', 100.0, 0.0, 0.7, '回測型', 85.0),
           fake_rec('2454', '聯發科', 1200.0, 1.5, 1.8, watch=True),
           fake_rec('1216', '統一', 70.0, 0.3, 1.0, stages={0: False}),
           dict(code='9999', name='新股', error='資料只有 10 天，至少要 80 天')]
    for r in res:
        screener.size(r, capital, risk)
    return res


def meta_for(**k):
    m = dict(capital=1000000, risk=0.02, list_path=None, list_count=0, missing=[], today='2026-10-22',
             generated='2026-10-22 15:00')
    m.update(k)
    return m


class TestRender(unittest.TestCase):
    def test_html_content(self):
        res = fake_results()
        h = screener.render_html(res, False, meta_for())
        self.assertIn('lang="zh-Hant"', h)
        self.assertIn('charset="utf-8"', h)
        self.assertIn('元大S&amp;P500', h)
        self.assertNotIn('<測試>', h)
        self.assertNotIn('<b>x</b>', h)
        for bad in ('http://', 'https://', '<link', 'src=', '@import', 'url('):
            self.assertNotIn(bad, h)
        for cls in ('sortable', 't-break', 't-pull', 'p-cap', 'p-odd', 'd-wide', 'd-ok'):
            self.assertIn(cls, h)
        self.assertIn('2026-10-22（星期四）', h)
        self.assertIn('掃描 7 檔（資料有效 6 檔）', h)
        self.assertIn('部位（風險 2%）', h)
        self.assertIn('部位（5%）', h)
        self.assertIn('觀察名單', h)
        self.assertNotIn('資料日期不是今天', h)
        self.assertIn('資料只有 10 天', h)

    def test_list_mode_titles_and_missing(self):
        res = fake_results()
        h = screener.render_html(res, True, meta_for(list_path='C:\\x\\自選股.csv', list_count=9, missing=['50', '9999']))
        self.assertIn('自選股：過第 2、3 關但型態未到', h)
        self.assertIn('自選股清單（自選股.csv，9 檔）', h)
        self.assertIn('找不到資料：50、9999', h)
        self.assertNotIn('C:\\x', h)

    def test_no_capital_and_stale_date_and_empty(self):
        res = [fake_rec('1216', '統一', 70.0, 0.3, 1.0, stages={0: False})]
        h = screener.render_html(res, False, meta_for(capital=None, today='2026-10-25'))
        self.assertIn('未設定總資金，不試算部位', h)
        self.assertNotIn('部位（', h)
        self.assertIn('資料日期不是今天', h)
        self.assertIn('今天沒有標的同時通過所有關卡。', h)

    def test_risk_5_has_single_position_column(self):
        h = screener.render_html(fake_results(risk=0.05), False, meta_for(risk=0.05))
        self.assertIn('部位（風險 5%）', h)
        self.assertNotIn('部位（5%）', h)

    def test_render_does_not_mutate(self):
        res = fake_results()
        before = json.dumps(res, ensure_ascii=False, default=str)
        screener.render_html(res, True, meta_for())
        with tempfile.TemporaryDirectory() as d:
            screener.write_csv(os.path.join(d, 'a.csv'), res, 1000000)
        self.assertEqual(before, json.dumps(res, ensure_ascii=False, default=str))


class TestCsv(TempCase):
    def test_csv(self):
        p = os.path.join(self.tmp, 'o.csv')
        res = fake_results()
        res.append(fake_rec('0050', '元大台灣50', 150.0, 0.1, 1.0))
        screener.write_csv(p, res, 1000000)
        raw = readb(p)
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline='')))
        self.assertEqual(rows[0], screener.CSV_HEAD)
        self.assertEqual(rows[0][:5], ['代號', '名稱', '資料日期', '型態', '收盤'])
        self.assertEqual(rows[0][-3:], ['說明', '停損依據', '備註'])
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))
        codes = [r[0] for r in rows[1:]]
        self.assertIn('="0050"', codes)
        self.assertEqual(codes[-1], '="9999"')              # 錯誤列排最後
        self.assertIn('資料只有 10 天', rows[-1][-1])
        self.assertEqual([bool(r[3]) for r in rows[1:]], [True] * 4 + [False] * 4)     # 候選在前，其餘在後，錯誤列最後
        first = rows[1]
        self.assertIn(first[13], ('是', '否'))
        self.assertEqual(sorted({c for r in rows[1:-1] for c in r[13:19]}), ['否', '是'])


def make_data(d, codes=('1101', '2330'), n=150):
    os.makedirs(d, exist_ok=True)
    start = dt.date(2026, 3, 2)
    days = []
    while len(days) < n:
        if start.weekday() < 5:
            days.append(start.isoformat())
        start += dt.timedelta(days=1)
    for k, code in enumerate(codes):
        with open(os.path.join(d, code + '.csv'), 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'name'])
            for i, day in enumerate(days):
                c = round(100 + k * 50 + i * 0.3 + (i % 7) * 0.4, 2)
                w.writerow([day, c - 0.2, c + 0.8, c - 0.9, c, 1000000 + (i % 5) * 50000, c, '測試' + code])
    return days[-1].replace('-', '')


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith('TWSTOCK_') and k not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
    env.update(extra)
    return env


class TestScanCli(TempCase):
    def run_cli(self, *args, **envx):
        env = clean_env(PYTHONUTF8='1')
        env.update(envx)
        return subprocess.run([sys.executable, os.path.join(ROOT, 'screener.py')] + list(args), cwd=self.tmp, env=env,
                              capture_output=True, timeout=120)

    def test_out_dir_writes_five_files(self):
        d = os.path.join(self.tmp, 'data')
        tag = make_data(d)
        out = os.path.join(self.tmp, '結果 輸出')
        r = self.run_cli('scan', '--data', d, '--out-dir', out, '--capital', '1000000')
        self.assertEqual(r.returncode, 0, r.stderr.decode('utf-8', 'replace'))
        self.assertEqual(sorted(os.listdir(out)), sorted(f'盤後選股_{tag}.{e}' for e in ('html', 'csv', 'md', 'txt', 'json')))
        self.assertIn('選股完成', r.stderr.decode('utf-8'))

    def test_missing_data(self):
        r = self.run_cli('scan', '--data', os.path.join(self.tmp, 'nope'))
        self.assertEqual(r.returncode, 6)
        self.assertIn('原因：', r.stderr.decode('utf-8'))

    def test_bad_config(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        cfg = write(os.path.join(self.tmp, '設定.txt'), 'risk=8%\n')
        r = self.run_cli('scan', '--data', d, '--config', cfg)
        self.assertEqual(r.returncode, 9)
        e = r.stderr.decode('utf-8')
        self.assertIn('第 1 行', e)

    def test_bad_watchlist_header(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        lst = write(os.path.join(self.tmp, 'l.csv'), 'code,name\n1101,x\n')
        r = self.run_cli('scan', '--data', d, '--list', lst)
        self.assertEqual(r.returncode, 7)
        self.assertIn('代號', r.stderr.decode('utf-8'))

    def test_watchlist_nothing_found(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        lst = write(os.path.join(self.tmp, 'l.csv'), '代號,名稱\n50,x\n')
        r = self.run_cli('scan', '--data', d, '--list', lst)
        self.assertEqual(r.returncode, 7)
        self.assertIn('50', r.stderr.decode('utf-8'))

    def test_watchlist_partial_notice(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        lst = write(os.path.join(self.tmp, 'l.csv'), '代號,名稱\n1101,x\n50,y\n')
        r = self.run_cli('scan', '--data', d, '--list', lst, '--out', os.path.join(self.tmp, 'o.md'))
        self.assertEqual(r.returncode, 0)
        self.assertIn('找不到資料：50', r.stderr.decode('utf-8'))

    def test_ascii_console_without_out(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        env = clean_env(PYTHONIOENCODING='ascii')
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'screener.py'), 'scan', '--data', d], cwd=self.tmp,
                           env=env, capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 8)
        self.assertNotIn('✓'.encode('utf-8'), r.stderr)

    def test_resolve_scan_options(self):
        cfgp = write(os.path.join(self.tmp, '設定.txt'), 'capital=500000\nrisk=3%\nmode=watchlist\nwatchlist=w.csv\n')
        ns = lambda **k: argparse.Namespace(**dict(dict(config=None, capital=None, risk=None, list=None, out_dir=None), **k))
        o = screener.resolve_scan_options(ns())
        self.assertEqual((o['capital'], o['risk'], o['list'], o['out_dir']), (None, 0.02, None, None))
        o = screener.resolve_scan_options(ns(config=cfgp))
        self.assertEqual((o['capital'], o['risk']), (500000, 0.03))
        self.assertEqual(o['list'], os.path.join(self.tmp, 'w.csv'))
        self.assertEqual(o['out_dir'], os.path.join(self.tmp, '結果'))
        o = screener.resolve_scan_options(ns(config=cfgp, capital=1.0, risk=0.04, list='x.csv', out_dir='o'))
        self.assertEqual((o['capital'], o['risk'], o['list'], o['out_dir']), (1.0, 0.04, 'x.csv', 'o'))


class TestFetch(TempCase):
    def setUp(self):
        super().setUp()
        for target, val in (('GAP', 0), ('RETRY', ()), ('CACHE', os.path.join(self.tmp, 'cache')),
                            ('DATA', os.path.join(self.tmp, 'data'))):
            p = mock.patch.object(fetch_data, target, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(fetch_data.time, 'sleep', side_effect=AssertionError('不該等待'))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(fetch_data, 'log')
        p.start()
        self.addCleanup(p.stop)

    ROW = ['1101', '台泥', 10.0, 11.0, 9.0, 10.5, 1000]

    def preload(self):
        """快取放一個舊日期；今天是平日的話連今天也放，避免觸發 600 秒等待"""
        today = dt.date.today()
        old = today - dt.timedelta(days=5)
        while old.weekday() >= 5:
            old -= dt.timedelta(days=1)
        days = [old] + ([today] if today.weekday() < 5 else [])
        for d in days:
            for m in ('twse', 'tpex'):
                fetch_data.write_day(m, d.isoformat(), [self.ROW])

    def test_http_json_kinds(self):
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=urllib.error.URLError('x')):
            with self.assertRaises(fetch_data.FetchError) as cm:
                fetch_data.http_json('https://example.invalid/')
        self.assertEqual(cm.exception.kind, 'network')
        err = urllib.error.HTTPError('https://example.invalid/', 403, 'forbidden', {}, None)
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=err):
            with self.assertRaises(fetch_data.FetchError) as cm:
                fetch_data.http_json('https://example.invalid/')
        self.assertEqual(cm.exception.kind, 'site')

    def test_daily_network_down(self):
        self.preload()
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=urllib.error.URLError('x')) as m:
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.cmd_daily(None)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(m.call_count, 1)

    def test_daily_empty_cache(self):
        with self.assertRaises(fetch_data.UserError) as cm:
            fetch_data.cmd_daily(None)
        self.assertEqual(cm.exception.code, 6)
        self.assertIn('setup.bat', cm.exception.todo)

    def test_daily_tpex_missing(self):
        self.preload()
        with mock.patch.object(fetch_data, 'fetch_twse', return_value=[self.ROW]), \
                mock.patch.object(fetch_data, 'fetch_tpex', return_value=None):
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.cmd_daily(None)
        self.assertEqual(cm.exception.code, 5)

    def test_backfill_network_down(self):
        a = argparse.Namespace(start='20260302', end='20260306')
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=urllib.error.URLError('x')) as m:
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.cmd_backfill(a)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(m.call_count, 1)

    def test_build_empty_cache(self):
        with self.assertRaises(fetch_data.UserError) as cm:
            fetch_data.build()
        self.assertEqual(cm.exception.code, 6)


MARK = b'rem ===== messages below'


class TestBatFiles(unittest.TestCase):
    def raw(self, name):
        with open(os.path.join(ROOT, name), 'rb') as f:
            return f.read()

    def test_format(self):
        for name in ('run.bat', 'setup.bat', 'run_daily.bat'):
            b = self.raw(name)
            self.assertFalse(b.startswith(b'\xef\xbb\xbf'), name)
            self.assertGreater(b.count(b'\n'), 3, name)
            self.assertEqual(b.count(b'\r\n'), b.count(b'\n'), name + ' 要全是 CRLF')
            self.assertEqual(b.count(b'\r'), b.count(b'\r\n'), name)
        for name in ('run.bat', 'setup.bat'):
            b = self.raw(name)
            b[:b.index(b'chcp 65001')].decode('ascii')
            # cmd 讀含中文的 UTF-8 批次檔會偶爾切歪行：會被執行的行一律 ASCII，中文只放在檔尾資料區
            b[:b.index(MARK)].decode('ascii')

    def test_every_message_key_exists(self):
        for name in ('run.bat', 'setup.bat'):
            t = self.raw(name).decode('utf-8')
            code, data = t.split(MARK.decode('ascii'))
            keys = set(re.findall(r'^::([A-Z0-9_]+)\|', data, re.M))
            used = set(re.findall(r'call :(?:msg|msgl|msgv)\s+([A-Z0-9_]+)', code))
            used |= set(re.findall(r'call :ask_(?:yn|12)\s+([A-Z0-9_]+)', code))
            self.assertEqual(used - keys, set(), name)

    def test_run_daily_wrapper(self):
        t = self.raw('run_daily.bat').decode('ascii')
        for s in ('TWSTOCK_NOPAUSE=1', 'TWSTOCK_NO_OPEN=1', 'call "%~dp0run.bat" %*', 'exit /b %ERRORLEVEL%'):
            self.assertIn(s, t)

    def findpy(self, name):
        t = self.raw(name).decode('utf-8')
        return t[t.index('\r\n:findpy\r\n'):t.index('\r\n:nopython\r\n')]

    def test_findpy_identical(self):
        a, b = self.findpy('run.bat'), self.findpy('setup.bat')
        self.assertEqual(a, b)
        self.assertIn('sys.version_info>=(3,9)', a)

    def test_no_paren_blocks(self):
        for name in ('run.bat', 'setup.bat', 'run_daily.bat'):
            for i, line in enumerate(self.raw(name).decode('utf-8').splitlines(), 1):
                line = re.sub(r'"[^"]*"', '""', line)          # 引號裡面的括號（例如 python -c）不算
                self.assertIsNone(re.search(r'\bdo\s*\($', line), (name, i))
                self.assertIsNone(re.search(r'\)\s*else\b', line), (name, i))
                self.assertIsNone(re.match(r'^\s*\)\s*$', line), (name, i))
                self.assertIsNone(re.search(r'\bif\b.*\($', line), (name, i))

    def test_no_percent_expansion_of_user_input(self):
        t = self.raw('setup.bat').decode('utf-8')
        for v in ('IN_CAPITAL', 'IN_RISK', 'ANS'):
            self.assertNotRegex(t, '%' + v + '%')

    def test_example_config_and_gitignore(self):
        b = self.raw('設定.範例.txt')
        self.assertFalse(b.startswith(b'\xef\xbb\xbf'))
        c, err = quiet(screener.load_config, os.path.join(ROOT, '設定.範例.txt'))
        self.assertEqual(err, '')
        self.assertEqual((c['capital'], c['risk'], c['mode']), (None, 0.02, 'full'))
        self.assertTrue(c['watchlist'].endswith('自選股.csv'))
        self.assertTrue(c['output_dir'].endswith('結果'))
        lines = self.raw('.gitignore').decode('utf-8').splitlines()
        self.assertLess(lines.index('設定*.txt'), lines.index('!設定.範例.txt'))
        self.assertEqual(self.raw('.gitattributes').decode('ascii').strip(), '*.bat text eol=crlf')


if __name__ == '__main__':
    unittest.main()
